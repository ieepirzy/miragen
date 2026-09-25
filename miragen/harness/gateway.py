"""The tool gateway: one MCP endpoint serving an agent's whole tool set to a
harness that can't run miragen's tools in-process (docs/design/harnesses.md).

A foreign harness (Grok Build) can't call Python functions and doesn't go
through PydanticAI's hooks. So the gateway gives it, over MCP:

* the profile's ``MCP`` capabilities, proxied — named ``<capability>_<tool>``,
  honouring ``allowed_tools``; miragen holds the upstream credentials;
* runtime tools (``speak``, memory, scheduling) — the same closures the
  PydanticAI tier gets, run with the app's run/instance context bound;
* ``@register``-ed local tools, called with a small run-context shim.

Every call passes through ``call_tool`` here, which is where the host keeps
authority, exactly as in the PydanticAI tier: ``approval_required`` globs go
through the shared approval decision, and each call is recorded against the
active run. It fails closed: an unknown credential is a 401, and a known one
with no turn in progress is refused.

Identity: one bearer credential per *instance* (a harness session binds its
MCP servers once, so a per-run token could not rotate). The instance's active
run is the one turn admission allows at a time.
"""

from __future__ import annotations

import contextlib
import contextvars
import inspect
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp.tools.base import Tool as FnTool
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from miragen.approval import ApprovalDenied, approval_gated, decide_approval, with_approver_note
from miragen.models import AgentProfile, ToolCallRecord

logger = logging.getLogger("miragen.gateway")

_ARGS_MAX = 2_000
_UPSTREAM_TOOLS_TTL_S = 300.0
# A server that failed to list its tools isn't asked again for this long, so
# an unreachable (e.g. not yet deployed) server doesn't slow every turn.
_UPSTREAM_BACKOFF_S = 120.0
# Capabilities a gateway harness can carry. Others (WebSearch, Thinking, Peer,
# custom PydanticAI capabilities) configure PydanticAI itself and mean
# nothing to a foreign harness; a profile naming them fails loudly.
GATEWAY_CAPABILITIES = frozenset({"MCP"})

BindContext = Callable[[str | None, str | None], contextlib.AbstractContextManager]

# (run_id, instance) of the gateway call in progress — read by the
# @register RunContext shim.
_CURRENT: contextvars.ContextVar[tuple[str | None, str | None]] = contextvars.ContextVar(
    "miragen_gateway_current", default=(None, None))


class GatewayConfigError(ValueError):
    pass


@dataclass
class _Upstream:
    name: str
    url: str
    token: str | None
    allowed: set[str] | None
    auth: "OAuthUpstream | None" = None
    # Send X-Harness-Session for the calling instance (miragen's memory
    # bridge binds its memory_*/store_* tools to a session this way).
    bridge_session: bool = False
    tools: list[types.Tool] = field(default_factory=list)
    fetched_at: float = 0.0
    failed_until: float = 0.0


@dataclass
class _LocalTool:
    name: str
    fn_tool: FnTool
    runtime: bool


@dataclass
class TurnLog:
    run_id: str | None
    calls: list[ToolCallRecord] = field(default_factory=list)


def _capability_entries(profile: AgentProfile) -> list[tuple[str, dict]]:
    out = []
    for entry in (profile.spec.capabilities or []) if profile.spec else []:
        if isinstance(entry, str):
            out.append((entry, {}))
        else:
            name, cfg = next(iter(entry.items()))
            out.append((name, dict(cfg or {})))
    return out


def unsupported_capabilities(profile: AgentProfile, native: frozenset[str] = frozenset()) -> list[str]:
    """Capabilities neither the gateway nor the harness itself can carry."""
    return [name for name, _ in _capability_entries(profile)
            if name not in GATEWAY_CAPABILITIES and name not in native]


