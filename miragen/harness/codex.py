"""The Codex harness: base-tier turns on the ChatGPT subscription, over the
Codex app-server's JSON-RPC (stdio), with one long-lived ``codex
app-server`` process per instance.

Design record: docs/design/harnesses.md ("The Codex harness"). Same shape as
the Grok and Claude Code harnesses, so a profile swaps between them with
``spec.model``. What is Codex-specific is the tool boundary: Codex has no
"no built-in tools" switch, so it is built in three layers, each verified
live (codex 0.159):

1. **Feature flags** take the shell and the rest of the coding-agent kit out
   of the model's tool list (``FEATURES_OFF``). What stays listed — code
   mode's ``exec`` (a bare ECMAScript isolate: no process, require, import,
   fetch or sockets; it only calls the nested tools, which route through
   layer 2 like any other call), ``apply_patch``, the
   collaboration (sub-agent) tools, ``web.run`` — has no filesystem or
   process access of its own. The gateway's tools are *dynamic tools*
   declared on the thread (experimental app-server API): always visible,
   never deferred behind discovery the way Codex defers MCP tools, and each
   call comes back to this process (``item/tool/call``) and runs through
   ``ToolGateway.call_tool`` — approvals, run binding, call records. No MCP
   server and no gateway credential are involved.
2. **Every consequential action needs an approval, and the host denies them
   all.** The thread runs read-only with ``approvalPolicy: untrusted`` and
   the ``user`` reviewer, so file changes, command executions and
   permission escalations (also from sub-agents and from inside code mode)
   arrive here as server requests; everything that is not a gateway MCP call
   is declined.
3. **Nothing worth taking.** The process environment is an allowlist: no
   API keys, none of the deployment's upstream MCP secrets, no gateway
   credential. ``auth.json`` (the ChatGPT login) is in
   CODEX_HOME, which no remaining tool can read.

Conversation state is Codex's (a thread per instance, its rollout under
``$CODEX_HOME/sessions``). The memory packet rides the prompt (decision 6);
``spec.instructions`` replaces Codex's own base instructions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from miragen.harness.base import (
    HarnessResult, HarnessTurn, InstanceBusyError, TaskStream, parse_harness_model,
)
from miragen.harness.gateway import ToolGateway, _capability_entries
from miragen.harness.served import ServedLedger
from miragen.models import AgentProfile, RunUsage

logger = logging.getLogger("miragen.harness.codex")

NAME = "codex"
STATE_FILE = "miragen-instances.json"
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
                 "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
# Everything of Codex's coding-agent kit that a feature flag can switch off.
# NOT code_mode_host: gpt-6 models reach dynamic tools only through code
# mode's exec (observed live: with the host off every tool call failed).
FEATURES_OFF = (
    "shell_tool", "unified_exec", "shell_snapshot", "multi_agent",
    "multi_agent_v2", "apps", "plugins", "remote_plugin", "memories", "browser_use",
    "browser_use_external", "in_app_browser", "computer_use", "image_generation", "view_image",
    "goals", "skill_search", "skill_mcp_dependency_install", "tool_suggest", "sleep_tool", "hooks",
)
_CARRY_MAX_ITEMS = 200
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Codex's web tool (web.run) searches and opens pages: it carries both.
NATIVE_CAPABILITIES = frozenset({"WebSearch", "WebFetch"})
_APPROVAL_REQUESTS = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "item/tool/requestUserInput": {"answers": {}},
    "mcpServer/elicitation/request": {"action": "decline"},
}
_BASE_GUIDANCE = (
    "\n\n# Tools\nYou act only through the tools you were given (and web search when it "
    "is available). You have no shell and no filesystem. Do not spawn sub-agents.")


class CodexHarnessError(RuntimeError):
    pass


@dataclass
class CodexSettings:
    codex_home: Path
    workdirs: Path
    gateway_url: str
    codex_bin: str | None = None
    max_processes: int = 3
    idle_s: float = 900.0
    turn_timeout_s: float = 600.0
    effort: str | None = None
    # Codex compacts on its own at this context size (config
    # model_auto_compact_token_limit). None leaves the model's default.
    auto_compact_tokens: int | None = None

    @classmethod
    def from_env(cls, *, gateway_url: str) -> "CodexSettings":
        env = os.environ
        home = Path(env.get("MIRAGEN_CODEX_HOME", "/agent/codex-home"))
        return cls(
            codex_home=home,
            workdirs=Path(env.get("MIRAGEN_CODEX_WORKDIRS", str(home / "workspaces"))),
            gateway_url=gateway_url,
            codex_bin=env.get("CODEX_BIN") or None,
            max_processes=int(env.get("MIRAGEN_CODEX_MAX_PROCESSES", "3")),
            idle_s=float(env.get("MIRAGEN_CODEX_IDLE_S", "900")),
            turn_timeout_s=float(env.get("MIRAGEN_CODEX_TURN_TIMEOUT_S", "600")),
            effort=env.get("MIRAGEN_CODEX_EFFORT") or None,
            auto_compact_tokens=(int(env["MIRAGEN_CODEX_AUTO_COMPACT_TOKENS"])
                                 if env.get("MIRAGEN_CODEX_AUTO_COMPACT_TOKENS") else None),
        )


def bundled_codex() -> str | None:
    """The codex binary shipped with openai-codex (codex-cli-bin)."""
    with contextlib.suppress(Exception):
        import codex_cli_bin

        return str(codex_cli_bin.bundled_codex_path())
    return None


class AppServer:
    """A minimal JSON-RPC client for one ``codex app-server`` process."""

    def __init__(self, argv: list[str], *, env: dict[str, str], cwd: str,
                 on_request: Callable[[str, dict], Awaitable[dict]]):
        self.argv, self.env, self.cwd, self._on_request = argv, env, cwd, on_request
        self.proc: asyncio.subprocess.Process | None = None
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}
        # threadId -> queue of notifications for that thread
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._tasks: list[asyncio.Task] = []
        self.stderr: list[str] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.env, cwd=self.cwd, limit=2 ** 24)
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._drain_stderr())]
        # experimentalApi: dynamic tools (the gateway's tools) are experimental.
        await self.request("initialize", {"clientInfo": {"name": "miragen", "version": "1"},
                                          "capabilities": {"experimentalApi": True}})
        await self._send({"method": "initialized"})

    async def _send(self, obj: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict, *, timeout: float = 120.0) -> Any:
        self._next += 1
        rid = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"id": rid, "method": method, "params": params})
        async with asyncio.timeout(timeout):
            return await fut

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        return self._subscribers.setdefault(thread_id, asyncio.Queue())

    def unsubscribe(self, thread_id: str) -> None:
        self._subscribers.pop(thread_id, None)

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while line := await self.proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "id" in msg and "method" not in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_exception(CodexHarnessError(
                                f"codex {msg['error'].get('message', msg['error'])}"))
                        else:
                            fut.set_result(msg.get("result"))
                    continue
                if "id" in msg:  # a server request: answered by the harness
                    asyncio.create_task(self._answer(msg))
                    continue
                params = msg.get("params") or {}
                queue = self._subscribers.get(params.get("threadId", ""))
                if queue is not None:
                    queue.put_nowait(msg)
                elif msg.get("method") in ("mcpServer/startupStatus/updated", "configWarning",
                                           "warning", "error"):
                    logger.info("codex: %s %s", msg.get("method"), json.dumps(params)[:300])
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CodexHarnessError("codex app-server exited"))
            for queue in self._subscribers.values():
                queue.put_nowait({"method": "_exited"})

    async def _answer(self, msg: dict) -> None:
        try:
            result = await self._on_request(msg["method"], msg.get("params") or {})
            await self._send({"id": msg["id"], "result": result})
        except Exception as exc:  # noqa: BLE001 — an unanswerable request is refused, never left hanging
            with contextlib.suppress(Exception):
                await self._send({"id": msg["id"], "error": {"code": -32603, "message": str(exc)}})

    async def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while line := await self.proc.stderr.readline():
            self.stderr = [*self.stderr[-39:], line.decode(errors="replace").rstrip()]

    async def close(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                await self.proc.wait()
        for task in self._tasks:
            task.cancel()


@dataclass
class _Agent:
    instance: str
    server: AppServer
    thread_id: str
    ephemeral: bool
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    reserved: int = 0

    @property
    def busy(self) -> bool:
        return self.reserved > 0 or self.lock.locked()


class CodexHarness:
    name = NAME
    native_capabilities = NATIVE_CAPABILITIES

    def __init__(self, profile: AgentProfile, gateway: ToolGateway, settings: CodexSettings, *,
                 system_guidance: str | None = None, served: ServedLedger | None = None):
        if profile.spec is None:
            raise CodexHarnessError("the codex harness runs base-tier (spec:) profiles")
        self.profile = profile
        self.gateway = gateway
        self.settings = settings
        self.served = served
        self.model = parse_harness_model(profile.spec.model)[1] or None
        caps = {name for name, _ in _capability_entries(profile)}
        self.web = bool(caps & NATIVE_CAPABILITIES)
        from miragen.voice import with_voice_guidance

        self.instructions = with_voice_guidance(profile.spec.instructions or "", system_guidance)
        self.tool_timeout_s: int | None = (
            profile.approval_timeout_s + 120 if profile.approval_required else None)
        if self.tool_timeout_s and settings.turn_timeout_s < self.tool_timeout_s + 60:
            logger.warning("codex harness: raising the turn timeout from %.0fs to %ds so it "
                           "outlasts gated tool calls", settings.turn_timeout_s,
                           self.tool_timeout_s + 60)
            settings.turn_timeout_s = self.tool_timeout_s + 60
        self._agents: dict[str, _Agent] = {}
        self._aliases: dict[str, str] = {}  # dynamic tool name -> gateway tool name
        self._spawn_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        self.on_lifecycle: Callable[[str, str, dict], Awaitable[None]] | None = None
        self.prepare()

    # ── home, config, state ──────────────────────────────────────────────
    def config(self) -> dict[str, Any]:
        """Per-thread config overrides (also written to config.toml)."""
        cfg: dict[str, Any] = {f"features.{f}": False for f in FEATURES_OFF}
        cfg["web_search"] = "live" if self.web else "disabled"
        if self.settings.auto_compact_tokens:
            cfg["model_auto_compact_token_limit"] = self.settings.auto_compact_tokens
        return cfg

    def prepare(self) -> None:
        home = self.settings.codex_home
        home.mkdir(parents=True, exist_ok=True)
        (home / "home").mkdir(exist_ok=True)
        self.settings.workdirs.mkdir(parents=True, exist_ok=True)
        # Hermetic: this file is miragen's, rewritten at every start, so no
        # stale MCP server, hook or feature from a previous config survives.
        lines = ["# Generated by miragen (codex harness) at startup; edit the agent profile."]
        features = [f"{f} = false" for f in FEATURES_OFF]
        cfg = self.config()
        lines.append(f'web_search = "{cfg["web_search"]}"')
        if self.settings.auto_compact_tokens:
            lines.append(f"model_auto_compact_token_limit = {self.settings.auto_compact_tokens}")
        lines += ["", "[features]", *features]
        (home / "config.toml").write_text("\n".join(lines) + "\n")
        (home / "hooks.json").unlink(missing_ok=True)
        if not self._auth_mode():
            logger.warning("codex harness: no ChatGPT login in %s; turns will fail until "
                           "`codex login --device-auth` runs with CODEX_HOME set to it", home)

    def _auth_mode(self) -> str | None:
        try:
            return json.loads((self.settings.codex_home / "auth.json").read_text()).get("auth_mode")
        except (FileNotFoundError, ValueError, AttributeError):
            return None

    def _state_path(self) -> Path:
        return self.settings.codex_home / STATE_FILE

    def _load_state(self) -> dict[str, dict]:
        try:
            return json.loads(self._state_path().read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _write_state(self, state: dict) -> None:
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
        os.replace(tmp, self._state_path())

    async def _update_instance(self, instance: str, **fields: Any) -> dict:
        async with self._state_lock:
            state = self._load_state()
            entry = {**state.get(instance, {}), **fields, "updated_at": time.time()}
            state[instance] = entry
            self._write_state(state)
            return entry

    def _env(self, instance: str) -> dict[str, str]:
        env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
        env.update({"CODEX_HOME": str(self.settings.codex_home),
                    "HOME": str(self.settings.codex_home / "home")})
        return env

    # ── server requests: gateway tool calls run, everything else is denied ──
    async def _on_request(self, instance: str, method: str, params: dict) -> dict:
        if method == "item/tool/call":
            if params.get("namespace"):
                return _tool_output(_error_result(f"unknown tool {params.get('tool')}"))
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            tool = str(params.get("tool"))
            result = await self.gateway.call_tool(self._aliases.get(tool, tool), args,
                                                  instance=instance)
            return _tool_output(result)
        answer = _APPROVAL_REQUESTS.get(method)
        logger.warning("codex harness: refused %s", method)
        if answer is None:
            raise CodexHarnessError(f"unsupported request {method}")
        return answer

    # ── processes ────────────────────────────────────────────────────────
    async def _dynamic_tools(self) -> list[dict]:
        """The gateway's tools as dynamic tools. A name the model API won't
        take (``[A-Za-z0-9_-]{1,64}``) gets an alias that maps back to the
        gateway name, so one odd upstream tool can't break the thread."""
        out, aliases = [], {}
        for t in await self.gateway._list_tools():
            name = t.name
            if not _TOOL_NAME.fullmatch(name):
                base = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:58]
                name = base
                n = 1
                while name in aliases or any(o["name"] == name for o in out):
                    n += 1
                    name = f"{base}_{n}"
                aliases[name] = t.name
            out.append({"type": "function", "name": name, "description": t.description or t.name,
                        "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
                        "deferLoading": False})
        self._aliases.update(aliases)
        return out

    async def _thread_params(self, workdir: Path) -> dict[str, Any]:
        params: dict[str, Any] = {
            "dynamicTools": await self._dynamic_tools(),
            "cwd": str(workdir), "approvalPolicy": "untrusted", "approvalsReviewer": "user",
            "sandbox": "read-only", "config": self.config(),
            "baseInstructions": (self.instructions or "You are a helpful assistant.") + _BASE_GUIDANCE}
        if self.model:
            params["model"] = self.model
        return params

    def _rollout(self, thread_id: str) -> Path | None:
        sessions = self.settings.codex_home / "sessions"
        return next(sessions.rglob(f"*{thread_id}.jsonl"), None) if sessions.is_dir() else None

    async def _spawn(self, instance: str, *, ephemeral: bool) -> _Agent:
        if self._auth_mode() != "chatgpt":
            raise CodexHarnessError(
                f"codex harness: no ChatGPT subscription login in {self.settings.codex_home} "
                "(run `codex login --device-auth` with CODEX_HOME set to it); API keys are "
                "never used")
        workdir = self.settings.workdirs / instance
        workdir.mkdir(parents=True, exist_ok=True)
        binary = self.settings.codex_bin or bundled_codex() or shutil.which("codex") or "codex"
        async def on_request(method: str, params: dict) -> dict:
            return await self._on_request(instance, method, params)

        server = AppServer([binary, "app-server"], env=self._env(instance), cwd=str(workdir),
                           on_request=on_request)
        try:
            await server.start()
            thread_id = await self._open_thread(server, instance, workdir, ephemeral=ephemeral)
        except BaseException:
            await server.close()
            raise
        return _Agent(instance=instance, server=server, thread_id=thread_id, ephemeral=ephemeral)

    async def _open_thread(self, server: AppServer, instance: str, workdir: Path, *,
                           ephemeral: bool) -> str:
        params = await self._thread_params(workdir)
        current = {t["name"]: t for t in params["dynamicTools"]}
        entry = {} if ephemeral else (self._load_state().get(instance) or {})
        known = entry.get("thread_id")
        declared: dict[str, dict] | None = entry.get("tools")
        carry: list[dict] = []
        if known and declared is not None and set(current) <= set(declared):
            # Codex fixes a thread's dynamic tools at thread/start (resume and
            # fork ignore new ones). Same or fewer tools: resume, keeping the
            # declared set — a tool whose upstream is down right now stays
            # declared and fails at call time rather than forcing a new thread.
            try:
                result = await server.request("thread/resume", {
                    "threadId": known, **params, "dynamicTools": list(declared.values()),
                    "excludeTurns": True})
                return result["thread"]["id"]
            except CodexHarnessError as exc:
                logger.warning("codex harness: thread %s of %s could not resume (%s); starting "
                               "a new one", known, instance, exc)
                await self._update_instance(instance, thread_id=None, lost=True)
                known = None
        elif known:
            # New tools: a new thread declares them, and the old thread's
            # history (since its last compaction) is carried into it.
            carry = self._carry_items(known)
            logger.info("codex harness: %s gained tools %s; moving to a new thread with %d "
                        "carried item(s)", instance, sorted(set(current) - set(declared or {})),
                        len(carry))
            current = {**(declared or {}), **current}
            params["dynamicTools"] = list(current.values())
        result = await server.request("thread/start", {**params, "ephemeral": ephemeral})
        thread_id = result["thread"]["id"]
        lost = False
        if carry:
            try:
                await server.request("thread/inject_items", {"threadId": thread_id, "items": carry})
            except CodexHarnessError:
                # An item the API won't take back (e.g. a compaction summary):
                # retry with plain messages, then give up on the carry.
                plain = [i for i in carry if i.get("type") == "message"]
                try:
                    await server.request("thread/inject_items", {"threadId": thread_id,
                                                                 "items": plain})
                except CodexHarnessError as exc:
                    logger.warning("codex harness: could not carry %s's history (%s)", instance, exc)
                    lost = True
        elif known and not entry.get("lost"):
            lost = True  # a new thread without the old one's history
        if not ephemeral:
            entry = self._load_state().get(instance) or {}
            fields: dict[str, Any] = {"lost": True} if lost else {}
            await self._update_instance(
                instance, thread_id=thread_id, threads=[*(entry.get("threads") or []), thread_id],
                tools=current, seq=int(entry.get("seq") or 0) or self._first_seq(instance),
                session_started_at=time.time(), **fields)
        return thread_id

    def _carry_items(self, thread_id: str) -> list[dict]:
        """The model-visible history of a thread since its last compaction,
        as raw Responses items: the compaction's replacement history (with
        its summary), then the user/assistant messages after it."""
        path = self._rollout(thread_id)
        if path is None:
            return []
        items: list[dict] = []

        def clean(item: dict) -> dict | None:
            kind = item.get("type")
            if kind == "message" and item.get("role") in ("user", "assistant"):
                text = "".join(c.get("text", "") for c in item.get("content") or []
                               if isinstance(c, dict))
                if text.startswith("<environment_context"):
                    return None
                return {"type": "message", "role": item["role"], "content": item["content"]}
            if kind == "compaction" and item.get("encrypted_content"):
                return {"type": "compaction", "encrypted_content": item["encrypted_content"]}
            return None

        for line in path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            payload = entry.get("payload") or {}
            if entry.get("type") == "compacted":
                items = [c for c in map(clean, payload.get("replacement_history") or []) if c]
                if not items and payload.get("message"):
                    items = [{"type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": payload["message"]}]}]
            elif entry.get("type") == "response_item":
                cleaned = clean(payload)
                if cleaned:
                    items.append(cleaned)
        return items[-_CARRY_MAX_ITEMS:]

    def _first_seq(self, instance: str) -> int:
        prev = self.served.get(instance) if self.served is not None else None
        return int(prev.get("seq") or 0) + 1 if prev else 1

    async def _agent_for(self, instance: str, *, ephemeral: bool) -> _Agent:
        async with self._spawn_lock:
            agent = self._agents.get(instance)
            if agent is not None and agent.server.alive:
                agent.reserved += 1
                return agent
            if agent is not None:
                await self._drop(instance)
            await self._evict_for_capacity()
            agent = await self._spawn(instance, ephemeral=ephemeral)
            agent.reserved += 1
            self._agents[instance] = agent
            self._ensure_reaper()
            return agent

    async def _evict_for_capacity(self) -> None:
        idle = sorted((a for a in self._agents.values() if not a.busy), key=lambda a: a.last_used)
        while len(self._agents) >= self.settings.max_processes and idle:
            await self._drop(idle.pop(0).instance)

    async def _drop(self, instance: str) -> None:
        agent = self._agents.pop(instance, None)
        if agent is not None:
            await agent.server.close()

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_idle(), name="codex-harness-reaper")

    async def _reap_idle(self) -> None:
        while self._agents:
            await asyncio.sleep(min(60.0, max(1.0, self.settings.idle_s / 4)))
            now = time.monotonic()
            for agent in list(self._agents.values()):
                if not agent.busy and now - agent.last_used > self.settings.idle_s:
                    async with self._spawn_lock:
                        await self._drop(agent.instance)

    # ── turns ────────────────────────────────────────────────────────────
    def _key(self, turn: HarnessTurn) -> tuple[str, bool]:
        if turn.use_history and turn.instance:
            return turn.instance, False
        return f"run-{turn.run_id or os.urandom(6).hex()}", True

    @staticmethod
    def _compose(turn: HarnessTurn) -> str:
        if turn.extra_instructions:
            return (f"<context source=\"miragen\">\n{turn.extra_instructions}\n</context>\n\n"
                    f"{turn.prompt}")
        return turn.prompt

    async def _turn(self, turn: HarnessTurn, on_text: Callable[[str], None] | None) -> HarnessResult:
        if turn.secret_env:
            raise CodexHarnessError("per-launch MCP credentials are not supported by the codex "
                                    "harness (the gateway holds deployment-level credentials)")
        instance, ephemeral = self._key(turn)
        agent = await self._agent_for(instance, ephemeral=ephemeral)
        final: list[str] = []
        other: list[str] = []
        usage: dict = {}
        context: int | None = None
        compacted = 0
        error: str | None = None
        status = None
        try:
            async with agent.lock:
                agent.last_used = time.monotonic()
                queue = agent.server.subscribe(agent.thread_id)
                with self.gateway.turn(instance, turn.run_id) as log:
                    turn_id = None
                    try:
                        async with asyncio.timeout(self.settings.turn_timeout_s):
                            params: dict[str, Any] = {
                                "threadId": agent.thread_id,
                                "input": [{"type": "text", "text": self._compose(turn)}]}
                            if self.settings.effort:
                                params["effort"] = self.settings.effort
                            started = await agent.server.request("turn/start", params)
                            turn_id = (started or {}).get("turn", {}).get("id")
                            while True:
                                msg = await queue.get()
                                method, p = msg.get("method"), msg.get("params") or {}
                                if method == "_exited":
                                    raise CodexHarnessError(
                                        "codex app-server exited mid-turn; stderr: "
                                        + " | ".join(agent.server.stderr[-5:]))
                                if p.get("turnId") not in (None, turn_id) and method != "turn/completed":
                                    continue
                                if method == "item/completed":
                                    item = p.get("item") or {}
                                    if item.get("type") == "agentMessage" and item.get("text"):
                                        text = item["text"]
                                        if item.get("phase") == "final_answer":
                                            final.append(text)
                                            if on_text:
                                                on_text(text)
                                        else:
                                            other.append(text)
                                elif method == "thread/tokenUsage/updated":
                                    tu = p.get("tokenUsage") or {}
                                    usage = tu.get("total") or usage
                                    last = tu.get("last") or {}
                                    if isinstance(last.get("inputTokens"), int):
                                        context = last["inputTokens"]
                                elif method == "thread/compacted":
                                    compacted += 1
                                    if self.on_lifecycle is not None:
                                        with contextlib.suppress(Exception):
                                            await self.on_lifecycle(instance, "compacting",
                                                                    {"trigger": "codex"})
                                elif method == "error":
                                    error = json.dumps(p.get("error") or p)[:500]
                                elif method == "turn/completed":
                                    t = p.get("turn") or {}
                                    if t.get("id") not in (None, turn_id):
                                        continue
                                    status = t.get("status")
                                    if t.get("error"):
                                        error = json.dumps(t["error"])[:500]
                                    break
                    except TimeoutError:
                        if turn_id:
                            with contextlib.suppress(Exception):
                                await agent.server.request(
                                    "turn/interrupt", {"threadId": agent.thread_id,
                                                       "turnId": turn_id}, timeout=10)
                        await agent.server.close()
                        raise CodexHarnessError(
                            f"turn exceeded {self.settings.turn_timeout_s:.0f}s; cancelled") from None
                    finally:
                        agent.server.unsubscribe(agent.thread_id)
                    calls = list(log.calls)
                agent.last_used = time.monotonic()
        finally:
            agent.reserved -= 1
            if ephemeral or not agent.server.alive:
                async with self._spawn_lock:
                    if self._agents.get(instance) is agent:
                        await self._drop(instance)
            if ephemeral:
                path = self._rollout(agent.thread_id)
                if path is not None:
                    path.unlink(missing_ok=True)
                shutil.rmtree(self.settings.workdirs / instance, ignore_errors=True)
        if not ephemeral and status == "completed":
            await self._after_turn(instance, context, compacted)
        if status != "completed":
            raise CodexHarnessError(f"codex turn {status or 'ended'}: {error or 'no detail'}")
        output = "\n\n".join(final) if final else "\n\n".join(other)
        if not final and other and on_text:
            on_text(output)
        return HarnessResult(output=output, usage=_usage(usage), tool_calls=calls)

    async def _after_turn(self, instance: str, context: int | None, compacted: int) -> None:
        entry = self._load_state().get(instance) or {}
        seq = int(entry.get("seq") or 1)
        if self.served is not None and self.served.switched(instance, NAME):
            prev = self.served.get(instance) or {}
            seq = max(seq, int(prev.get("seq") or 0) + 1)
        fields: dict[str, Any] = {}
        if context is not None:
            fields["context_tokens"] = context
        await self._update_instance(
            instance, seq=seq, turns=int(entry.get("turns") or 0) + 1, lost=False, rotated=False,
            compactions=int(entry.get("compactions") or 0) + compacted, **fields)
        if self.served is not None:
            self.served.mark(instance, NAME, seq)

    async def run(self, turn: HarnessTurn) -> HarnessResult:
        return await self._turn(turn, None)

    @contextlib.asynccontextmanager
    async def stream(self, turn: HarnessTurn) -> AsyncIterator[TaskStream]:
        stream = TaskStream()
        task = asyncio.create_task(self._turn(turn, stream.push))
        stream.attach(task)
        try:
            yield stream
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    # ── sessions ─────────────────────────────────────────────────────────
    def session_info(self, instance: str) -> dict | None:
        entry = self._load_state().get(instance)
        switched = self.served is not None and self.served.switched(instance, NAME)
        if not entry and not switched:
            return None
        entry = entry or {}
        return {"seq": int(entry.get("seq") or self._first_seq(instance)),
                "turns": int(entry.get("turns") or 0),
                "compactions": int(entry.get("compactions") or 0),
                "context_tokens": int(entry.get("context_tokens") or 0),
                "rotation_pending": False,
                "fresh": (not entry.get("thread_id") or bool(entry.get("rotated"))
                          or bool(entry.get("lost")) or switched),
                "maintenance_running": False,
                "harness": NAME}

    async def rotate(self, instance: str, reason: str = "requested") -> dict:
        if not self._load_state().get(instance):
            raise KeyError(instance)
        async with self._spawn_lock:
            agent = self._agents.get(instance)
            if agent is not None and agent.busy:
                raise InstanceBusyError(f"instance '{instance}' has a running turn")
            await self._drop(instance)
        if self.on_lifecycle is not None:
            with contextlib.suppress(Exception):
                await self.on_lifecycle(instance, "closed", {"reason": reason})
        entry = self._load_state().get(instance) or {}
        await self._update_instance(instance, thread_id=None, rotated=True, turns=0,
                                    compactions=0, context_tokens=0,
                                    seq=int(entry.get("seq") or 1) + 1)
        return self.session_info(instance) or {}

    async def forget(self, instance: str) -> list[str]:
        removed: list[str] = []
        async with self._spawn_lock:
            agent = self._agents.get(instance)
            if agent is not None and agent.busy:
                raise InstanceBusyError(f"instance '{instance}' has a running turn")
            if agent is not None:
                await self._drop(instance)
                removed.append("process")
            async with self._state_lock:
                state = self._load_state()
                entry = state.pop(instance, None)
                if entry is not None:
                    self._write_state(state)
                    removed.append("session_mapping")
            if self.served is not None:
                self.served.forget(instance)
            for tid in (entry or {}).get("threads") or []:
                path = self._rollout(tid)
                if path is not None:
                    path.unlink()
                    removed.append(f"codex_thread:{tid}")
            workdir = self.settings.workdirs / instance
            if workdir.is_dir():
                shutil.rmtree(workdir)
                removed.append("workdir")
        return removed

    async def aclose(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
        for instance in list(self._agents):
            await self._drop(instance)

    def status(self) -> dict[str, Any]:
        return {"processes": sorted(self._agents), "max_processes": self.settings.max_processes}


def _error_result(message: str):
    from mcp import types

    return types.CallToolResult(content=[types.TextContent(type="text", text=message)],
                                isError=True)


def _tool_output(result: Any) -> dict:
    """A gateway CallToolResult as a dynamic tool call response."""
    items: list[dict] = []
    for block in result.content or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            items.append({"type": "inputText", "text": block.text})
        elif kind == "image":
            items.append({"type": "inputImage",
                          "imageUrl": f"data:{block.mimeType};base64,{block.data}"})
        else:
            items.append({"type": "inputText",
                          "text": json.dumps(block.model_dump(mode="json"), default=str)})
    if not items and result.structuredContent is not None:
        items.append({"type": "inputText", "text": json.dumps(result.structuredContent)})
    return {"contentItems": items or [{"type": "inputText", "text": ""}],
            "success": not result.isError}


def _usage(raw: dict[str, Any]) -> RunUsage:
    def pick(key):
        return raw.get(key) if isinstance(raw.get(key), int) else None
    return RunUsage(requests=1, input_tokens=pick("inputTokens"),
                    output_tokens=pick("outputTokens"),
                    cached_input_tokens=pick("cachedInputTokens"))
