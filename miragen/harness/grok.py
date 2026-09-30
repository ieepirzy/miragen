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
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from grok_build_client import AcpSession

from miragen.executor.grok_hermetic import hermetic_home_dir, write_hermetic_home
from miragen.harness.base import HarnessResult, HarnessTurn, InstanceBusyError, parse_harness_model
from miragen.harness.gateway import ToolGateway
from miragen.harness.grok_lifecycle import (
    Decision, Handoff, Ledger, LifecyclePolicy, SessionStats, ThresholdPolicy,
    accepted_memory_writes, load_policy, memory_save_prompt, rotation_prompt,
)
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
    # Session lifecycle (grok_lifecycle.py): compaction, rotation, handoff.
    lifecycle: bool = True
    policy: LifecyclePolicy = field(default_factory=ThresholdPolicy)
    handoff_max_chars: int = 4000
    session_retention_days: float = 30.0
    # grok's own auto-compaction, as a safety net above the policy (percent
    # of the model's window; grok-4.7: 500k). None leaves grok's default.
    native_compact_percent: int | None = 90

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
            lifecycle=env.get("MIRAGEN_GROK_LIFECYCLE", "on").lower() not in ("off", "0", "false"),
            policy=load_policy(dict(env)),
            handoff_max_chars=int(env.get("MIRAGEN_GROK_HANDOFF_MAX_CHARS", "4000")),
            session_retention_days=float(env.get("MIRAGEN_GROK_SESSION_RETENTION_DAYS", "30")),
            native_compact_percent=(int(env["MIRAGEN_GROK_NATIVE_COMPACT_PERCENT"])
                                    if env.get("MIRAGEN_GROK_NATIVE_COMPACT_PERCENT") else 90),
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


def _capability_names(profile: AgentProfile) -> list[tuple[str, dict]]:
    out = []
    for entry in profile.spec.capabilities or [] if profile.spec else []:
        if isinstance(entry, str):
            out.append((entry, {}))
        elif isinstance(entry, dict) and len(entry) == 1:
            name, cfg = next(iter(entry.items()))
            out.append((name, cfg or {}))
    return out


def instructions_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


_GATEWAY_TOOL = re.compile(rf"^{GATEWAY_SERVER}__[A-Za-z0-9_.-]+$")
# Fields that name the tool being called — never its (model-written) input.
_IDENTITY_FIELDS = ("toolName", "tool_name", "name", "title")
_DISPATCH_SERVER = ("server", "server_name", "serverName", "mcp_server")
_DISPATCH_TOOL = ("tool", "tool_name", "toolName", "name")


def _identity(params: dict[str, Any]) -> tuple[list[str], dict | None]:
    call = params.get("toolCall")
    if not isinstance(call, dict):
        return [], None
    names = [call.get(k) for k in _IDENTITY_FIELDS if isinstance(call.get(k), str)]
    meta = call.get("_meta") if isinstance(call.get("_meta"), dict) else {}
    tool_meta = meta.get("x.ai/tool") if isinstance(meta.get("x.ai/tool"), dict) else {}
    if isinstance(tool_meta.get("name"), str):
        names.append(tool_meta["name"])  # grok's own tool identity (seen live)
    raw = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else None
    return names, raw


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
    names, raw = _identity(params)
    if any(_GATEWAY_TOOL.fullmatch(n) for n in names):
        return True
    if any(n in DISPATCHERS for n in names) and raw is not None:
        server = next((raw[k] for k in _DISPATCH_SERVER if isinstance(raw.get(k), str)), None)
        tool = next((raw[k] for k in _DISPATCH_TOOL if isinstance(raw.get(k), str)), None)
        if server is not None:
            return server == GATEWAY_SERVER and bool(tool)
        return bool(tool and _GATEWAY_TOOL.fullmatch(tool))
    return False


def builtin_tool_call(params: dict[str, Any], enabled: frozenset[str]) -> str | None:
    """The enabled grok built-in this permission request is for, or None.
    Identity fields only, exact names."""
    names, _raw = _identity(params)
    return next((n for n in names if n in enabled), None)