def _plain_signature_tool(fn: Callable, name: str, shim: Callable[[], Any]) -> Callable:
    """A ctx-first ``@register`` tool as a plain function: the first
    parameter (PydanticAI's RunContext) is filled from ``shim`` per call and
    hidden from the schema."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if not params:
        return fn
    rest = params[1:]

    async def call(**kwargs):
        result = fn(shim(), **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    call.__name__ = name
    call.__doc__ = fn.__doc__
    call.__signature__ = sig.replace(parameters=rest)  # type: ignore[attr-defined]
    call.__annotations__ = {k: v for k, v in getattr(fn, "__annotations__", {}).items()
                            if k != params[0].name}
    return call


@dataclass
class GatewayRunContext:
    """What a ``@register`` tool gets for PydanticAI's RunContext on a gateway
    harness. Tools that only read ``ctx.deps`` / run identity work; tools that
    need PydanticAI internals (model, messages, usage) are PydanticAI-only."""

    deps: Any = None
    run_id: str | None = None
    instance: str | None = None
    tool_name: str | None = None


class ToolGateway:
    def __init__(
        self,
        profile: AgentProfile,
        *,
        runtime_tools: list[Callable] | None = None,
        registered_tools: dict[str, Callable] | None = None,
        bind_context: BindContext | None = None,
        env: dict[str, str] | None = None,
        upstream_client: Callable[..., Any] | None = None,
        native_capabilities: frozenset[str] = frozenset(),
        session_key_for: Callable[[str], str | None] | None = None,
        oauth_transport: Any = None,
    ):
        import os

        self.profile = profile
        self._env = env if env is not None else dict(os.environ)
        self._bind = bind_context or (lambda run_id, instance: contextlib.nullcontext())
        self._client_factory = upstream_client or streamablehttp_client
        self._session_key_for = session_key_for or (lambda instance: None)
        self._credentials: dict[str, str] = {}   # token -> instance
        self._by_instance: dict[str, str] = {}   # instance -> token
        self._active: dict[str, TurnLog] = {}    # instance -> the turn in progress
        self._upstreams: dict[str, _Upstream] = {}
        self._local: dict[str, _LocalTool] = {}
        self._tz = ZoneInfo(profile.timezone) if profile.timezone else None

        unsupported = unsupported_capabilities(profile, native_capabilities)
        if unsupported:
            raise GatewayConfigError(
                f"Agent '{profile.name}' uses capabilities {unsupported} that this harness "
                "can't carry (the gateway serves MCP capabilities; the harness itself "
                f"provides only {sorted(native_capabilities) or 'nothing else'})"
            )
        for cap_name, cfg in _capability_entries(profile):
            if cap_name not in GATEWAY_CAPABILITIES:
                continue  # harness-native (e.g. Grok's own web tools)
            name = cfg.get("name")
            if not name:
                raise GatewayConfigError("gateway MCP capabilities need a 'name' (the tool prefix)")
            if name in self._upstreams:
                raise GatewayConfigError(f"duplicate MCP capability name '{name}'")
            optional = bool(cfg.get("optional", False))
            token = None
            if cfg.get("bearer_token_env"):
                token = self._env.get(cfg["bearer_token_env"])
                if not token and optional:
                    logger.warning("gateway: optional MCP '%s' skipped (%s is not set)",
                                   name, cfg["bearer_token_env"])
                    continue
            auth = None
            if cfg.get("oauth"):
                if cfg.get("bearer_token_env"):
                    raise GatewayConfigError(f"MCP '{name}': use bearer_token_env or oauth, not both")
                from miragen.harness.upstream_auth import OAuthUpstream
                try:
                    auth = OAuthUpstream.from_config(cfg["url"], cfg["oauth"], self._env,
                                                     transport=oauth_transport)
                except ValueError as exc:
                    if optional:
                        logger.warning("gateway: optional MCP '%s' skipped (%s)", name, exc)
                        continue
                    raise GatewayConfigError(f"MCP '{name}': {exc}") from exc
            allowed = cfg.get("allowed_tools")
            self._upstreams[name] = _Upstream(name=name, url=cfg["url"], token=token,
                                              allowed=set(allowed) if allowed else None,
                                              auth=auth,
                                              bridge_session=bool(cfg.get("bridge_session")))
        for fn in runtime_tools or ():
            self._add_local(fn.__name__, fn, runtime=True)
        for tool_name in profile.tools or ():
            if tool_name not in (registered_tools or {}):
                raise GatewayConfigError(
                    f"Agent '{profile.name}' references unknown tools: ['{tool_name}']")
            self._add_local(tool_name, _plain_signature_tool(
                registered_tools[tool_name], tool_name, self._shim_for(tool_name)), runtime=False)

        self.server = Server("miragen-gateway")
        self.server.list_tools()(self._list_tools)
        self.server.call_tool(validate_input=True)(self._call_tool_handler)
        self._manager = StreamableHTTPSessionManager(
            app=self.server, stateless=True, json_response=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    # ── identity and turns ───────────────────────────────────────────────
    def credential(self, instance: str) -> str:
        """The bearer a harness session for `instance` uses (stable for this
        process; a restarted container issues new ones, and resumed harness
        sessions are handed the new credential)."""
        token = self._by_instance.get(instance)
        if token is None:
            token = secrets.token_urlsafe(32)
            self._by_instance[instance] = token
            self._credentials[token] = instance
        return token

    def instance_for(self, token: str | None) -> str | None:
        return self._credentials.get(token or "")

    @contextlib.contextmanager
    def turn(self, instance: str, run_id: str | None) -> Iterator[TurnLog]:
        if instance in self._active:
            raise RuntimeError(f"instance '{instance}' already has a turn in progress")
        log = TurnLog(run_id=run_id)
        self._active[instance] = log
        try:
            yield log
        finally:
            self._active.pop(instance, None)

    # ── tool table ───────────────────────────────────────────────────────
    def _shim_for(self, tool_name: str) -> Callable[[], GatewayRunContext]:
        def shim() -> GatewayRunContext:
            run_id, instance = _CURRENT.get()
            return GatewayRunContext(run_id=run_id, instance=instance, tool_name=tool_name)
        return shim

    def _add_local(self, name: str, fn: Callable, *, runtime: bool) -> None:
        if name in self._local:
            raise GatewayConfigError(f"duplicate gateway tool '{name}'")
        self._local[name] = _LocalTool(name=name, fn_tool=FnTool.from_function(fn, name=name),
                                       runtime=runtime)

    async def _upstream_tools(self, up: _Upstream) -> list[types.Tool]:
        if up.tools and time.monotonic() - up.fetched_at < _UPSTREAM_TOOLS_TTL_S:
            return up.tools
        if time.monotonic() < up.failed_until:
            raise RuntimeError(f"{up.name} unreachable recently; backing off")
        try:
            async with self._session(up) as session:
                listed = await session.list_tools()
        except Exception:
            up.failed_until = time.monotonic() + _UPSTREAM_BACKOFF_S
            if up.auth is not None:
                up.auth.invalidate()
            raise
        tools = [t for t in listed.tools if up.allowed is None or t.name in up.allowed]
        up.tools, up.fetched_at = tools, time.monotonic()
        return tools

    @contextlib.asynccontextmanager
    async def _session(self, up: _Upstream) -> AsyncIterator[ClientSession]:
        # miragen authenticates to the upstream with its own credential; the
        # caller's Authorization (the gateway bearer) is never forwarded.
        token = up.token
        if up.auth is not None:
            token = await up.auth.token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # Which agent/run/instance is calling, for upstream attribution (a
        # harness session binds its MCP credentials once, so identity can't
        # ride a per-run token). Informational: upstream authorization is
        # still miragen's own credential above.
        run_id, instance = _CURRENT.get()
        headers["X-Miragen-Agent"] = self.profile.name
        if run_id:
            headers["X-Miragen-Run-Id"] = run_id
        if instance:
            headers["X-Miragen-Instance"] = instance
            if up.bridge_session:
                key = self._session_key_for(instance)
                if key:
                    headers["X-Harness-Session"] = key
        async with self._client_factory(up.url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def _list_tools(self) -> list[types.Tool]:
        out: list[types.Tool] = []
        for local in self._local.values():
            t = local.fn_tool
            out.append(types.Tool(name=t.name, description=t.description or "",
                                  inputSchema=t.parameters))
        for up in self._upstreams.values():
            try:
                tools = await self._upstream_tools(up)
            except Exception as exc:  # one dead upstream must not hide the rest
                logger.warning("gateway: listing %s failed: %s", up.name, type(exc).__name__)
                continue
            for t in tools:
                out.append(t.model_copy(update={"name": f"{up.name}_{t.name}"}))
        return out

    def _resolve(self, name: str) -> tuple[str, Any, str]:
        """(kind, target, raw_name) for a gateway tool name."""
        if name in self._local:
            return "local", self._local[name], name
        for up in self._upstreams.values():
            prefix = f"{up.name}_"
            if name.startswith(prefix):
                raw = name[len(prefix):]
                if up.allowed is None or raw in up.allowed:
                    return "upstream", up, raw
        raise KeyError(name)

    # ── calls ────────────────────────────────────────────────────────────
    async def _call_tool_handler(self, name: str, arguments: dict) -> types.CallToolResult:
        ctx = self.server.request_context
        request = getattr(ctx, "request", None)
        header = request.headers.get("authorization", "") if request is not None else ""
        token = header[7:] if header.lower().startswith("bearer ") else None
        return await self.call_tool(name, arguments, instance=self.instance_for(token))

    async def call_tool(self, name: str, arguments: dict, *, instance: str | None) -> types.CallToolResult:
        if instance is None:
            return _error("unknown gateway credential")
        log = self._active.get(instance)
        if log is None:
            # Fail closed: a tool call outside a turn has no run to answer to.
            return _error(f"no turn in progress for instance '{instance}'")
        try:
            kind, target, raw = self._resolve(name)
        except KeyError:
            return self._record(log, name, arguments, ok=False,
                                result=_error(f"unknown tool '{name}'"))
        response = None
        if approval_gated(self.profile, name, raw, args=arguments):
            try:
                response = await decide_approval(self.profile, name, arguments)
            except ApprovalDenied as exc:
                return self._record(log, name, arguments, ok=False, result=_error(str(exc)))
            if self._active.get(instance) is not log:
                # The turn that asked ended (timeout, cancel) while the approval
                # waited: nothing may run on behalf of a turn that is gone.
                logger.warning("gateway: %s approved after its turn ended; not executed", name)
                return self._record(log, name, arguments, ok=False, result=_error(
                    f"'{name}' was approved only after this turn had ended; it was not run"))
        token = _CURRENT.set((log.run_id, instance))
        try:
            with self._bind(log.run_id, instance):
                if kind == "local":
                    value = await target.fn_tool.run(arguments)
                    result = _text_result(value)
                else:
                    try:
                        async with self._session(target) as session:
                            result = await session.call_tool(raw, arguments)
                    except Exception:
                        if target.auth is None:
                            raise
                        # An expired or revoked token: one fresh authorization.
                        target.auth.invalidate()
                        async with self._session(target) as session:
                            result = await session.call_tool(raw, arguments)
        except Exception as exc:
            logger.warning("gateway tool %s failed: %s", name, exc)
            return self._record(log, name, arguments, ok=False, result=_error(f"{type(exc).__name__}: {exc}"))
        finally:
            _CURRENT.reset(token)
        if response is not None and response.prompt:
            result = result.model_copy(update={"content": [
                types.TextContent(type="text", text=with_approver_note(response, "").rstrip())
            ] + list(result.content)})
        return self._record(log, name, arguments, ok=not result.isError, result=result)

    def _record(self, log: TurnLog, name: str, arguments: dict, *, ok: bool,
                result: types.CallToolResult) -> types.CallToolResult:
        log.calls.append(ToolCallRecord(
            tool_name=name, args=json.dumps(arguments, default=str)[:_ARGS_MAX], ok=ok,
            result_status=_result_status(result)))
        if self._tz is not None:
            result = with_local_time(result, datetime.now(self._tz))
        return result

    # ── serving ──────────────────────────────────────────────────────────
    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        async with self._manager.run():
            yield

    async def asgi(self, scope, receive, send) -> None:
        """The endpoint (mount at /mcp/gateway). Unknown credential = 401."""
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            header = headers.get(b"authorization", b"").decode(errors="replace")
            token = header[7:] if header.lower().startswith("bearer ") else None
            if self.instance_for(token) is None:
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self._manager.handle_request(scope, receive, send)


def with_local_time(result: types.CallToolResult, now: datetime) -> types.CallToolResult:
    """The result, led by the time it was produced (minute resolution, the
    agent's zone). Structured content is left as it is."""
    stamp = types.TextContent(type="text", text=f"[{now:%Y-%m-%d %H:%M %Z}]")
    return result.model_copy(update={"content": [stamp, *result.content]})


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)


def _text_result(value: Any) -> types.CallToolResult:
    if isinstance(value, types.CallToolResult):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, types.TextContent) for v in value):
        return types.CallToolResult(content=list(value))
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def _result_status(result: types.CallToolResult) -> str | None:
    """A JSON result's top-level "status" string, else None."""
    if result.isError:
        return None
    text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
    try:
        value = json.loads(text)
    except ValueError:
        return None
    status = value.get("status") if isinstance(value, dict) else None
    return status[:40] if isinstance(status, str) else None
