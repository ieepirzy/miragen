"""The Claude Code harness: base-tier turns on the Claude subscription,
through the claude-agent-sdk, with one long-lived ``claude`` process per
instance.

Design record: docs/design/harnesses.md ("The Claude Code harness"). It is
the Grok Build harness's shape on a different subscription, so a profile can
swap between them (``spec.model``) when one is rate limited:

* **Conversation state is Claude Code's.** Each instance maps to a Claude
  Code session (``<CLAUDE_CONFIG_DIR>/projects/…/<id>.jsonl``), resumed
  whenever its process is (re)started. The mapping is persisted in
  ``<claude_home>/miragen-instances.json``, separate from Grok's, so
  swapping back resumes Grok's sessions untouched. Claude Code compacts on
  its own; miragen runs no compaction policy here (yet).
* **Tools come only from the miragen tool gateway.** The built-in tools are
  removed (``tools=[]``, plus WebSearch/WebFetch when the profile names
  them), no settings, plugins, skills or CLAUDE.md are loaded, the gateway
  is the only MCP server (``--strict-mcp-config``), and ``can_use_tool``
  refuses anything but a gateway tool or an enabled web tool. The gateway
  enforces approvals and fails closed on its own.
* **Subscription only.** The child's environment is an allowlist enforced by
  an exec wrapper (the SDK merges the parent's environment otherwise): the
  subscription token (``CLAUDE_CODE_OAUTH_TOKEN``) or a login in
  ``CLAUDE_CONFIG_DIR``, never an API key or a base URL, and none of the
  deployment's upstream MCP secrets.
* **Per-turn context rides the prompt** (decision 6), which also keeps the
  system prompt byte-stable for the prompt cache. Prompts are delivered
  verbatim: no ``@path`` expansion, no slash commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from miragen.harness.base import (
    HarnessResult, HarnessTurn, InstanceBusyError, TaskStream, parse_harness_model,
)
from miragen.harness.gateway import ToolGateway, _capability_entries
from miragen.harness.served import ServedLedger
from miragen.memory.claude_code import SCRUBBED_ENV, WORKER_ENV
from miragen.models import AgentProfile, RunUsage

logger = logging.getLogger("miragen.harness.claude_code")

NAME = "claude-code"
# Short on purpose: Claude Code names MCP tools mcp__<server>__<tool>, and
# tool names are capped at 64 characters.
GATEWAY_SERVER = "gw"
GATEWAY_TOKEN_ENV = "MIRAGEN_GATEWAY_TOKEN"
STATE_FILE = "miragen-instances.json"
WRAPPER_FILE = "miragen-claude"
KEEP_ENV = "MIRAGEN_ENV_KEEP"
# The only environment a claude process inherits from the container (plus
# what _env sets). Same list as the Grok harness's.
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
                 "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
_GATEWAY_PREFIX = f"mcp__{GATEWAY_SERVER}__"

# miragen capability name -> the Claude Code built-in it enables.
NATIVE_CAPABILITIES: dict[str, str] = {"WebSearch": "WebSearch", "WebFetch": "WebFetch"}


class ClaudeCodeHarnessError(RuntimeError):
    pass


@dataclass
class ClaudeCodeSettings:
    claude_home: Path
    workdirs: Path
    gateway_url: str
    cli_path: str | None = None
    max_processes: int = 3
    idle_s: float = 900.0
    turn_timeout_s: float = 600.0
    effort: str | None = None
    # Claude Code's own auto-compaction (there is no miragen policy here):
    # the window it compacts against. None leaves its default, the model's
    # full window (sonnet: compaction at 967k, observed); every heartbeat and
    # tool step re-sends the whole session, so a persistent agent wants it
    # low. 100000 is Claude Code's floor and compacts at 67k (observed via
    # get_context_usage; CLAUDE_AUTOCOMPACT_PCT_OVERRIDE did not move it).
    auto_compact_window: int | None = None

    @classmethod
    def from_env(cls, *, gateway_url: str) -> "ClaudeCodeSettings":
        env = os.environ
        home = Path(env.get("MIRAGEN_CLAUDE_HOME", "/agent/claude-home"))
        return cls(
            claude_home=home,
            workdirs=Path(env.get("MIRAGEN_CLAUDE_WORKDIRS", str(home / "workspaces"))),
            gateway_url=gateway_url,
            cli_path=env.get("CLAUDE_BIN") or None,
            max_processes=int(env.get("MIRAGEN_CLAUDE_MAX_PROCESSES", "3")),
            idle_s=float(env.get("MIRAGEN_CLAUDE_IDLE_S", "900")),
            turn_timeout_s=float(env.get("MIRAGEN_CLAUDE_TURN_TIMEOUT_S", "600")),
            effort=env.get("MIRAGEN_CLAUDE_EFFORT") or None,
            auto_compact_window=(int(env["MIRAGEN_CLAUDE_AUTO_COMPACT_WINDOW"])
                                 if env.get("MIRAGEN_CLAUDE_AUTO_COMPACT_WINDOW") else None),
        )


@dataclass
class _Agent:
    instance: str
    client: Any  # ClaudeSDKClient
    session_id: str
    ephemeral: bool
    stderr: deque = field(default_factory=lambda: deque(maxlen=40))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    reserved: int = 0
    dead: bool = False

    @property
    def busy(self) -> bool:
        return self.reserved > 0 or self.lock.locked()


def bundled_cli() -> str | None:
    """The CLI shipped inside claude-agent-sdk (version-matched to it)."""
    with contextlib.suppress(ImportError):
        import claude_agent_sdk

        path = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        if path.is_file():
            return str(path)
    return None


def _sdk_client(options):
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options)


class ClaudeCodeHarness:
    name = NAME
    native_capabilities = frozenset(NATIVE_CAPABILITIES)

    def __init__(
        self,
        profile: AgentProfile,
        gateway: ToolGateway,
        settings: ClaudeCodeSettings,
        *,
        client_factory: Callable[[Any], Any] | None = None,
        system_guidance: str | None = None,
        served: ServedLedger | None = None,
    ):
        if profile.spec is None:
            raise ClaudeCodeHarnessError("the claude-code harness runs base-tier (spec:) profiles")
        self.profile = profile
        self.gateway = gateway
        self.settings = settings
        self.model = parse_harness_model(profile.spec.model)[1] or None
        caps = {name for name, _ in _capability_entries(profile)}
        self.builtins = frozenset(NATIVE_CAPABILITIES[c] for c in caps if c in NATIVE_CAPABILITIES)
        from miragen.voice import with_voice_guidance

        self.instructions = with_voice_guidance(profile.spec.instructions or "", system_guidance)
        self._client_factory = client_factory or _sdk_client
        self.served = served
        # Clock order, as in the Grok harness: approval wait < Claude Code's
        # MCP tool timeout < this harness's turn timeout.
        self.tool_timeout_s: int | None = (
            profile.approval_timeout_s + 120 if profile.approval_required else None)
        if self.tool_timeout_s and settings.turn_timeout_s < self.tool_timeout_s + 60:
            logger.warning("claude-code harness: raising the turn timeout from %.0fs to %ds so "
                           "it outlasts gated tool calls", settings.turn_timeout_s,
                           self.tool_timeout_s + 60)
            settings.turn_timeout_s = self.tool_timeout_s + 60
        self._agents: dict[str, _Agent] = {}
        self._spawn_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        # Set by the app: session events for the memory plane.
        self.on_lifecycle: Callable[[str, str, dict], Awaitable[None]] | None = None
        self.prepare()

    # ── home, wrapper, state ─────────────────────────────────────────────
    def prepare(self) -> None:
        home = self.settings.claude_home
        home.mkdir(parents=True, exist_ok=True)
        (home / "home").mkdir(exist_ok=True)
        self.settings.workdirs.mkdir(parents=True, exist_ok=True)
        real = self.settings.cli_path or bundled_cli() or shutil.which("claude")
        if not real:
            logger.warning("claude-code harness: no claude CLI (bundled with claude-agent-sdk, "
                           "CLAUDE_BIN or PATH); turns will fail")
            real = "claude"
        # The SDK merges the parent's environment into the child's; this
        # wrapper replaces it with an allowlist before exec'ing the CLI.
        wrapper = home / WRAPPER_FILE
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            f"keep = set(os.environ.get({KEEP_ENV!r}, '').split())\n"
            "env = {k: v for k, v in os.environ.items()\n"
            "       if k in keep or k.startswith('CLAUDE_AGENT_SDK_')}\n"
            f"os.execve({real!r}, [{real!r}, *sys.argv[1:]], env)\n")
        wrapper.chmod(0o700)
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and not self._logged_in():
            logger.warning("claude-code harness: no subscription credential (neither the "
                           "setup-token env var nor a login in CLAUDE_CONFIG_DIR); turns will "
                           "fail until one is provided (`claude setup-token`)")

    def _logged_in(self) -> bool:
        return (self.settings.claude_home / ".credentials.json").is_file()

    def _state_path(self) -> Path:
        return self.settings.claude_home / STATE_FILE

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
        env = {
            "HOME": str(self.settings.claude_home / "home"),
            "CLAUDE_CONFIG_DIR": str(self.settings.claude_home),
            GATEWAY_TOKEN_ENV: self.gateway.credential(instance),
            WORKER_ENV: "1",  # the memory hook adapter must never capture these turns
            # Tool search defers MCP tools behind a built-in this harness
            # removes; keep every gateway tool in context instead.
            "ENABLE_TOOL_SEARCH": "false",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        if self.tool_timeout_s:
            env["MCP_TOOL_TIMEOUT"] = str(self.tool_timeout_s * 1000)
        if self.settings.auto_compact_window:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(self.settings.auto_compact_window)
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        keep = [k for k in ENV_ALLOWLIST if k in os.environ]
        keep += [*env, "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SDK_READS_SESSION_STATE", "PWD"]
        assert not set(keep) & set(SCRUBBED_ENV)
        env[KEEP_ENV] = " ".join(keep)
        return env

    # ── processes ────────────────────────────────────────────────────────
    async def _permission(self, tool_name: str, _input: dict, _context: Any):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        if (tool_name.startswith(_GATEWAY_PREFIX) and len(tool_name) > len(_GATEWAY_PREFIX)) \
                or tool_name in self.builtins:
            return PermissionResultAllow()
        logger.warning("claude-code harness: refused a non-gateway tool (%s)", tool_name)
        return PermissionResultDeny(message="Only miragen gateway tools are available.")

    def _session_file(self, session_id: str) -> Path | None:
        projects = self.settings.claude_home / "projects"
        return next(projects.glob(f"*/{session_id}.jsonl"), None) if projects.is_dir() else None

    def _options(self, instance: str, workdir: Path, *, resume: str | None, new_id: str | None,
                 stderr: deque):
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        async def pre_compact(_input, _tool_use_id, _context):
            if self.on_lifecycle is not None:
                with contextlib.suppress(Exception):
                    await self.on_lifecycle(instance, "compacting", {"trigger": "claude-code"})
            with contextlib.suppress(Exception):
                entry = self._load_state().get(instance) or {}
                await self._update_instance(
                    instance, compactions=int(entry.get("compactions") or 0) + 1)
            return {}

        return ClaudeAgentOptions(
            tools=sorted(self.builtins),
            system_prompt=self.instructions,
            mcp_servers={GATEWAY_SERVER: {
                "type": "http", "url": self.settings.gateway_url,
                # Expanded by Claude Code from the child's environment, so the
                # credential never lands on the command line or on disk.
                "headers": {"Authorization": f"Bearer ${{{GATEWAY_TOKEN_ENV}}}"}}},
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            permission_mode="default",
            can_use_tool=self._permission,
            model=self.model,
            effort=self.settings.effort,
            cwd=str(workdir),
            cli_path=str(self.settings.claude_home / WRAPPER_FILE),
            env=self._env(instance),
            resume=resume,
            session_id=new_id,
            verbatim_prompts=True,
            hooks={"PreCompact": [HookMatcher(hooks=[pre_compact])]},
            stderr=stderr.append,
        )

    async def _spawn(self, instance: str, *, ephemeral: bool) -> _Agent:
        workdir = self.settings.workdirs / instance
        workdir.mkdir(parents=True, exist_ok=True)
        known = None if ephemeral else (self._load_state().get(instance) or {}).get("session_id")
        if known and self._session_file(known) is None:
            logger.warning("claude-code harness: session %s of %s is gone; starting a new one",
                           known, instance)
            await self._update_instance(instance, session_id=None, lost=True)
            known = None
        session_id = known or str(uuid.uuid4())
        stderr: deque = deque(maxlen=40)
        client = self._client_factory(self._options(
            instance, workdir, resume=known, new_id=None if known else session_id, stderr=stderr))
        try:
            await client.connect()
        except BaseException as exc:
            with contextlib.suppress(Exception):
                await client.disconnect()
            if known and isinstance(exc, Exception):
                # A session file that exists but won't resume (corrupt,
                # incompatible): don't fail every turn on it — start over.
                logger.warning("claude-code harness: session %s of %s would not resume (%s); "
                               "starting a new one", known, instance, exc)
                await self._update_instance(instance, session_id=None, lost=True)
                return await self._spawn(instance, ephemeral=ephemeral)
            if ephemeral:
                shutil.rmtree(workdir, ignore_errors=True)
            raise
        if not ephemeral and not known:
            entry = self._load_state().get(instance) or {}
            sessions = [*(entry.get("sessions") or []), session_id]
            await self._update_instance(instance, session_id=session_id, sessions=sessions,
                                        seq=int(entry.get("seq") or 0) or self._first_seq(instance),
                                        session_started_at=time.time())
        return _Agent(instance=instance, client=client, session_id=session_id,
                      ephemeral=ephemeral, stderr=stderr)

    def _seq(self, instance: str, entry: dict) -> int:
        """The plane session number now (the same before and after a turn)."""
        own = int(entry.get("seq") or 0) or self._first_seq(instance)
        return self.served.seq_for(instance, NAME, own) if self.served is not None else own

    def _first_seq(self, instance: str) -> int:
        """A new conversation continues the plane's session numbering after
        whichever harness served the instance before."""
        prev = self.served.get(instance) if self.served is not None else None
        return int(prev.get("seq") or 0) + 1 if prev else 1

    async def _agent_for(self, instance: str, *, ephemeral: bool) -> _Agent:
        async with self._spawn_lock:
            agent = self._agents.get(instance)
            if agent is not None and not agent.dead:
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
            with contextlib.suppress(Exception):
                await agent.client.disconnect()

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_idle(), name="claude-code-harness-reaper")

    async def _reap_idle(self) -> None:
        while self._agents:
            await asyncio.sleep(min(60.0, max(1.0, self.settings.idle_s / 4)))
            now = time.monotonic()
            for agent in list(self._agents.values()):
                if not agent.busy and now - agent.last_used > self.settings.idle_s:
                    async with self._spawn_lock:
                        # Re-check under the lock: a turn may have reserved it meanwhile.
                        if self._agents.get(agent.instance) is agent and not agent.busy:
                            logger.info("claude-code harness: stopping idle process for %s",
                                        agent.instance)
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
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        if turn.secret_env:
            raise ClaudeCodeHarnessError(
                "per-launch MCP credentials are not supported by the claude-code harness "
                "(the gateway holds deployment-level upstream credentials)")
        instance, ephemeral = self._key(turn)
        # A `lost` the client could see before this turn is now consumed; one
        # raised while starting this turn must survive it (next turn: fresh).
        lost_before = bool((self._load_state().get(instance) or {}).get("lost"))
        agent = await self._agent_for(instance, ephemeral=ephemeral)
        chunks: list[str] = []
        result: Any = None
        context: int | None = None
        try:
            async with agent.lock:
                agent.last_used = time.monotonic()
                with self.gateway.turn(instance, turn.run_id) as log:
                    try:
                        async with asyncio.timeout(self.settings.turn_timeout_s):
                            await agent.client.query(self._compose(turn))
                            after_tool = False
                            async for message in agent.client.receive_response():
                                if isinstance(message, AssistantMessage):
                                    if message.parent_tool_use_id:
                                        continue
                                    if message.usage:
                                        # The last model call's prompt = the context
                                        # size (the result's usage sums every call).
                                        context = _context_tokens(message.usage)
                                    for block in message.content:
                                        if isinstance(block, ToolUseBlock):
                                            after_tool = True
                                        elif isinstance(block, TextBlock) and block.text:
                                            text = block.text
                                            # Text around a tool call arrives as
                                            # separate messages; keep them apart.
                                            if after_tool and chunks and not chunks[-1].endswith("\n"):
                                                text = "\n\n" + text
                                            after_tool = False
                                            chunks.append(text)
                                            if on_text:
                                                on_text(text)
                                elif isinstance(message, ResultMessage):
                                    result = message
                    except TimeoutError:
                        with contextlib.suppress(Exception):
                            await agent.client.interrupt()
                        agent.dead = True
                        raise ClaudeCodeHarnessError(
                            f"turn exceeded {self.settings.turn_timeout_s:.0f}s; cancelled") from None
                    except asyncio.CancelledError:
                        # The caller went away (e.g. a dropped stream). The rest of
                        # this turn would stay queued in the SDK's message stream
                        # and be read as the next turn's reply, and its tool calls
                        # would run under the next turn: stop it and respawn.
                        with contextlib.suppress(Exception):
                            await agent.client.interrupt()
                        agent.dead = True
                        raise
                    except ClaudeCodeHarnessError:
                        raise
                    except Exception as exc:
                        agent.dead = True  # the process is gone or confused: respawn + resume
                        raise ClaudeCodeHarnessError(
                            f"claude: {exc}; stderr: {' | '.join(list(agent.stderr)[-5:])}") from exc
                    calls = list(log.calls)
                agent.last_used = time.monotonic()
        finally:
            agent.reserved -= 1
            if ephemeral or agent.dead:
                async with self._spawn_lock:
                    if self._agents.get(instance) is agent:
                        await self._drop(instance)
            if ephemeral:  # nothing of a one-off run outlives it
                path = self._session_file(agent.session_id)
                if path is not None:
                    path.unlink()
                    shutil.rmtree(path.with_suffix(""), ignore_errors=True)
                    with contextlib.suppress(OSError):
                        path.parent.rmdir()
                shutil.rmtree(self.settings.workdirs / instance, ignore_errors=True)
        if result is None:
            raise ClaudeCodeHarnessError("claude ended the turn without a result")
        if not ephemeral:
            await self._after_turn(instance, context, lost_before=lost_before)
        if result.is_error:
            raise ClaudeCodeHarnessError(
                f"claude: {result.subtype}: {(result.result or '').strip()[:500]}")
        return HarnessResult(output="".join(chunks), usage=_usage(result.usage or {}),
                             tool_calls=calls)

    async def _after_turn(self, instance: str, context: int | None, *,
                          lost_before: bool = False) -> None:
        entry = self._load_state().get(instance) or {}
        seq = self._seq(instance, entry)
        fields: dict[str, Any] = {}
        if context is not None:
            fields["context_tokens"] = context
        if lost_before or not entry.get("lost"):
            fields["lost"] = False
        await self._update_instance(
            instance, seq=seq, turns=int(entry.get("turns") or 0) + 1, rotated=False, **fields)
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
        return {"seq": self._seq(instance, entry),
                "turns": int(entry.get("turns") or 0),
                "compactions": int(entry.get("compactions") or 0),
                "context_tokens": int(entry.get("context_tokens") or 0),
                "rotation_pending": False,
                # The next turn doesn't continue the conversation the client
                # saw last: new, rotated, lost, or served by another harness.
                "fresh": (not entry.get("session_id") or bool(entry.get("rotated"))
                          or bool(entry.get("lost")) or switched),
                "maintenance_running": False,
                "harness": NAME}

    async def rotate(self, instance: str, reason: str = "requested") -> dict:
        """Start the instance's next session now. No handoff note: Claude
        Code sessions are cheap to replace, and the client adds its recent
        transcript when it sees `fresh`."""
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
        await self._update_instance(instance, session_id=None, rotated=True, turns=0,
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
            for sid in (entry or {}).get("sessions") or []:
                path = self._session_file(sid)
                if path is not None:
                    path.unlink()
                    shutil.rmtree(path.with_suffix(""), ignore_errors=True)
                    removed.append(f"claude_session:{sid}")
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


def _context_tokens(usage: dict[str, Any]) -> int:
    return sum(int(usage.get(k) or 0) for k in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def _usage(raw: dict[str, Any]) -> RunUsage:
    def pick(*keys):
        for k in keys:
            if isinstance(raw.get(k), int):
                return raw[k]
        return None
    return RunUsage(requests=1, input_tokens=pick("input_tokens"),
                    output_tokens=pick("output_tokens"),
                    cached_input_tokens=pick("cache_read_input_tokens"))
