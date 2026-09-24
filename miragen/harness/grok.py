"""The Grok Build harness: base-tier turns on the Grok subscription, over
ACP, with one long-lived ``grok agent`` process per instance.

Design record: docs/design/harnesses.md. In short:

* **Conversation state is Grok's.** Each instance maps to a Grok session,
  resumed with ``session/load`` whenever its process is (re)started. The
  mapping (session id + a hash of the instructions it was created with) is
  persisted beside GROK_HOME, so a container restart continues the same
  conversations.
* **Tools come only from the miragen tool gateway.** The hermetic GROK_HOME
  (config.toml + fail_closed requirements.toml, isolated HOME) declares the
  gateway as the one MCP server; each process gets its instance's gateway
  credential in its environment (``${MIRAGEN_GATEWAY_TOKEN}`` in the config,
  never on disk). An agent profile removes the built-in tools
  (``tools: search_tool, use_tool``), and permission requests for anything
  but a gateway tool are rejected host-side. The gateway enforces approvals
  and fails closed on its own.
* **Subscription only.** The process environment is an allowlist: no
  XAI_API_KEY, no GROK_* overrides, nothing from the operator's shell.
* **Instructions are stable per session.** ``spec.instructions`` goes in
  once as the session's ``rules``. If they change, the next turn forks the
  session so history is kept and the fork carries the new text.
* **Per-turn context rides the prompt.** The memory packet is prepended to
  the turn's prompt (decision 6); it persists in Grok's transcript.

What is verified only against a live, logged-in grok (see the PR): that the
agent profile really removes built-ins, that permission requests carry the
gateway tool names this module keys on, that fork accepts new rules.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from grok_build_client import AcpSession

from miragen.executor.grok_hermetic import hermetic_home_dir, write_hermetic_home
from miragen.harness.base import HarnessResult, HarnessTurn, parse_harness_model
from miragen.harness.gateway import ToolGateway
from miragen.models import AgentProfile, RunUsage

logger = logging.getLogger("miragen.harness.grok")

NAME = "grok-build"
GATEWAY_SERVER = "gateway"
GATEWAY_TOKEN_ENV = "MIRAGEN_GATEWAY_TOKEN"
STATE_FILE = "miragen-instances.json"
PROFILE_FILE = "miragen-agent-profile.md"
# The only environment a grok process inherits from the container.
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
                 "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
# Tool names that are dispatchers, not tools: allowed only when they carry a
# gateway tool (checked via the call's content).
DISPATCHERS = ("use_tool", "search_tool")


class GrokHarnessError(RuntimeError):
    pass


@dataclass
class GrokSettings:
    grok_home: Path
    workdirs: Path
    gateway_url: str
    grok_bin: str | None = None
    max_processes: int = 3
    idle_s: float = 900.0
    turn_timeout_s: float = 600.0

    @classmethod
    def from_env(cls, *, gateway_url: str) -> "GrokSettings":
        env = os.environ
        return cls(
            grok_home=Path(env.get("MIRAGEN_GROK_HOME", "/agent/grok-home")),
            workdirs=Path(env.get("MIRAGEN_GROK_WORKDIRS", "/agent/workspaces")),
            gateway_url=gateway_url,
            grok_bin=env.get("GROK_BIN") or None,
            max_processes=int(env.get("MIRAGEN_GROK_MAX_PROCESSES", "3")),
            idle_s=float(env.get("MIRAGEN_GROK_IDLE_S", "900")),
            turn_timeout_s=float(env.get("MIRAGEN_GROK_TURN_TIMEOUT_S", "600")),
        )


@dataclass
class _Agent:
    instance: str
    acp: AcpSession
    session_id: str
    ephemeral: bool
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    # Turns that hold this agent (reserved under the spawn lock, before the
    # turn takes `lock`): eviction and the reaper never touch a reserved agent.
    reserved: int = 0

    @property
    def busy(self) -> bool:
        return self.reserved > 0 or self.lock.locked()


def instructions_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


_GATEWAY_TOOL = re.compile(rf"^{GATEWAY_SERVER}__[A-Za-z0-9_.-]+$")
# Fields that name the tool being called — never its (model-written) input.
_IDENTITY_FIELDS = ("toolName", "tool_name", "name", "title")
_DISPATCH_SERVER = ("server", "server_name", "serverName", "mcp_server")
_DISPATCH_TOOL = ("tool", "tool_name", "toolName", "name")


def is_gateway_tool_call(params: dict[str, Any]) -> bool:
    """Does a permission request concern a gateway tool?

    Only the call's *identity* is examined: a tool-name field that is
    exactly ``gateway__<tool>``, or a use_tool/search_tool dispatch whose
    target server is exactly ``gateway``. Arguments are never searched —
    they are model-written, and a built-in whose input merely mentions a
    gateway tool must still be refused.

    This layer only sees tools that ask permission. Grok auto-runs some
    read-only built-ins without asking, so removing built-ins through the
    agent profile remains the primary barrier; the gateway is the last."""
    call = params.get("toolCall")
    if not isinstance(call, dict):
        return False
    names = [call.get(k) for k in _IDENTITY_FIELDS if isinstance(call.get(k), str)]
    if any(_GATEWAY_TOOL.fullmatch(n) for n in names):
        return True
    raw = call.get("rawInput")
    if any(n in DISPATCHERS for n in names) and isinstance(raw, dict):
        server = next((raw[k] for k in _DISPATCH_SERVER if isinstance(raw.get(k), str)), None)
        tool = next((raw[k] for k in _DISPATCH_TOOL if isinstance(raw.get(k), str)), None)
        if server is not None:
            return server == GATEWAY_SERVER and bool(tool)
        return bool(tool and _GATEWAY_TOOL.fullmatch(tool))
    return False


class GrokHarness:
    name = NAME

    def __init__(
        self,
        profile: AgentProfile,
        gateway: ToolGateway,
        settings: GrokSettings,
        *,
        session_factory: Callable[..., AcpSession] | None = None,
        system_guidance: str | None = None,
    ):
        if profile.spec is None:
            raise GrokHarnessError("the grok-build harness runs base-tier (spec:) profiles")
        self.profile = profile
        self.gateway = gateway
        self.settings = settings
        self.model = parse_harness_model(profile.spec.model)[1] or None
        # System instructions = identity + stable profile-level guidance
        # (the voice renderer's 'Speaking aloud' section). Part of the
        # session's rules, so a change forks the session (decision 7).
        from miragen.voice import with_voice_guidance

        self.instructions = with_voice_guidance(profile.spec.instructions or "", system_guidance)
        self._session_factory = session_factory or AcpSession
        self._agents: dict[str, _Agent] = {}
        self._spawn_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        self.prepare()

    # ── home, profile, state ─────────────────────────────────────────────
    def prepare(self) -> None:
        home = self.settings.grok_home
        spec = SimpleNamespace(web_search=False, mcp_servers=[SimpleNamespace(
            name=GATEWAY_SERVER, url=self.settings.gateway_url, bearer_token_env=GATEWAY_TOKEN_ENV)])
        write_hermetic_home(home, self.profile.name, spec)
        # Built-in tools removed: the MCP dispatchers are all that's left.
        (home / PROFILE_FILE).write_text(
            "---\n"
            f"name: miragen-{self.profile.name}\n"
            "description: miragen base-tier agent; acts only through the miragen tool gateway\n"
            "tools: search_tool, use_tool\n"
            "---\n"
        )
        self.settings.workdirs.mkdir(parents=True, exist_ok=True)

    def _state_path(self) -> Path:
        return self.settings.grok_home / STATE_FILE

    def _load_state(self) -> dict[str, dict]:
        try:
            return json.loads(self._state_path().read_text())
        except (FileNotFoundError, ValueError):
            return {}

    async def _save_instance(self, instance: str, session_id: str) -> None:
        async with self._state_lock:
            state = self._load_state()
            state[instance] = {"session_id": session_id,
                               "instructions_sha": instructions_hash(self.instructions),
                               "updated_at": time.time()}
            tmp = self._state_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
            os.replace(tmp, self._state_path())

    def _env(self, instance: str) -> dict[str, str]:
        env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
        env.update({
            "HOME": str(hermetic_home_dir(self.settings.grok_home)),
            "GROK_HOME": str(self.settings.grok_home),
            GATEWAY_TOKEN_ENV: self.gateway.credential(instance),
        })
        return env

    # ── processes ────────────────────────────────────────────────────────
    async def _permission(self, params: dict[str, Any]) -> str:
        if is_gateway_tool_call(params):
            return "allow"
        logger.warning("grok harness: refused a non-gateway tool permission request")
        return "deny"

    async def _spawn(self, instance: str, *, ephemeral: bool) -> _Agent:
        workdir = self.settings.workdirs / instance
        workdir.mkdir(parents=True, exist_ok=True)
        acp = self._session_factory(
            grok_bin=self.settings.grok_bin,
            grok_home=str(self.settings.grok_home),
            always_approve=False,
            model=self.model,
            env=self._env(instance),
            permission_handler=self._permission,
            extra_args=["--agent-profile", str(self.settings.grok_home / PROFILE_FILE)],
            cwd=str(workdir),
            strict_auth=True,
        )
        try:
            await acp.start()
            session_id = await self._open_session(acp, instance, str(workdir), ephemeral=ephemeral)
        except BaseException:
            await acp.close()  # never leave a half-started agent running
            raise
        return _Agent(instance=instance, acp=acp, session_id=session_id, ephemeral=ephemeral)

    async def _open_session(self, acp: AcpSession, instance: str, cwd: str, *,
                            ephemeral: bool) -> str:
        rules = {"rules": self.instructions} if self.instructions else {}
        known = None if ephemeral else self._load_state().get(instance)
        if known:
            if known.get("instructions_sha") == instructions_hash(self.instructions):
                return await acp.session_load(known["session_id"], cwd, mcp_servers=[])
            # Instructions changed: fork so history is kept and the fork
            # carries the new rules (decision 7).
            try:
                forked = await acp.request("x.ai/session/fork", {
                    "sessionId": known["session_id"], "cwd": cwd, "mcpServers": [],
                    "_meta": rules})
                session_id = str((forked or {}).get("sessionId") or "")
                if session_id:
                    await self._save_instance(instance, session_id)
                    logger.info("grok harness: forked %s for new instructions", instance)
                    return session_id
            except Exception as exc:
                logger.warning("grok harness: fork of %s failed (%s); starting a new session",
                               instance, exc)
        session_id = await acp.session_new(cwd, mcp_servers=[], yolo=False, meta=rules)
        if not ephemeral:
            await self._save_instance(instance, session_id)
        return session_id

    async def _agent_for(self, instance: str, *, ephemeral: bool) -> _Agent:
        async with self._spawn_lock:
            agent = self._agents.get(instance)
            if agent is not None and agent.acp.alive:
                agent.reserved += 1
                return agent
            if agent is not None:  # died since last turn: resume via session/load
                self._agents.pop(instance, None)
                with contextlib.suppress(Exception):
                    await agent.acp.close()
            await self._evict_for_capacity()
            agent = await self._spawn(instance, ephemeral=ephemeral)
            agent.reserved += 1
            self._agents[instance] = agent
            self._ensure_reaper()
            return agent

    async def _evict_for_capacity(self) -> None:
        idle = sorted((a for a in self._agents.values() if not a.busy),
                      key=lambda a: a.last_used)
        while len(self._agents) >= self.settings.max_processes and idle:
            await self._drop(idle.pop(0).instance)

    async def _drop(self, instance: str) -> None:
        agent = self._agents.pop(instance, None)
        if agent is not None:
            with contextlib.suppress(Exception):
                await agent.acp.close()

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_idle(), name="grok-harness-reaper")

    async def _reap_idle(self) -> None:
        while self._agents:
            await asyncio.sleep(min(60.0, max(1.0, self.settings.idle_s / 4)))
            now = time.monotonic()
            for agent in list(self._agents.values()):
                if not agent.busy and now - agent.last_used > self.settings.idle_s:
                    logger.info("grok harness: stopping idle process for %s", agent.instance)
                    async with self._spawn_lock:
                        await self._drop(agent.instance)

    # ── turns ────────────────────────────────────────────────────────────
    def _key(self, turn: HarnessTurn) -> tuple[str, bool]:
        if turn.use_history and turn.instance:
            return turn.instance, False
        return f"run-{turn.run_id or os.urandom(6).hex()}", True

    @staticmethod
    def _compose(turn: HarnessTurn) -> str:
        if not turn.extra_instructions:
            return turn.prompt
        return (f"<context source=\"miragen\">\n{turn.extra_instructions}\n</context>\n\n"
                f"{turn.prompt}")

    async def _turn(self, turn: HarnessTurn, on_text: Callable[[str], None] | None) -> HarnessResult:
        if turn.secret_env:
            raise GrokHarnessError(
                "per-launch MCP credentials are not supported by the grok-build harness "
                "yet (the gateway holds deployment-level upstream credentials)")
        instance, ephemeral = self._key(turn)
        agent = await self._agent_for(instance, ephemeral=ephemeral)
        chunks: list[str] = []
        end: dict[str, Any] = {}
        try:
            async with agent.lock:
                agent.last_used = time.monotonic()
                with self.gateway.turn(instance, turn.run_id) as log:
                    try:
                        async with asyncio.timeout(self.settings.turn_timeout_s):
                            async for update in agent.acp.prompt(agent.session_id, self._compose(turn)):
                                kind = update.get("type")
                                if kind == "text":
                                    chunks.append(update["data"])
                                    if on_text:
                                        on_text(update["data"])
                                elif kind == "error":
                                    raise GrokHarnessError(
                                        f"grok: {update.get('message')}; "
                                        f"stderr: {' | '.join(agent.acp.stderr_tail()[-5:])}")
                                elif kind == "end":
                                    end = update
                    except TimeoutError:
                        with contextlib.suppress(Exception):
                            await agent.acp.cancel(agent.session_id)
                        await self._drop(instance)
                        raise GrokHarnessError(
                            f"turn exceeded {self.settings.turn_timeout_s:.0f}s; cancelled") from None
                    calls = list(log.calls)
                agent.last_used = time.monotonic()
        finally:
            agent.reserved -= 1
            if ephemeral:
                await self._drop(instance)
        if not agent.acp.alive and not ephemeral:
            await self._drop(instance)  # next turn respawns and session/loads
        stop = end.get("stopReason")
        if stop == "cancelled":
            raise GrokHarnessError("turn was cancelled")
        if stop == "refusal":
            raise GrokHarnessError("the model refused the turn")
        return HarnessResult(output="".join(chunks), usage=_usage(end.get("usage") or {}),
                             tool_calls=calls)

    async def run(self, turn: HarnessTurn) -> HarnessResult:
        return await self._turn(turn, None)

    @contextlib.asynccontextmanager
    async def stream(self, turn: HarnessTurn) -> AsyncIterator["_GrokStream"]:
        stream = _GrokStream()
        task = asyncio.create_task(self._turn(turn, stream.push))
        stream.attach(task)
        try:
            yield stream
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    async def aclose(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
        for instance in list(self._agents):
            await self._drop(instance)

    def status(self) -> dict[str, Any]:
        return {"processes": sorted(self._agents), "max_processes": self.settings.max_processes}


class _GrokStream:
    def __init__(self):
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._result: HarnessResult | None = None

    def attach(self, task: asyncio.Task) -> None:
        self._task = task
        task.add_done_callback(lambda _t: self._queue.put_nowait(None))

    def push(self, text: str) -> None:
        self._queue.put_nowait(text)

    async def __aiter__(self):
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item
        assert self._task is not None
        self._result = await self._task  # re-raises a failed turn

    @property
    def result(self) -> HarnessResult:
        if self._result is None:
            raise RuntimeError("stream not finished")
        return self._result


def _usage(raw: dict[str, Any]) -> RunUsage:
    def pick(*keys):
        for k in keys:
            if isinstance(raw.get(k), int):
                return raw[k]
        return None
    return RunUsage(requests=1,
                    input_tokens=pick("inputTokens", "input_tokens", "promptTokens"),
                    output_tokens=pick("outputTokens", "output_tokens", "completionTokens"),
                    cached_input_tokens=pick("cachedInputTokens", "cache_read_input_tokens"))