# miragen capability name -> the grok built-in it enables. Everything else
# about these tools stays grok's (web_fetch's SSRF block on private,
# link-local, metadata and loopback addresses is on by default).
NATIVE_CAPABILITIES: dict[str, str] = {"WebSearch": "web_search", "WebFetch": "web_fetch"}


class GrokHarness:
    name = NAME
    native_capabilities = frozenset(NATIVE_CAPABILITIES)

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
        caps = {name for name, _ in _capability_names(profile)}
        self.builtins = frozenset(NATIVE_CAPABILITIES[c] for c in caps if c in NATIVE_CAPABILITIES)
        # System instructions = identity + stable profile-level guidance
        # (the voice renderer's 'Speaking aloud' section). Part of the
        # session's rules, so a change forks the session (decision 7).
        from miragen.voice import with_voice_guidance

        self.instructions = with_voice_guidance(profile.spec.instructions or "", system_guidance)
        self._session_factory = session_factory or AcpSession
        # Clock order (outer waits for inner): approval wait < grok's MCP
        # tool-call timeout < this harness's turn timeout. A gated call can
        # wait approval_timeout_s in the gateway; grok must not give up on
        # that call first, and the turn must not end under it.
        # (Without gated tools grok's own default applies, and the turn
        # timeout stays exactly as configured.)
        self.tool_timeout_s: int | None = (
            profile.approval_timeout_s + 120 if profile.approval_required else None)
        if self.tool_timeout_s and settings.turn_timeout_s < self.tool_timeout_s + 60:
            logger.warning("grok harness: raising the turn timeout from %.0fs to %ds so it "
                           "outlasts gated tool calls", settings.turn_timeout_s,
                           self.tool_timeout_s + 60)
            settings.turn_timeout_s = self.tool_timeout_s + 60
        self._agents: dict[str, _Agent] = {}
        self._maintenance: dict[str, asyncio.Task] = {}
        self.ledger = Ledger(settings.grok_home / "lifecycle")
        # Set by the app: session events for the memory plane
        # (instance, "compacting" | "closed", info).
        self.on_lifecycle: Callable[[str, str, dict], Awaitable[None]] | None = None
        self._spawn_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        self.prepare()

    # ── home, profile, state ─────────────────────────────────────────────
    def prepare(self) -> None:
        home = self.settings.grok_home
        spec = SimpleNamespace(
            web_search="web_search" in self.builtins, web_fetch="web_fetch" in self.builtins,
            auto_compact_percent=(self.settings.native_compact_percent
                                  if self.settings.lifecycle else None),
            mcp_servers=[SimpleNamespace(name=GATEWAY_SERVER, url=self.settings.gateway_url,
                                         bearer_token_env=GATEWAY_TOKEN_ENV,
                                         tool_timeout_sec=self.tool_timeout_s)])
        write_hermetic_home(home, self.profile.name, spec)
        # Built-in tools removed: the MCP dispatchers are all that's left.
        (home / PROFILE_FILE).write_text(
            "---\n"
            f"name: miragen-{self.profile.name}\n"
            "description: miragen base-tier agent; acts only through the miragen tool gateway\n"
            f"tools: {', '.join(['search_tool', 'use_tool', *sorted(self.builtins)])}\n"
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

    async def _save_instance(self, instance: str, session_id: str, *,
                             update_pending: bool = False) -> None:
        await self._update_instance(instance, session_id=session_id,
                                    instructions_sha=instructions_hash(self.instructions),
                                    instructions_update_pending=update_pending)

    async def _update_instance(self, instance: str, **fields: Any) -> dict:
        """Merge fields into the instance's state (lifecycle counters survive
        a session_id change)."""
        async with self._state_lock:
            state = self._load_state()
            entry = {**state.get(instance, {}), **fields, "updated_at": time.time()}
            state[instance] = entry
            tmp = self._state_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
            os.replace(tmp, self._state_path())
            return entry

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
        if is_gateway_tool_call(params) or builtin_tool_call(params, self.builtins):
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
            # Instructions changed. Grok fixes a session's system rules at
            # session/new — neither fork nor load accepts new ones (verified
            # live, grok 1.0.41). So: fork (history kept), and deliver the new
            # instructions once, in the first turn of the fork (decision 7).
            try:
                forked = await acp.request("_x.ai/session/fork", {
                    "sourceSessionId": known["session_id"], "sourceCwd": cwd, "newCwd": cwd,
                    "mcpServers": []})
                session_id = str((forked or {}).get("newSessionId") or "")
                if session_id:
                    await acp.session_load(session_id, cwd, mcp_servers=[])
                    await self._save_instance(instance, session_id, update_pending=True)
                    logger.info("grok harness: forked %s for new instructions", instance)
                    return session_id
            except Exception as exc:
                logger.warning("grok harness: fork of %s failed (%s); starting a new session",
                               instance, exc)
        session_id = await acp.session_new(cwd, mcp_servers=[], yolo=False, meta=rules)
        if not ephemeral:
            await self._save_instance(instance, session_id)
            if not (known or {}).get("session_started_at"):
                await self._update_instance(instance, seq=int((known or {}).get("seq") or 1),
                                            session_started_at=time.time(), turns=0,
                                            compactions=0, context_tokens=0, memory_writes=0)
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

    def _update_pending(self, instance: str, ephemeral: bool) -> bool:
        if ephemeral:
            return False
        return bool((self._load_state().get(instance) or {}).get("instructions_update_pending"))

    def _compose(self, turn: HarnessTurn, *, instructions_update: bool = False,
                 handoff: Handoff | None = None) -> str:
        parts = []
        if handoff is not None:
            parts.append(handoff.render())
        if instructions_update:
            parts.append(
                "<instructions-update source=\"miragen\">\nYour system instructions have "
                "changed. These replace the instructions you were given at the start of this "
                f"conversation, from now on:\n\n{self.instructions}\n</instructions-update>")
        if turn.extra_instructions:
            parts.append(f"<context source=\"miragen\">\n{turn.extra_instructions}\n</context>")
        parts.append(turn.prompt)
        return "\n\n".join(parts)

    async def _turn(self, turn: HarnessTurn, on_text: Callable[[str], None] | None) -> HarnessResult:
        if turn.secret_env:
            raise GrokHarnessError(
                "per-launch MCP credentials are not supported by the grok-build harness "
                "yet (the gateway holds deployment-level upstream credentials)")
        instance, ephemeral = self._key(turn)
        # A compaction or rotation still running for this instance finishes
        # first: the turn must land in the session it leaves behind.
        pending = self._maintenance.get(instance)
        if pending is not None and not ephemeral:
            with contextlib.suppress(Exception):
                await asyncio.shield(pending)
        agent = await self._agent_for(instance, ephemeral=ephemeral)
        deliver_update = self._update_pending(instance, ephemeral)
        handoff = None if ephemeral else self._pending_handoff(instance)
        chunks: list[str] = []
        end: dict[str, Any] = {}
        context_tokens: int | None = None
        native_compactions: list[dict] = []
        try:
            async with agent.lock:
                agent.last_used = time.monotonic()
                with self.gateway.turn(instance, turn.run_id) as log:
                    try:
                        async with asyncio.timeout(self.settings.turn_timeout_s):
                            after_tool = False
                            async for update in agent.acp.prompt(
                                    agent.session_id, self._compose(
                                        turn, instructions_update=deliver_update,
                                        handoff=handoff)):
                                kind = update.get("type")
                                if kind == "usage":
                                    context_tokens = update["context_tokens"]
                                elif kind == "compacted":
                                    native_compactions.append(update)
                                elif kind == "tool_call":
                                    after_tool = True
                                elif kind == "text":
                                    text = update["data"]
                                    # Grok streams each message segment around a
                                    # tool call separately; keep them apart.
                                    if after_tool and chunks and not chunks[-1].endswith("\n"):
                                        text = "\n\n" + text
                                    after_tool = False
                                    chunks.append(text)
                                    if on_text:
                                        on_text(text)
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
        if deliver_update and stop not in ("cancelled", "refusal"):
            await self._save_instance(instance, agent.session_id)  # delivered: clear pending
        if not ephemeral and self.settings.lifecycle and stop not in ("cancelled", "refusal"):
            await self._after_turn(instance, context_tokens, native_compactions, calls,
                                   handoff_delivered=handoff is not None)
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

    # ── session lifecycle ────────────────────────────────────────────────
    def _pending_handoff(self, instance: str) -> Handoff | None:
        raw = (self._load_state().get(instance) or {}).get("handoff")
        return Handoff(**raw) if raw else None

    def _stats(self, entry: dict) -> SessionStats:
        return SessionStats(
            context_tokens=int(entry.get("context_tokens") or 0),
            compactions=int(entry.get("compactions") or 0),
            turns=int(entry.get("turns") or 0),
            age_s=time.time() - float(entry.get("session_started_at") or time.time()),
            seq=int(entry.get("seq") or 1))

    async def _after_turn(self, instance: str, context_tokens: int | None,
                          native: list[dict], calls: list, *, handoff_delivered: bool) -> None:
        entry = self._load_state().get(instance) or {}
        writes = accepted_memory_writes(calls)
        fields: dict[str, Any] = {
            "turns": int(entry.get("turns") or 0) + 1,
            "compactions": int(entry.get("compactions") or 0) + len(native),
            "memory_writes": int(entry.get("memory_writes") or 0) + writes,
        }
        if writes:
            fields["last_memory_write_at"] = time.time()
        if context_tokens is not None:
            fields["context_tokens"] = context_tokens
        if handoff_delivered:
            fields["handoff"] = None
        for c in native:
            # grok compacted on its own (its safety net): no memory save ran first.
            self.ledger.write(instance, "compaction", trigger="grok", seq=entry.get("seq", 1),
                              tokens_before=c.get("tokens_before"),
                              tokens_after=c.get("tokens_after"))
        entry = await self._update_instance(instance, **fields)
        stats = self._stats(entry)
        self.ledger.write(instance, "turn", seq=stats.seq, context_tokens=stats.context_tokens,
                          turns=stats.turns, compactions=stats.compactions, memory_writes=writes)
        if native and self.on_lifecycle:
            with contextlib.suppress(Exception):
                await self.on_lifecycle(instance, "compacting", {"trigger": "grok"})
        decision = self.settings.policy.decide(stats)
        if decision.action == "none":
            return
        if decision.action == "rotate":
            # Visible at once (GET /instances): the next turn lands in a new
            # session, so a client adds its own recent-transcript context.
            await self._update_instance(instance, rotation_pending=True)
        self._schedule(instance, decision)

    def _schedule(self, instance: str, decision: Decision) -> asyncio.Task:
        task = asyncio.create_task(self._maintain(instance, decision),
                                   name=f"grok-maintenance-{instance}")
        self._maintenance[instance] = task
        task.add_done_callback(lambda t: self._maintenance.pop(instance, None)
                               if self._maintenance.get(instance) is t else None)
        return task

    async def rotate(self, instance: str, reason: str = "requested") -> dict:
        """Rotate now (e.g. the user asked for a fresh start): memory save +
        handoff, then a new session. Waits for it to finish."""
        pending = self._maintenance.get(instance)
        if pending is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(pending)
        if not self._load_state().get(instance):
            raise KeyError(instance)
        await self._update_instance(instance, rotation_pending=True)
        await self._schedule(instance, Decision("rotate", reason))
        return self.session_info(instance)

    async def _prompt_text(self, agent: _Agent, text: str) -> tuple[str, int | None, list[dict]]:
        chunks: list[str] = []
        context: int | None = None
        compacted: list[dict] = []
        async with asyncio.timeout(self.settings.turn_timeout_s):
            async for update in agent.acp.prompt(agent.session_id, text):
                kind = update.get("type")
                if kind == "text":
                    chunks.append(update["data"])
                elif kind == "usage":
                    context = update["context_tokens"]
                elif kind == "compacted":
                    compacted.append(update)
                elif kind == "error":
                    raise GrokHarnessError(f"grok: {update.get('message')}")
        return "".join(chunks), context, compacted

    async def _maintain(self, instance: str, decision: Decision) -> None:
        try:
            agent = await self._agent_for(instance, ephemeral=False)
        except Exception as exc:  # noqa: BLE001 — maintenance is retried by the next turn's policy
            logger.warning("grok harness: maintenance of %s could not start: %s", instance, exc)
            await self._update_instance(instance, rotation_pending=False)
            return
        run_id = f"maintenance-{os.urandom(6).hex()}"
        try:
            async with agent.lock:
                agent.last_used = time.monotonic()
                if decision.action == "compact":
                    await self._compact(agent, run_id, decision)
                else:
                    await self._rotate(agent, run_id, decision)
                agent.last_used = time.monotonic()
        except Exception as exc:  # noqa: BLE001 — logged + ledgered; the session stays usable
            logger.warning("grok harness: %s of %s failed: %s", decision.action, instance, exc)
            self.ledger.write(instance, "maintenance_failed", action=decision.action,
                              reason=decision.reason, error=str(exc)[:300])
            await self._update_instance(instance, rotation_pending=False)
        finally:
            agent.reserved -= 1

    async def _compact(self, agent: _Agent, run_id: str, decision: Decision) -> None:
        instance = agent.instance
        entry = self._load_state().get(instance) or {}
        with self.gateway.turn(instance, run_id) as log:
            await self._prompt_text(agent, memory_save_prompt(decision.reason))
            writes = accepted_memory_writes(log.calls)
        if self.on_lifecycle:
            with contextlib.suppress(Exception):
                await self.on_lifecycle(instance, "compacting", {"trigger": "miragen"})
        _text, _ctx, compacted = await self._prompt_text(
            agent, "/compact Keep open threads, commitments, the user's latest requests and "
                   "anything not yet saved to memory.")
        done = compacted[-1] if compacted else {}
        after = done.get("tokens_after")
        fields: dict[str, Any] = {
            "compactions": int(entry.get("compactions") or 0) + 1,
            "memory_writes": int(entry.get("memory_writes") or 0) + writes}
        if writes:
            fields["last_memory_write_at"] = time.time()
        if isinstance(after, int):
            fields["context_tokens"] = after
        await self._update_instance(instance, **fields)
        self.ledger.write(instance, "compaction", trigger="miragen", reason=decision.reason,
                          seq=entry.get("seq", 1), memory_writes=writes,
                          tokens_before=done.get("tokens_before"), tokens_after=after,
                          observed=bool(compacted))

    async def _rotate(self, agent: _Agent, run_id: str, decision: Decision) -> None:
        instance = agent.instance
        entry = self._load_state().get(instance) or {}
        limit = self.settings.handoff_max_chars
        text, error = "", None
        with self.gateway.turn(instance, run_id) as log:
            try:
                text, _ctx, _c = await self._prompt_text(
                    agent, rotation_prompt(decision.reason, limit))
            except Exception as exc:  # noqa: BLE001 — best-effort: rotate anyway, say the note is missing
                error = f"{type(exc).__name__}: {exc}"[:200]
            writes = accepted_memory_writes(log.calls)
        text = text.strip()
        rotated_at = time.time()
        memory_writes = int(entry.get("memory_writes") or 0) + writes
        handoff = Handoff(
            text=text[:limit], truncated=len(text) > limit, prev_seq=int(entry.get("seq") or 1),
            reason=decision.reason, started_at=float(entry.get("session_started_at") or rotated_at),
            rotated_at=rotated_at, turns=int(entry.get("turns") or 0),
            compactions=int(entry.get("compactions") or 0),
            context_tokens=int(entry.get("context_tokens") or 0), memory_writes=memory_writes,
            last_memory_write_at=(rotated_at if writes else entry.get("last_memory_write_at")),
            handoff_error=error or (None if text else "the note came back empty"))
        if self.on_lifecycle:
            with contextlib.suppress(Exception):
                await self.on_lifecycle(instance, "closed", {"reason": decision.reason})
        old = agent.session_id
        cwd = str(self.settings.workdirs / instance)
        rules = {"rules": self.instructions} if self.instructions else {}
        new = await agent.acp.session_new(cwd, mcp_servers=[], yolo=False, meta=rules)
        agent.session_id = new
        retired = list(entry.get("retired") or []) + [{"session_id": old, "retired_at": rotated_at}]
        retired = self._prune_retired(instance, retired)
        await self._update_instance(
            instance, session_id=new, instructions_sha=instructions_hash(self.instructions),
            instructions_update_pending=False, seq=handoff.prev_seq + 1,
            session_started_at=rotated_at, turns=0, compactions=0, context_tokens=0,
            memory_writes=0, last_memory_write_at=None, rotation_pending=False,
            handoff=handoff.to_json(), retired=retired)
        self.ledger.write(instance, "rotation", reason=decision.reason, from_seq=handoff.prev_seq,
                          to_seq=handoff.prev_seq + 1, turns=handoff.turns,
                          compactions=handoff.compactions, context_tokens=handoff.context_tokens,
                          memory_writes=writes, handoff_chars=len(text),
                          handoff_truncated=handoff.truncated, handoff_error=handoff.handoff_error)

    def _prune_retired(self, instance: str, retired: list[dict]) -> list[dict]:
        """Delete retired sessions' files once past retention; keep the rest."""
        cutoff = time.time() - self.settings.session_retention_days * 86400
        keep = []
        for r in retired:
            if float(r.get("retired_at") or 0) < cutoff:
                path = self.session_dir(instance) / str(r.get("session_id"))
                if path.is_dir():
                    shutil.rmtree(path)
                self.ledger.write(instance, "session_pruned", session_id=r.get("session_id"))
            else:
                keep.append(r)
        return keep

    def session_info(self, instance: str) -> dict | None:
        entry = self._load_state().get(instance)
        if not entry:
            return None
        seq = int(entry.get("seq") or 1)
        return {"seq": seq, "turns": int(entry.get("turns") or 0),
                "compactions": int(entry.get("compactions") or 0),
                "context_tokens": int(entry.get("context_tokens") or 0),
                "rotation_pending": bool(entry.get("rotation_pending")),
                # The next turn is the first in a rotated session.
                "fresh": bool(entry.get("rotation_pending")) or bool(entry.get("handoff")),
                "maintenance_running": instance in self._maintenance}

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

    def session_dir(self, instance: str) -> Path:
        """Where grok keeps an instance's sessions (forks included): one
        directory per working directory, named by the percent-encoded cwd."""
        cwd = str(self.settings.workdirs / instance)
        return self.settings.grok_home / "sessions" / quote(cwd, safe="")

    async def forget(self, instance: str) -> list[str]:
        """Discard an instance's conversation for good: stop its process, drop
        its session mapping, and delete grok's session files and the working
        directory. Returns what was removed (empty: nothing was there)."""
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
                if state.pop(instance, None) is not None:
                    tmp = self._state_path().with_suffix(".tmp")
                    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
                    os.replace(tmp, self._state_path())
                    removed.append("session_mapping")
            for label, path in (("grok_sessions", self.session_dir(instance)),
                                ("workdir", self.settings.workdirs / instance)):
                if path.is_dir():
                    shutil.rmtree(path)
                    removed.append(label)
        return removed


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
