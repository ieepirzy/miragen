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
from miragen.harness.grok_lifecycle import (
    Decision, Handoff, Ledger, LifecyclePolicy, SessionStats, ThresholdPolicy,
    accepted_memory_writes, load_policy, memory_save_prompt, rotation_prompt,
)
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
# Stays enabled whatever the config says (codex 0.159), but has no tool of its
# own once shell_tool is off (verified live: no shell in any probe).
_FORCED_ON = frozenset({"unified_exec"})
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
    # Codex's own auto-compaction (config model_auto_compact_token_limit):
    # with the lifecycle on, a safety net ABOVE the policy's thresholds (it
    # compacts mid-turn, with no memory save first). None leaves the
    # model's default.
    auto_compact_tokens: int | None = None
    # Session lifecycle (grok_lifecycle.py, shared with the Grok harness):
    # a memory-save turn before each compaction, a handoff note on rotation.
    lifecycle: bool = True
    policy: LifecyclePolicy = field(default_factory=ThresholdPolicy)
    handoff_max_chars: int = 4000

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
            lifecycle=env.get("MIRAGEN_CODEX_LIFECYCLE", "on").lower() not in ("off", "0", "false"),
            policy=load_policy(dict(env), prefix="MIRAGEN_CODEX_"),
            handoff_max_chars=int(env.get("MIRAGEN_CODEX_HANDOFF_MAX_CHARS", "4000")),
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
        return (self.proc is not None and self.proc.returncode is None
                and bool(self._tasks) and not self._tasks[0].done())

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
        try:
            async with asyncio.timeout(timeout):
                return await fut
        finally:
            self._pending.pop(rid, None)

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
    active_turn: str | None = None

    @property
    def busy(self) -> bool:
        return self.reserved > 0 or self.lock.locked()


@dataclass
class _Outcome:
    """What one codex turn produced."""

    final: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)
    compacted: int = 0
    context: int | None = None
    error: str | None = None
    status: str | None = None
    requests: int = 0
    # tokenUsage.total is the THREAD's running total; this turn's usage is
    # the final total minus the total before the turn (the first update's
    # total less that call's own `last`). Codex can repeat an update, so
    # totals, not a sum of `last`, are the source of truth.
    before: dict | None = None
    total: dict = field(default_factory=dict)

    def usage_update(self, tu: dict) -> None:
        last = tu.get("last") or {}
        new_total = tu.get("total") or {}
        if self.before is None and new_total:
            self.before = {k: v - int(last.get(k) or 0) for k, v in new_total.items()
                           if isinstance(v, int)}
        if new_total and new_total != self.total:
            self.requests += 1
            self.total = new_total
        if isinstance(last.get("inputTokens"), int):
            self.context = last["inputTokens"]

    def turn_usage(self) -> dict:
        return {k: v - (self.before or {}).get(k, 0) for k, v in self.total.items()
                if isinstance(v, int)}

    @property
    def text(self) -> str:
        return "\n\n".join(self.final or self.other).strip()


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
        self._maintenance: dict[str, asyncio.Task] = {}
        self.ledger = Ledger(settings.codex_home / "lifecycle")
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
            agent = self._agents.get(instance)
            active = agent.active_turn if agent is not None else None
            if active is None or params.get("turnId") != active:
                # A call from a turn nobody is waiting on any more (cancelled
                # or timed out): it must not run under the next turn.
                logger.warning("codex harness: refused a tool call from a stale turn")
                return _tool_output(_error_result("this turn is no longer active"))
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

    async def _verify_features(self, server: AppServer) -> None:
        """Fail closed unless every flag the boundary relies on exists and is
        off in this codex: a renamed flag would silently bring a tool (the
        shell) back, and Codex only logs an unknown setting as ignored."""
        listed = {f.get("name"): f for f in
                  ((await server.request("experimentalFeature/list", {})) or {}).get("data") or []}
        missing = [f for f in FEATURES_OFF if f not in listed]
        still_on = [f for f in FEATURES_OFF
                    if f in listed and listed[f].get("enabled") and f not in _FORCED_ON]
        if missing or still_on:
            raise CodexHarnessError(
                f"codex harness: this codex doesn't honour the tool boundary (unknown flags "
                f"{missing}, still enabled {still_on}); refusing to run")

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
            await self._verify_features(server)
            thread_id = await self._open_thread(server, instance, workdir, ephemeral=ephemeral)
        except BaseException:
            await server.close()
            if ephemeral:
                shutil.rmtree(workdir, ignore_errors=True)
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

    def _seq(self, instance: str, entry: dict) -> int:
        """The plane session number now (the same before and after a turn)."""
        own = int(entry.get("seq") or 0) or self._first_seq(instance)
        return self.served.seq_for(instance, NAME, own) if self.served is not None else own

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
                        # Re-check under the lock: a turn may have reserved it meanwhile.
                        if self._agents.get(agent.instance) is agent and not agent.busy:
                            await self._drop(agent.instance)

    # ── turns ────────────────────────────────────────────────────────────
    def _key(self, turn: HarnessTurn) -> tuple[str, bool]:
        if turn.use_history and turn.instance:
            return turn.instance, False
        return f"run-{turn.run_id or os.urandom(6).hex()}", True

    @staticmethod
    def _compose(turn: HarnessTurn, *, handoff: Handoff | None = None) -> str:
        parts = []
        if handoff is not None:
            parts.append(handoff.render())
        if turn.extra_instructions:
            parts.append(f"<context source=\"miragen\">\n{turn.extra_instructions}\n</context>")
        parts.append(turn.prompt)
        return "\n\n".join(parts)

    async def _drive(self, agent: _Agent, instance: str, method: str, params: dict,
                     on_text: Callable[[str], None] | None = None) -> "_Outcome":
        """Run one codex turn (``turn/start``, or ``thread/compact/start`` for a
        manual compaction) on the agent's thread until it completes. The
        caller holds the agent lock and the gateway turn."""
        out = _Outcome()
        queue = agent.server.subscribe(agent.thread_id)
        turn_id = None
        try:
            async with asyncio.timeout(self.settings.turn_timeout_s):
                started = await agent.server.request(method, params)
                turn_id = ((started or {}).get("turn") or {}).get("id")
                agent.active_turn = turn_id
                while True:
                    msg = await queue.get()
                    kind, p = msg.get("method"), msg.get("params") or {}
                    if kind == "_exited":
                        raise CodexHarnessError(
                            "codex app-server exited mid-turn; stderr: "
                            + " | ".join(agent.server.stderr[-5:]))
                    if turn_id is None and kind == "turn/started":
                        # thread/compact/start answers before its turn exists.
                        turn_id = ((p.get("turn") or {}).get("id")) or p.get("turnId")
                        agent.active_turn = turn_id
                        continue
                    if p.get("turnId") not in (None, turn_id) and kind != "turn/completed":
                        continue
                    if kind == "thread/compacted" or (
                            kind == "item/completed"
                            and (p.get("item") or {}).get("type") == "contextCompaction"):
                        # Auto-compaction arrives as a contextCompaction item
                        # (observed); thread/compacted is the manual one.
                        out.compacted += 1
                    elif kind == "item/completed":
                        item = p.get("item") or {}
                        if item.get("type") == "agentMessage" and item.get("text"):
                            if item.get("phase") == "final_answer":
                                out.final.append(item["text"])
                                if on_text:
                                    on_text(item["text"])
                            else:
                                out.other.append(item["text"])
                    elif kind == "thread/tokenUsage/updated":
                        out.usage_update(p.get("tokenUsage") or {})
                    elif kind == "error":
                        out.error = json.dumps(p.get("error") or p)[:500]
                    elif kind == "turn/completed":
                        t = p.get("turn") or {}
                        if turn_id is not None and t.get("id") not in (None, turn_id):
                            continue
                        out.status = t.get("status")
                        if t.get("error"):
                            out.error = json.dumps(t["error"])[:500]
                        break
        except TimeoutError:
            if turn_id:
                with contextlib.suppress(Exception):
                    await agent.server.request("turn/interrupt", {
                        "threadId": agent.thread_id, "turnId": turn_id}, timeout=10)
            await agent.server.close()
            raise CodexHarnessError(
                f"turn exceeded {self.settings.turn_timeout_s:.0f}s; cancelled") from None
        except asyncio.CancelledError:
            # The caller went away: stop the turn in codex too, so it doesn't
            # keep running (and calling tools) unattended.
            if turn_id:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(agent.server.request("turn/interrupt", {
                        "threadId": agent.thread_id, "turnId": turn_id}, timeout=10))
            with contextlib.suppress(BaseException):
                await asyncio.shield(agent.server.close())
            raise
        finally:
            agent.active_turn = None
            agent.server.unsubscribe(agent.thread_id)
        return out

    def _turn_params(self, agent: _Agent, text: str) -> dict:
        params: dict[str, Any] = {"threadId": agent.thread_id,
                                  "input": [{"type": "text", "text": text}]}
        if self.settings.effort:
            params["effort"] = self.settings.effort
        return params

    async def _turn(self, turn: HarnessTurn, on_text: Callable[[str], None] | None) -> HarnessResult:
        if turn.secret_env:
            raise CodexHarnessError("per-launch MCP credentials are not supported by the codex "
                                    "harness (the gateway holds deployment-level credentials)")
        instance, ephemeral = self._key(turn)
        # A compaction or rotation still running for this instance finishes
        # first: the turn must land in the thread it leaves behind.
        pending = None if ephemeral else self._maintenance.get(instance)
        if pending is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(pending)
        # A `lost` the client could see before this turn is now consumed; one
        # raised while starting this turn must survive it (next turn: fresh).
        lost_before = bool((self._load_state().get(instance) or {}).get("lost"))
        handoff = None if ephemeral else self._pending_handoff(instance)
        agent = await self._agent_for(instance, ephemeral=ephemeral)
        try:
            async with agent.lock:
                agent.last_used = time.monotonic()
                with self.gateway.turn(instance, turn.run_id) as log:
                    out = await self._drive(agent, instance, "turn/start", self._turn_params(
                        agent, self._compose(turn, handoff=handoff)), on_text)
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
        if not ephemeral and out.status == "completed":
            if out.compacted and self.on_lifecycle is not None:
                with contextlib.suppress(Exception):
                    await self.on_lifecycle(instance, "compacting", {"trigger": "codex"})
            await self._after_turn(instance, out.context, out.compacted, calls,
                                   lost_before=lost_before, handoff_delivered=handoff is not None)
        if out.status != "completed":
            raise CodexHarnessError(f"codex turn {out.status or 'ended'}: "
                                    f"{out.error or 'no detail'}")
        output = "\n\n".join(out.final) if out.final else "\n\n".join(out.other)
        if not out.final and out.other and on_text:
            on_text(output)
        return HarnessResult(output=output, usage=_usage(out.turn_usage(), requests=out.requests),
                             tool_calls=calls)

    async def _after_turn(self, instance: str, context: int | None, compacted: int,
                          calls: list, *, lost_before: bool = False,
                          handoff_delivered: bool = False) -> None:
        entry = self._load_state().get(instance) or {}
        seq = self._seq(instance, entry)
        writes = accepted_memory_writes(calls)
        fields: dict[str, Any] = {
            "turns": int(entry.get("turns") or 0) + 1,
            "compactions": int(entry.get("compactions") or 0) + compacted,
            "memory_writes": int(entry.get("memory_writes") or 0) + writes}
        if writes:
            fields["last_memory_write_at"] = time.time()
        if context is not None:
            fields["context_tokens"] = context
        if lost_before or not entry.get("lost"):
            fields["lost"] = False
        if handoff_delivered:
            fields["handoff"] = None
        entry = await self._update_instance(instance, seq=seq, rotated=False, **fields)
        if self.served is not None:
            self.served.mark(instance, NAME, seq)
        stats = self._stats(entry)
        self.ledger.write(instance, "turn", seq=stats.seq, context_tokens=stats.context_tokens,
                          turns=stats.turns, compactions=stats.compactions, memory_writes=writes)
        if compacted:
            # codex compacted on its own (its safety net): no memory save first.
            self.ledger.write(instance, "compaction", trigger="codex", seq=stats.seq)
        if not self.settings.lifecycle:
            return
        decision = self.settings.policy.decide(stats)
        if decision.action == "none":
            return
        if decision.action == "rotate":
            # Visible at once: the next turn lands in a new thread.
            await self._update_instance(instance, rotation_pending=True)
        self._schedule(instance, decision)

    # ── session lifecycle (compaction, rotation, handoff) ────────────────
    def _stats(self, entry: dict) -> SessionStats:
        return SessionStats(
            context_tokens=int(entry.get("context_tokens") or 0),
            compactions=int(entry.get("compactions") or 0),
            turns=int(entry.get("turns") or 0),
            age_s=time.time() - float(entry.get("session_started_at") or time.time()),
            seq=int(entry.get("seq") or 1))

    def _pending_handoff(self, instance: str) -> Handoff | None:
        raw = (self._load_state().get(instance) or {}).get("handoff")
        return Handoff(**raw) if raw else None

    def _schedule(self, instance: str, decision: Decision) -> asyncio.Task:
        task = asyncio.create_task(self._maintain(instance, decision),
                                   name=f"codex-maintenance-{instance}")
        self._maintenance[instance] = task
        task.add_done_callback(lambda t: self._maintenance.pop(instance, None)
                               if self._maintenance.get(instance) is t else None)
        return task

    async def _maintain(self, instance: str, decision: Decision) -> bool:
        """Run a compaction or rotation after the user's turn returned; the
        next turn waits for it. Returns whether it ran."""
        try:
            agent = await self._agent_for(instance, ephemeral=False)
        except Exception as exc:  # noqa: BLE001 — retried by the next turn's policy
            logger.warning("codex harness: maintenance of %s could not start: %s", instance, exc)
            await self._update_instance(instance, rotation_pending=False)
            return False
        run_id = f"maintenance-{os.urandom(6).hex()}"
        try:
            async with agent.lock:
                agent.last_used = time.monotonic()
                if decision.action == "compact":
                    await self._compact(agent, run_id, decision)
                else:
                    await self._rotate(agent, run_id, decision)
                agent.last_used = time.monotonic()
            return True
        except Exception as exc:  # noqa: BLE001 — logged + ledgered; the thread stays usable
            logger.warning("codex harness: %s of %s failed: %s", decision.action, instance, exc)
            self.ledger.write(instance, "maintenance_failed", action=decision.action,
                              reason=decision.reason, error=str(exc)[:300])
            await self._update_instance(instance, rotation_pending=False)
            return False
        finally:
            agent.reserved -= 1

    async def _compact(self, agent: _Agent, run_id: str, decision: Decision) -> None:
        instance = agent.instance
        entry = self._load_state().get(instance) or {}
        before = int(entry.get("context_tokens") or 0)
        with self.gateway.turn(instance, run_id) as log:
            await self._drive(agent, instance, "turn/start",
                              self._turn_params(agent, memory_save_prompt(decision.reason)))
            writes = accepted_memory_writes(log.calls)
        if self.on_lifecycle is not None:
            with contextlib.suppress(Exception):
                await self.on_lifecycle(instance, "compacting", {"trigger": "miragen"})
        with self.gateway.turn(instance, run_id):
            out = await self._drive(agent, instance, "thread/compact/start",
                                    {"threadId": agent.thread_id})
        if out.status not in (None, "completed"):
            raise CodexHarnessError(f"compaction {out.status}: {out.error or 'no detail'}")
        fields: dict[str, Any] = {
            "compactions": int(entry.get("compactions") or 0) + 1,
            "memory_writes": int(entry.get("memory_writes") or 0) + writes}
        if writes:
            fields["last_memory_write_at"] = time.time()
        if out.context is not None:
            fields["context_tokens"] = out.context
        await self._update_instance(instance, **fields)
        self.ledger.write(instance, "compaction", trigger="miragen", reason=decision.reason,
                          seq=entry.get("seq", 1), memory_writes=writes, tokens_before=before,
                          tokens_after=out.context, observed=bool(out.compacted))

    async def _rotate(self, agent: _Agent, run_id: str, decision: Decision) -> None:
        instance = agent.instance
        entry = self._load_state().get(instance) or {}
        limit = self.settings.handoff_max_chars
        text, error = "", None
        with self.gateway.turn(instance, run_id) as log:
            try:
                out = await self._drive(agent, instance, "turn/start",
                                        self._turn_params(agent, rotation_prompt(decision.reason,
                                                                                 limit)))
                text = out.text
                if out.status != "completed":
                    error = f"turn {out.status}: {out.error or 'no detail'}"[:200]
            except Exception as exc:  # noqa: BLE001 — best-effort: rotate anyway, say the note is missing
                error = f"{type(exc).__name__}: {exc}"[:200]
            writes = accepted_memory_writes(log.calls)
        rotated_at = time.time()
        seq = int(entry.get("seq") or 1)
        handoff = Handoff(
            text=text[:limit], truncated=len(text) > limit, prev_seq=seq, reason=decision.reason,
            started_at=float(entry.get("session_started_at") or rotated_at), rotated_at=rotated_at,
            turns=int(entry.get("turns") or 0), compactions=int(entry.get("compactions") or 0),
            context_tokens=int(entry.get("context_tokens") or 0),
            memory_writes=int(entry.get("memory_writes") or 0) + writes,
            last_memory_write_at=(rotated_at if writes else entry.get("last_memory_write_at")),
            handoff_error=error or (None if text else "the note came back empty"))
        if self.on_lifecycle is not None:
            with contextlib.suppress(Exception):
                await self.on_lifecycle(instance, "closed", {"reason": decision.reason})
        # A new thread with the same declared tools; the old one's rollout stays
        # (listed in `threads`, removed by forget).
        old = agent.thread_id
        params = await self._thread_params(self.settings.workdirs / instance)
        declared = entry.get("tools")
        if declared:
            params["dynamicTools"] = list(declared.values())
        result = await agent.server.request("thread/start", {**params, "ephemeral": False})
        agent.thread_id = result["thread"]["id"]
        await self._update_instance(
            instance, thread_id=agent.thread_id,
            threads=[*(entry.get("threads") or []), agent.thread_id],
            tools=declared or {t["name"]: t for t in params["dynamicTools"]},
            seq=seq + 1, session_started_at=rotated_at, turns=0, compactions=0,
            context_tokens=0, memory_writes=0, last_memory_write_at=None,
            rotation_pending=False, rotated=False, handoff=handoff.to_json())
        self.ledger.write(instance, "rotation", reason=decision.reason, from_seq=seq,
                          to_seq=seq + 1, from_thread=old, to_thread=agent.thread_id,
                          memory_writes=writes, handoff_chars=len(text),
                          handoff_truncated=handoff.truncated, handoff_error=handoff.handoff_error)

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
        return {"seq": self._seq(instance, entry),
                "turns": int(entry.get("turns") or 0),
                "compactions": int(entry.get("compactions") or 0),
                "context_tokens": int(entry.get("context_tokens") or 0),
                "rotation_pending": bool(entry.get("rotation_pending")),
                # The next turn doesn't continue the thread the client saw
                # last: new, rotated (with or without a handoff), lost, or
                # served by another harness.
                "fresh": (not entry.get("thread_id") or bool(entry.get("rotated"))
                          or bool(entry.get("lost")) or switched
                          or bool(entry.get("rotation_pending")) or bool(entry.get("handoff"))),
                "maintenance_running": instance in self._maintenance,
                "harness": NAME}

    async def rotate(self, instance: str, reason: str = "requested") -> dict:
        """Start the instance's next thread now (Mira's /clear): memory save +
        handoff note, then a new thread, as an automatic rotation. If the
        maintenance can't run (e.g. no login), the thread is still dropped."""
        entry = self._load_state().get(instance)
        if not entry:
            raise KeyError(instance)
        pending = self._maintenance.get(instance)
        if pending is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(pending)
        agent = self._agents.get(instance)
        if agent is not None and agent.busy:
            raise InstanceBusyError(f"instance '{instance}' has a running turn")
        ran = False
        if self.settings.lifecycle and entry.get("thread_id"):
            await self._update_instance(instance, rotation_pending=True)
            ran = await self._schedule(instance, Decision("rotate", reason))
        if not ran:
            async with self._spawn_lock:
                await self._drop(instance)
            if self.on_lifecycle is not None:
                with contextlib.suppress(Exception):
                    await self.on_lifecycle(instance, "closed", {"reason": reason})
            entry = self._load_state().get(instance) or {}
            await self._update_instance(instance, thread_id=None, rotated=True, turns=0,
                                        compactions=0, context_tokens=0, rotation_pending=False,
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
        for task in list(self._maintenance.values()):
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
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


def _usage(raw: dict[str, Any], *, requests: int = 1) -> RunUsage:
    def pick(key):
        return raw.get(key) if isinstance(raw.get(key), int) else None
    return RunUsage(requests=max(requests, 1), input_tokens=pick("inputTokens"),
                    output_tokens=pick("outputTokens"),
                    cached_input_tokens=pick("cachedInputTokens"))
