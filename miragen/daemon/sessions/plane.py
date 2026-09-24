"""The session plane: what miragend does with an external session's events.

One `SessionPlane` per daemon. Per event it: registers/touches the
session (any event — recovery after a daemon restart is free), resolves
the project and its scopes once, answers context-bearing events
synchronously (bounded), journals and then processes captures in the
background under a per-session lock (ordered, off the hook's clock),
writes a deterministic episode + a working-state checkpoint at
compaction and at session end, and finalizes sessions whose process
disappeared. Every durable write goes through MemoryLifecycle → Loimi;
nothing here is a second memory model.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from miragen.daemon.sessions.config import SessionsConfig
from miragen.daemon.sessions.judgments import JudgmentLog
from miragen.daemon.sessions.models import (
    ChildAgent,
    EventEnvelope,
    ExternalSession,
    ProjectIdentity,
    now_iso,
    session_key,
)
from miragen.daemon.sessions.projects import (
    ScopeAssignment,
    assign_scopes,
    identity_from_directory,
    identity_from_remote,
    is_name_derived,
    normalize_remote,
    project_slug,
    resolve_project,
)
from miragen.daemon.sessions.registry import EventJournal, SessionRegistry, pid_alive
from miragen.daemon.sessions.store import StoreAPIError, StoreClient, StoreUnavailable
from miragen.memory.client import MemoryAPIError, MemoryClient, MemoryUnavailable
from miragen.memory.lifecycle import MemoryLifecycle, RecallCandidates, RecallSelection
from miragen.models import MemoryRecallSpec, MemoryScopesSpec, MemorySpec
from miragen_hook.normalize import CAPTURED, CONTEXT_OPENING, NormalizedEvent


def _same_dir(a: str, b: str) -> bool:
    """Path equality as the harness reported it (no filesystem access: the
    paths usually belong to another host)."""
    return a.rstrip("/") == b.rstrip("/")

logger = logging.getLogger("miragend.sessions")

# Daemon-side bounds UNDER the adapter's (client.py: 10 s for context).
RETRIEVAL_TIMEOUT_S = 8.0
WRITE_TIMEOUT_S = 15.0
_PROVISION_VERBS = ["read", "propose", "resolve", "retract"]
# The extraction worker reads events, proposes records and claims jobs.
_WORKER_VERBS = ["read", "propose", "maintain"]


RECENT_CAPTURE_WINDOW = 200
RECENT_CAPTURE_SECONDS = 30 * 60


@dataclass
class PlaneStats:
    """Mechanical counters for /health (the telemetry convention: what
    happened, never content). Latencies are running totals so the health
    reader can compute means without the daemon keeping histograms."""

    events_received: int = 0
    events_rejected: int = 0
    sessions_registered: int = 0
    retrievals: int = 0
    retrieval_failures: int = 0
    injections: int = 0
    deferred_injections: int = 0
    prompt_recalls: int = 0
    captures: int = 0
    capture_failures: int = 0
    episodes: int = 0
    checkpoints: int = 0
    timeouts: int = 0
    provisioned_scopes: int = 0
    provisioning_failures: int = 0
    replayed: int = 0
    finalized_by_sweep: int = 0
    retrieval_ms_total: float = 0.0
    retrieval_count: int = 0
    write_ms_total: float = 0.0
    write_count: int = 0
    last_loimi_ok_at: str | None = None
    last_loimi_error: str | None = None
    last_loimi_error_at: str | None = None
    by_harness: dict[str, int] = field(default_factory=dict)
    # Artifact store (Loimi /v0) participation.
    runs_opened: int = 0
    runs_closed: int = 0
    artifacts_written: int = 0
    store_failures: int = 0
    last_store_ok_at: str | None = None
    last_store_error: str | None = None
    last_store_error_at: str | None = None
    # Startup provisioning + identity adoption.
    adopted_by_name: int = 0
    remote_sessions: int = 0
    project_switches: int = 0
    async_recalls_started: int = 0
    async_recalls_delivered: int = 0
    async_recalls_empty: int = 0
    async_recalls_dropped: int = 0
    async_recall_failures: int = 0
    recall_wait_timeouts: int = 0
    nudges_fired: int = 0
    nudges_reasked: int = 0
    nudges_answered: int = 0
    nudges_ignored: int = 0
    memory_writes: int = 0
    worker_grants: int = 0
    worker_grant_failures: int = 0
    raw_hooks_shadowed: int = 0
    late_opens: int = 0
    empty_sessions: int = 0
    # Recent write outcomes as (monotonic time, scope, ok) — hook captures,
    # episodes and checkpoints alike. The lifetime counters above never
    # forget an old outage; the status line an agent sees must reflect what
    # is failing now, for its own project.
    recent_captures: deque = field(default_factory=lambda: deque(maxlen=RECENT_CAPTURE_WINDOW))

    def note_outcome(self, ok: bool, scope: str | None) -> None:
        self.recent_captures.append((time.monotonic(), scope, ok))

    def note_capture(self, ok: bool, scope: str | None) -> None:
        if ok:
            self.captures += 1
        else:
            self.capture_failures += 1
        self.note_outcome(ok, scope)

    def recent_outcomes(self, scope: str | None = None) -> list[bool]:
        """Outcomes within RECENT_CAPTURE_SECONDS, for one scope or all."""
        cutoff = time.monotonic() - RECENT_CAPTURE_SECONDS
        return [ok for at, sc, ok in self.recent_captures
                if at >= cutoff and (scope is None or sc == scope)]

    def note_loimi(self, ok: bool, error: str | None = None) -> None:
        if ok:
            self.last_loimi_ok_at = now_iso()
        else:
            self.last_loimi_error = (error or "unknown")[:300]
            self.last_loimi_error_at = now_iso()

    def note_store(self, ok: bool, error: str | None = None) -> None:
        if ok:
            self.last_store_ok_at = now_iso()
        else:
            self.store_failures += 1
            self.last_store_error = (error or "unknown")[:300]
            self.last_store_error_at = now_iso()

    def snapshot(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if not k.endswith("_total")
                and k not in ("retrieval_count", "write_count", "recent_captures")}
        recent = self.recent_outcomes()
        data["recent_capture_failures"] = recent.count(False)
        data["recent_capture_window"] = len(recent)
        data["retrieval_ms_avg"] = (
            round(self.retrieval_ms_total / self.retrieval_count, 1) if self.retrieval_count else None
        )
        data["write_ms_avg"] = (
            round(self.write_ms_total / self.write_count, 1) if self.write_count else None
        )
        return data


@dataclass
class HandleResult:
    key: str
    state: str
    context: str | None = None
    detail: str | None = None
    # A background recall was started for this prompt: the adapter keeps a
    # marker with this sequence number and claims the result later.
    recall_pending: int | None = None
    # Keep the agent going at this Stop with this text (the end-of-work
    # save nudge); only adapters advertising `stop-continue` get it.
    continue_with: str | None = None


ASYNC_RECALL_CAPABILITY = "async-recall"
STOP_CONTINUE_CAPABILITY = "stop-continue"
NOTHING_DURABLE = "nothing durable"
# The longest any claim may hold its HTTP request open.
MAX_CLAIM_WAIT_S = 15.0


@dataclass
class RecallJob:
    """One prompt's background recall. Superseded by the next prompt,
    dropped at compaction/end; delivered at most once (the claim IS the
    delivery)."""

    seq: int
    key: str
    scope: str
    trigger: str
    candidates: RecallCandidates
    lifecycle: MemoryLifecycle
    task: asyncio.Task | None = None
    selection: RecallSelection | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    delivered: str | None = None


ClientFactory = Callable[[MemorySpec], MemoryClient]


class SessionPlane:
    def __init__(
        self,
        config: SessionsConfig,
        *,
        state_dir: Path | None = None,
        environ: dict | None = None,
        client_factory: ClientFactory | None = None,
        operator_client_factory: Callable[[str], MemoryClient] | None = None,
        selector=None,
        telemetry=None,
        is_alive: Callable[[int], bool] = pid_alive,
        resolver: Callable[[str | None], ProjectIdentity] = resolve_project,
        store_client: StoreClient | None = None,
        local_host: str | None = None,
    ) -> None:
        import os

        self.config = config
        self.environ = os.environ if environ is None else environ
        self.state_dir = state_dir or config.resolved_state_dir(self.environ)
        # Liveness and filesystem inspection only mean something for
        # harnesses on this host; everything else is a remote session.
        self.local_host = local_host or socket.gethostname()
        self.registry = SessionRegistry(
            self.state_dir,
            retention_hours=config.housekeeping.retention_hours,
            stale_after_minutes=config.housekeeping.stale_after_minutes,
        )
        self.journal = EventJournal(self.state_dir)
        self.stats = PlaneStats()
        # Sessions already told memory is unavailable (cleared on recovery).
        self._outage_announced: set[str] = set()
        self._client_factory = client_factory
        self._operator_client_factory = operator_client_factory
        self.selector = selector
        self.telemetry = telemetry
        self._is_alive = is_alive
        self._resolve = resolver
        self._locks: dict[str, asyncio.Lock] = {}
        self._lifecycles: dict[str, MemoryLifecycle] = {}
        self._assignments: dict[str, ScopeAssignment] = {}
        self._projects: dict[str, ProjectIdentity] = {}
        self._provisioned: set[str] = set()
        self._unprovisionable: dict[str, str] = {}
        self._worker_ungranted: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._recalls: dict[str, RecallJob] = {}
        self._recall_seq: dict[str, int] = {}
        self._recall_failure_announced: set[str] = set()
        self.judgments = JudgmentLog(
            self.state_dir / "judgments",
            retention_days=config.recall.judgment_retention_days,
            max_bytes=config.recall.judgment_max_mb * 1024 * 1024,
        )
        self._sweeper: asyncio.Task | None = None
        self.principal_source: str | None = None
        self.shared_scopes_ready: dict[str, str] = {}
        if store_client is not None:
            self.store: StoreClient | None = store_client
        elif config.store.enabled:
            self.store = StoreClient(
                config.store, environ=self.environ,
                memory_endpoint_env=config.endpoint_env,
                operator_token_env=config.operator_token_env,
            )
        else:
            self.store = None
        # Remote-derived identities seen so far, by repository name, so a
        # path-only session (cloud VM) can join the project it belongs to.
        self._known_by_name: dict[str, ProjectIdentity] = {}
        for session in self.registry.list():
            if session.project is not None:
                self._remember_project(session.project)

    # ── lifecycle of the plane itself ──────────────────────────────────────

    async def start(self) -> None:
        await self.ensure_principal()
        await self.ensure_shared_scopes()
        await self.replay_journal()
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="miragend-sessions-sweeper")

    # ── startup provisioning ─────────────────────────────────────────────────

    def _token_file(self) -> Path:
        return self.state_dir / "principal.token"

    async def ensure_principal(self) -> str:
        """Make sure the daemon holds a principal token. Order: the
        credential env (an operator-minted token wins), then the token this
        daemon minted earlier (state dir), then — with the operator token
        and `provision_principal: true` — create the principal or mint a
        fresh token for an existing one, and persist it. Returns where the
        credential came from; 'missing' means every path failed and the
        plane will degrade explicitly on first use."""
        credential_env = self.config.credential_env
        if self.environ.get(credential_env):
            self.principal_source = "environment"
            return self.principal_source
        token_file = self._token_file()
        try:
            stored = token_file.read_text().strip()
        except OSError:
            stored = ""
        if stored:
            self.environ[credential_env] = stored
            self.principal_source = "state_dir"
            return self.principal_source
        if not self.config.provision_principal:
            self.principal_source = "missing"
            return self.principal_source
        operator = self._operator_client()
        if operator is None:
            logger.warning(
                f"no {credential_env} and no {self.config.operator_token_env}: the daemon "
                "has no memory principal credential; memory degrades explicitly"
            )
            self.principal_source = "missing"
            return self.principal_source
        principal = self.config.principal
        try:
            try:
                created = await asyncio.wait_for(
                    operator.admin_create_principal(
                        principal_id=principal, kind="agent",
                        description="miragend session plane (external harness sessions)",
                    ),
                    timeout=WRITE_TIMEOUT_S,
                )
                token = created["token"]
                how = "created"
            except MemoryAPIError as exc:
                if exc.status_code != 409:
                    raise
                minted = await asyncio.wait_for(
                    operator.admin_mint_token(principal_id=principal), timeout=WRITE_TIMEOUT_S,
                )
                token = minted["token"]
                how = "minted"
        except (MemoryUnavailable, MemoryAPIError, asyncio.TimeoutError) as exc:
            self.stats.note_loimi(False, f"provision principal: {exc}")
            logger.warning(f"could not provision principal {principal}: {exc}")
            self.principal_source = "missing"
            return self.principal_source
        self.state_dir.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token + "\n")
        try:
            token_file.chmod(0o600)
        except OSError:  # pragma: no cover - exotic filesystems
            pass
        self.environ[credential_env] = token
        self.stats.note_loimi(True)
        self.principal_source = how
        logger.info(f"principal {principal}: token {how} and persisted at {token_file}")
        return self.principal_source

    async def _grant_worker(self, operator: MemoryClient, scope_id: str) -> None:
        """The extraction worker's grant on a scope this daemon provisioned.
        Best-effort: a missing worker principal (not created yet) must not
        cost the session its own scope; it is counted and logged instead."""
        worker = self.config.scopes.worker_principal
        if not worker:
            return
        try:
            await asyncio.wait_for(operator.admin_grant(
                principal_id=worker, scope_id=scope_id, verbs=_WORKER_VERBS,
            ), timeout=WRITE_TIMEOUT_S)
            self.stats.worker_grants += 1
            self._worker_ungranted.discard(scope_id)
        except MemoryAPIError as exc:
            if exc.status_code == 409:
                self._worker_ungranted.discard(scope_id)
                return
            self._worker_ungranted.add(scope_id)
            self.stats.worker_grant_failures += 1
            logger.warning(f"worker grant on {scope_id} for {worker} refused: {exc}")
        except (MemoryUnavailable, asyncio.TimeoutError) as exc:
            self._worker_ungranted.add(scope_id)
            self.stats.worker_grant_failures += 1
            logger.warning(f"worker grant on {scope_id} for {worker} failed: {exc}")

    @staticmethod
    def _scope_kind_for(scope_id: str) -> str:
        prefix = scope_id.split(":", 1)[0] if ":" in scope_id else ""
        return prefix if prefix in ("instance", "profile", "role", "group", "shared", "fleet") else "group"

    async def ensure_shared_scopes(self) -> dict[str, str]:
        """The read-only layers every session gets must exist and be
        granted; on a fresh Loimi nothing does. Idempotent (409 = exists),
        best-effort, and reported on /health rather than fatal."""
        operator = self._operator_client()
        for scope_id in self.config.scopes.shared_read:
            if operator is None:
                self.shared_scopes_ready[scope_id] = "unverified (no operator token)"
                continue
            try:
                try:
                    await asyncio.wait_for(operator.admin_create_scope(
                        scope_id=scope_id, kind=self._scope_kind_for(scope_id),
                        description="shared read layer for miragend external sessions",
                    ), timeout=WRITE_TIMEOUT_S)
                except MemoryAPIError as exc:
                    if exc.status_code != 409:
                        raise
                await asyncio.wait_for(operator.admin_grant(
                    principal_id=self.config.principal, scope_id=scope_id, verbs=["read"],
                ), timeout=WRITE_TIMEOUT_S)
                self.shared_scopes_ready[scope_id] = "ready"
                self.stats.note_loimi(True)
            except (MemoryUnavailable, MemoryAPIError, asyncio.TimeoutError) as exc:
                self.shared_scopes_ready[scope_id] = f"failed: {exc}"[:200]
                self.stats.note_loimi(False, f"shared scope {scope_id}: {exc}")
                logger.warning(f"shared scope {scope_id} not ensured: {exc}")
        return dict(self.shared_scopes_ready)

    async def stop(self, *, timeout: float = 10.0) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):
                pass
            self._sweeper = None
        await self.drain(timeout=timeout)
        self.registry.save()

    async def drain(self, *, timeout: float | None = None) -> None:
        """Wait for background captures — tests and shutdown."""
        while self._tasks:
            pending = list(self._tasks)
            done, _ = await asyncio.wait(pending, timeout=timeout)
            if not done:
                logger.warning(f"{len(pending)} session task(s) still pending at drain timeout")
                return

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    # ── the entry point ──────────────────────────────────────────────────────

    async def handle(self, envelope: EventEnvelope) -> HandleResult:
        self.stats.events_received += 1
        self.stats.by_harness[envelope.harness] = self.stats.by_harness.get(envelope.harness, 0) + 1
        session, created = self.registry.upsert(envelope)
        if created:
            self.stats.sessions_registered += 1
        await self._attach_project(session, envelope)

        span_cm = None
        if self.telemetry is not None:
            span_cm = self.telemetry.run_span(
                "session.event", run_id=session.key, trigger=envelope.event.name,
                tier="external",
                **{
                    "mira.session.harness": session.harness,
                    "mira.event.original": envelope.event.original_event,
                    "mira.project.id": session.project.id if session.project else None,
                },
            )
        if span_cm is None:
            result = await self._dispatch(session, envelope)
        else:
            with span_cm as span:
                result = await self._dispatch(session, envelope)
                span.set_attribute("mira.event.outcome", "context" if result.context else "accepted")
        self.registry.save()
        return result

    async def _dispatch(self, session: ExternalSession, envelope: EventEnvelope) -> HandleResult:
        event = envelope.event
        context: str | None = None
        detail: str | None = None
        recall_pending: int | None = None
        continue_with: str | None = None
        async with self._lock(session.key):
            if event.name in CONTEXT_OPENING:
                context, detail = await self._open_context(session, envelope)
            elif event.name == "input.received":
                if session.nudge_state is not None:
                    # A new prompt instead of an answer: the ask is over (it
                    # never re-surfaces at the end of an unrelated turn).
                    session.nudge_state = None
                    self.stats.nudges_ignored += 1
                session.note_prompt(event.content)
                self._journal_and_capture(session, envelope)
                if session.reopen_pending and session.counters.injections:
                    # The agent cd'd into another repository: its working
                    # state, status line and scope header are that
                    # project's now, so the model gets them before recall.
                    session.reopen_pending = False
                    context, detail = await self._open_context(session, envelope)
                elif session.counters.injections == 0:
                    # The session's start was never answered (a SessionStart
                    # hook that ran before its credentials existed — seen on
                    # cloud VMs — or a daemon that was down). The first prompt
                    # is the next context-bearing event: open the context now,
                    # so the session still gets its working state and guide.
                    self.stats.late_opens += 1
                    session.reopen_pending = False
                    context, detail = await self._open_context(session, envelope)
                recalled, recall_detail, recall_pending = await self._prompt_recall(
                    session, envelope)
                if recalled:
                    context = f"{context}\n{recalled}" if context else recalled
                detail = "; ".join(part for part in (detail, recall_detail) if part) or None
            elif event.name == "turn.finished":
                session.note_turn(event.content)
                self._journal_and_capture(session, envelope)
                if STOP_CONTINUE_CAPABILITY in envelope.client.capabilities:
                    continue_with = self._nudge(session, event.content)
            elif event.name == "tool.finished":
                session.counters.tool_failures += 1
                self._journal_and_capture(session, envelope)
            elif event.name == "context.compacting":
                # A result prepared for the pre-compaction context must not
                # land in the compacted one.
                self._drop_recall(session.key)
                session.counters.compactions += 1
                self.journal.append(envelope)
                self._spawn(self._capture_then_finalize(
                    session, envelope, occurrence=f"compact-{session.counters.compactions}",
                ))
            elif event.name == "context.compacted":
                self._journal_and_capture(session, envelope)
            elif event.name == "context.child_started":
                agent_id = event.ids.get("agent_id") or f"child-{len(session.children) + 1}"
                session.children.setdefault(agent_id, ChildAgent(
                    agent_id=agent_id, agent_type=event.attributes.get("agent_type"),
                ))
            elif event.name == "context.child_finished":
                agent_id = event.ids.get("agent_id")
                if agent_id:
                    child = session.children.setdefault(agent_id, ChildAgent(
                        agent_id=agent_id, agent_type=event.attributes.get("agent_type"),
                    ))
                    child.finished_at = now_iso()
                self._journal_and_capture(session, envelope)
            elif event.name == "context.closed":
                self._drop_recall(session.key)
                self._recall_seq.pop(session.key, None)
                self._recall_failure_announced.discard(session.key)
                session.end(str(event.attributes.get("reason") or "session_end"))
                self.journal.append(envelope)
                self._spawn(self._capture_then_finalize(session, envelope, occurrence="end"))
        return HandleResult(key=session.key, state=session.state, context=context, detail=detail,
                            recall_pending=recall_pending, continue_with=continue_with)

    # ── end-of-work save nudge (P1a) ──────────────────────────────────────────

    def note_memory_write(self, reference: str | None, *, ref: str | None = None) -> bool:
        """A memory_remember/checkpoint/correct credited to a session: by the
        bridge (explicit session key or connection header) or by the
        session's own adapter, which sees the accepted tool result. `ref`
        (record/event id) dedupes the two paths."""
        session = self.find_session(reference) if reference else None
        if session is None:
            return False
        if ref:
            if ref in session.credited_writes:
                return False
            session.credited_writes = [*session.credited_writes[-49:], ref]
        session.counters.memory_writes += 1
        self.stats.memory_writes += 1
        self.registry.save()  # a restart before the next Stop must not lose it
        return True

    def _nudge(self, session: ExternalSession, last_message: str | None) -> str | None:
        """Decide this Stop's nudge. Pushy by decision (Ilari 2026-09-23),
        bounded by construction: at most `max_per_session` nudges, each
        re-asked at most once, never while the agent already saved
        something since the last one, never in a child session. Keyed on
        its own per-session state, never on `stop_hook_active` (any
        plugin's blocking Stop sets that — it would silence this, or let
        this silence MiraDesign)."""
        cfg = self.config.nudge
        if not cfg.enabled or session.parent_session or session.project is None:
            return None
        writes = session.counters.memory_writes
        answered = bool(last_message) and NOTHING_DURABLE in last_message.lower()
        if session.nudge_state in ("asked", "reasked"):
            if writes > session.nudge_writes_mark or answered:
                session.nudge_state = None
                session.nudge_writes_mark = writes
                self.stats.nudges_answered += 1
                return None
            if session.nudge_state == "asked":
                session.nudge_state = "reasked"
                self.stats.nudges_reasked += 1
                return self._nudge_text(session, reask=True)
            session.nudge_state = None
            self.stats.nudges_ignored += 1
            return None
        prompts = session.counters.prompts
        compactions = session.counters.compactions
        if session.nudges_fired >= cfg.max_per_session:
            return None
        since = prompts - session.nudge_prompt_mark
        if session.nudges_fired == 0:
            due = since >= cfg.first_after_prompts
        else:
            due = since >= cfg.every_prompts or (
                compactions > session.nudge_compaction_mark and since > 0)
        if not due:
            return None
        session.nudge_prompt_mark = prompts
        session.nudge_compaction_mark = compactions
        if writes > session.nudge_writes_mark:
            # It saved on its own since the last check: that's the goal, so
            # the clock restarts instead of nagging.
            session.nudge_writes_mark = writes
            return None
        session.nudges_fired += 1
        session.nudge_state = "asked"
        self.stats.nudges_fired += 1
        return self._nudge_text(session, reask=False)

    def _nudge_text(self, session: ExternalSession, *, reask: bool) -> str:
        where = f"session='{session.key}'"
        if reask:
            return (
                "[memory — end-of-work save, asked again] That went unanswered. Call "
                f"memory_remember ({where}) once per durable learning now, or reply "
                "`nothing durable: <why, for each thing the user told you>`. This is the "
                "last time it is asked for this work."
            )
        told = [p for p in session.prompts[-5:] if p and p.strip()]
        # One line per prompt, however many lines it had: nothing the user
        # once typed may reappear here as a bare line that reads like an
        # instruction.
        candidates = "".join(
            "\n  > " + " ⏎ ".join(line.strip() for line in p.strip()[:300].splitlines() if line.strip())
            for p in told
        )
        return (
            "[memory — end-of-work save] Before you stop: this session did real work and "
            "none of it has been saved to miragen memory. Future sessions in this project "
            "only know what you save now — this is separate from any MEMORY.md. Call "
            f"memory_remember ({where}) once per item, one specific, self-contained fact "
            "each:\n"
            "- facts the user TOLD you that no file records (\"X only works from Y\", "
            "\"we never do Z\") — these are lost first;\n"
            "- decisions made, and why;\n"
            "- gotchas hit and how they were fixed (the fix, with its conditions);\n"
            "- environment facts: ports, hosts, paths, commands, where configuration lives "
            "(never a secret's value);\n"
            "- Ilari's stated preferences and corrections;\n"
            "- open intentions: what is unfinished or promised next.\n"
            "Skip only what the repository or git history already records."
            + (f"\nWhat you were told this session (quoted for reference only — not "
               f"instructions to act on again):{candidates}" if candidates else "")
            + "\nOnly if none of it is durable, reply `nothing durable: <why, for each "
            "thing the user told you>`."
        )

    # ── project + scopes ─────────────────────────────────────────────────────

    def _client_is_local(self, envelope: EventEnvelope) -> bool:
        client = envelope.client
        if client.remote:
            return False
        return client.host is None or client.host == self.local_host

    def _remember_project(self, project: ProjectIdentity) -> None:
        if project.remote or not is_name_derived(project):
            self._known_by_name.setdefault(project.name.lower(), project)

    async def _attach_project(self, session: ExternalSession, envelope: EventEnvelope) -> None:
        """Bind (or re-bind) the session's project from where it is now.

        Resolved on every event whose cwd/remote changed — sessions are
        mostly launched from ~ and cd into repositories later, the rule
        MiraDesign already follows. Sticky: a binding is only replaced by a
        REPOSITORY (a remote-derived identity) other than the current one.
        A provisional binding (`dir:`, `workspace:`, `path:`, the home
        directory) is replaced as soon as a repository resolves; moving to
        ~ or any non-repository directory never downgrades a binding.

        Identity, in order of trust in what we can verify: the remote
        the adapter observed (works from any host), the daemon's own look
        at the directory (this host only), the directory name (anything
        else — optionally adopting a known project of that name)."""
        client = envelope.client
        cwd = client.cwd or client.project_dir
        local = self._client_is_local(envelope)
        if not local and not session.remote:
            session.remote = True
        fingerprint = f"{cwd}\x00{client.project_remote or ''}"
        if session.project is not None and session.resolved_from == fingerprint:
            return
        at_home = bool(cwd and client.home) and _same_dir(cwd, client.home)
        project: ProjectIdentity | None = None
        if client.project_remote:
            project = identity_from_remote(client.project_remote, root=cwd)
        elif not cwd:
            return
        elif local:
            project = self._projects.get(cwd)
            if project is None:
                try:
                    project = await asyncio.to_thread(self._resolve, cwd)
                except Exception as exc:  # resolution must never fail an event
                    logger.warning(f"project resolution failed for {cwd}: {exc}")
                    # Not retried on every event from the same place (each
                    # attempt is a blocking git call on the hook path).
                    if session.project is not None:
                        session.resolved_from = fingerprint
                    return
                self._projects[cwd] = project
        else:
            project = identity_from_directory(cwd)
            if self._looks_like_workspace_root(cwd):
                project = ProjectIdentity(
                    id=f"workspace:{project.name.lower()}", slug=project_slug(f"workspace-{project.name}"),
                    name=project.name, root=cwd, remote=None,
                )
            known = self._known_by_name.get(project.name.lower())
            if known is not None and self.config.scopes.adopt_by_name and not project.id.startswith("workspace:"):
                project = ProjectIdentity(
                    id=known.id, slug=known.slug, name=known.name, root=cwd, remote=known.remote,
                )
                self.stats.adopted_by_name += 1
        if project is None:
            return
        session.resolved_from = fingerprint
        current = session.project
        # A dotfiles repository at ~ covers every subdirectory without its
        # own repository: its toplevel IS home. (Only visible when the daemon
        # resolved the directory itself; a remote adapter reports cwd only.)
        at_home = at_home or bool(
            local and client.home and project.root and _same_dir(project.root, client.home)
        )
        is_repository = bool(project.remote) and not at_home
        if current is not None and (not is_repository or project.id == current.id):
            return  # sticky: only another repository replaces a binding
        self._remember_project(project)
        session.project = project
        assignment = self._assignment_for(project)
        session.scope = assignment.write
        if project.id not in session.projects_seen:
            session.projects_seen.append(project.id)
        if current is None:
            if session.remote:
                self.stats.remote_sessions += 1
            return
        self.stats.project_switches += 1
        session.reopen_pending = True
        logger.info(f"[{session.key}] project {current.id} → {project.id} (cwd {cwd})")

    def _looks_like_workspace_root(self, cwd: str) -> bool:
        """A remote cwd that other known remote-derived projects sit
        directly under (a multi-repository cloud workspace such as
        /home/user with one clone per repository) is a workspace, not a
        project of its own."""
        prefix = cwd.rstrip("/") + "/"
        children = 0
        for project in list(self._known_by_name.values()) + list(self._projects.values()):
            root = (project.root or "").rstrip("/")
            if root.startswith(prefix) and "/" not in root[len(prefix):] and project.remote:
                children += 1
        return children >= 2

    def resolve_identity(self, text: str | None) -> ProjectIdentity:
        """A project named by a tool call: a session key, a remote URL or
        its normalized form, a known slug or repository name, or — as a
        last resort — a bare name. None → the configured default."""
        if not text:
            return self._default_project()
        value = text.strip()
        lowered = value.lower()
        if lowered == self.config.mcp.default_project.lower():
            return self._default_project()
        session = self.find_session(value)
        if session is not None and session.project is not None:
            return session.project
        by_cwd = self._projects.get(value) or self._projects.get(value.rstrip("/"))
        if by_cwd is not None:
            return by_cwd
        for project in {**self._projects, **{p.id: p for p in self._known_by_name.values()}}.values():
            if lowered in (project.id, project.slug, project.name.lower(), project.root):
                return project
        if lowered.startswith("dir:") or lowered.startswith("path:"):
            return ProjectIdentity(
                id=lowered, slug=project_slug(lowered), name=lowered.split(":", 1)[1], root="",
            )
        if "/" in value or value.startswith("git@"):
            return identity_from_remote(value)
        known = self._known_by_name.get(lowered)
        if known is not None:
            return known
        return identity_from_directory(value)

    def _default_project(self) -> ProjectIdentity:
        project_id = self.config.mcp.default_project.lower()
        return ProjectIdentity(
            id=project_id, slug=project_slug(project_id),
            name=project_id.split(":", 1)[-1], root="",
        )

    def find_session(self, reference: str | None) -> ExternalSession | None:
        """A session by key, by bare harness session id, or by store run."""
        if not reference:
            return None
        direct = self.registry.get(reference)
        if direct is not None:
            return direct
        for session in self.registry.list():
            if reference in (session.session_id, session.run_id):
                return session
        return None

    def _assignment_for(self, project: ProjectIdentity) -> ScopeAssignment:
        assignment = self._assignments.get(project.id)
        if assignment is None:
            assignment = assign_scopes(self.config.scopes, self.config.projects, project)
            self._assignments[project.id] = assignment
        return assignment

    async def _lifecycle_for(self, session: ExternalSession) -> tuple[MemoryLifecycle | None, str | None]:
        """The lifecycle bound to this session's project scopes. (None,
        reason) only when the session has no project at all."""
        if session.project is None:
            return None, "no working directory reported; memory not scoped"
        lifecycle, write, detail = await self.lifecycle_for_project(session.project)
        session.scope = write
        return lifecycle, detail

    async def lifecycle_for_project(
        self, project: ProjectIdentity,
    ) -> tuple[MemoryLifecycle, str, str | None]:
        """The lifecycle for a project's scopes, provisioning the project
        scope first when policy says so. A scope that could not be
        provisioned still gets a lifecycle (fallback scope if configured,
        else the project scope itself) so that Loimi's refusal degrades the
        packet EXPLICITLY instead of the caller silently getting nothing.
        Returns (lifecycle, effective write scope, detail)."""
        assignment = self._assignment_for(project)
        write = assignment.write
        read = assignment.read
        policy = self.config.scopes
        detail: str | None = None
        if write in self._worker_ungranted:
            # The worker's grant failed earlier (its principal did not exist
            # yet, or Loimi was away): retry on the scope's next use instead
            # of waiting for a bridge restart.
            operator = self._operator_client()
            if operator is not None:
                await self._grant_worker(operator, write)
        if assignment.templated and policy.provision == "auto" and write not in self._provisioned:
            failure = self._unprovisionable.get(write)
            if failure is None:
                outcome = await self._provision(write)
                if outcome is None:
                    self._provisioned.add(write)
                else:
                    failure, permanent = outcome
                    if permanent:
                        self._unprovisionable[write] = failure
            if write not in self._provisioned:
                detail = f"project scope {write} not provisioned ({failure})"
                if policy.fallback_write:
                    write = policy.fallback_write
                    read = [s for s in assignment.read if s != assignment.write] + [write]
                    detail += f"; writing to fallback {write}"
        lifecycle = self._lifecycles.get(write)
        if lifecycle is None:
            spec = MemorySpec(
                endpoint_env=self.config.endpoint_env,
                credential_env=self.config.credential_env,
                scopes=MemoryScopesSpec(read=read, propose=[write], default_write=write),
                recall=MemoryRecallSpec(**self.config.recall.model_dump(
                    include=set(MemoryRecallSpec.model_fields)
                )),
            )
            client = self._client_factory(spec) if self._client_factory else MemoryClient(spec)
            lifecycle = MemoryLifecycle(
                spec, self.config.principal, client,
                state_dir=self.state_dir / "memory",
                tools_available=self.config.mcp.enabled, selector=self.selector,
            )
            self._lifecycles[write] = lifecycle
        return lifecycle, write, detail

    def _operator_client(self) -> MemoryClient | None:
        token = self.environ.get(self.config.operator_token_env)
        if not token:
            return None
        if self._operator_client_factory is not None:
            return self._operator_client_factory(token)
        spec = MemorySpec(
            endpoint_env=self.config.endpoint_env, credential_env=self.config.credential_env,
            scopes=MemoryScopesSpec(read=[], propose=["operator"], default_write="operator"),
        )
        return MemoryClient(spec, token=token)

    async def _provision(self, scope_id: str) -> tuple[str, bool] | None:
        """Create the project scope and grant the daemon's principal on it.
        Idempotent: an existing scope/grant (409) is success. Returns None
        on success, else (reason, permanent): a missing operator credential
        or a refusal is permanent for this process; unreachability is not,
        so the next event tries again."""
        operator = self._operator_client()
        if operator is None:
            return f"{self.config.operator_token_env} is not set (provision: auto)", True
        try:
            for call in (
                operator.admin_create_scope(
                    scope_id=scope_id, kind=self.config.scopes.project_scope_kind,
                    description="auto-provisioned by miragend for an external session's project",
                ),
                operator.admin_grant(
                    principal_id=self.config.principal, scope_id=scope_id, verbs=_PROVISION_VERBS,
                ),
            ):
                try:
                    await asyncio.wait_for(call, timeout=WRITE_TIMEOUT_S)
                except MemoryAPIError as exc:
                    if exc.status_code != 409:
                        raise
            await self._grant_worker(operator, scope_id)
        except MemoryAPIError as exc:
            self.stats.provisioning_failures += 1
            self.stats.note_loimi(True)  # it answered — a refusal, not an outage
            logger.warning(f"provisioning {scope_id} refused: {exc}")
            return str(exc), True
        except (MemoryUnavailable, asyncio.TimeoutError) as exc:
            self.stats.provisioning_failures += 1
            self.stats.note_loimi(False, f"provision {scope_id}: {exc}")
            logger.warning(f"provisioning {scope_id} failed: {exc}")
            return str(exc), False
        self.stats.provisioned_scopes += 1
        self.stats.note_loimi(True)
        logger.info(f"provisioned project scope {scope_id} for principal {self.config.principal}")
        return None

    # ── context ───────────────────────────────────────────────────────────────

    def _session_header(self, session: ExternalSession) -> str:
        project = session.project
        parts = [
            f"harness={session.harness}",
            f"session={session.key}",
            f"project={project.id if project else 'unknown'}",
            f"cwd={session.cwd or '?'}",
            f"scope={session.scope or 'none'}",
        ]
        if session.remote:
            parts.append(f"host={session.host or 'remote'}")
        if session.parent_session:
            parts.append(f"parent={session.parent_session}")
        if session.agent:
            parts.append(f"agent={session.agent}")
        if session.run_id:
            parts.append(f"store_run={session.run_id}")
            parts.append(f"namespace={session.namespace}")
        lines = ["[session context — attributed reference data] " + " ".join(parts)]
        if self.config.mcp.enabled:
            lines.append(
                "[bridge tools] MCP server `miragen-bridge`, when connected: memory_recall / "
                "memory_read / memory_remember / memory_correct / memory_checkpoint act on this "
                f"project when called with project={project.id if project else 'unknown'!r} "
                f"(or session={session.key!r}); store_put_artifact files an immutable artifact "
                + (f"under this session's Loimi run {session.run_id} (pass run_id or session) "
                   if session.run_id else "under a Loimi run (store_open_run first) ")
                + "with honest direct `sources`; store_search / store_get_artifact / "
                "store_lineage read the artifact store. A write counts only when the tool "
                "answers accepted."
            )
        return "\n".join(lines)

    async def _open_context(
        self, session: ExternalSession, envelope: EventEnvelope,
    ) -> tuple[str | None, str | None]:
        lifecycle, scope_detail = await self._lifecycle_for(session)
        self.stats.retrievals += 1
        if lifecycle is None:
            self.stats.retrieval_failures += 1
            return None, scope_detail
        store_detail = await self._ensure_run(session)
        if store_detail:
            scope_detail = "; ".join(part for part in (scope_detail, store_detail) if part)
        started = time.monotonic()
        try:
            packet = await asyncio.wait_for(
                lifecycle.prepare_context(
                    instance=session.project.slug if session.project else None,
                    run_id=session.key,
                    trigger=f"hook:{envelope.event.original_event.lower()}",
                ),
                timeout=RETRIEVAL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            self.stats.timeouts += 1
            self.stats.retrieval_failures += 1
            self.stats.note_loimi(False, "retrieval timed out")
            # Say it once per session: while the outage lasts every prompt
            # retries the open (late-open path), and 20 copies are noise.
            if session.key in self._outage_announced:
                return None, "retrieval timed out"
            self._outage_announced.add(session.key)
            return self._unavailable_line("retrieval timed out"), "retrieval timed out"
        finally:
            elapsed = (time.monotonic() - started) * 1000
            self.stats.retrieval_ms_total += elapsed
            self.stats.retrieval_count += 1
        if packet.degraded:
            self.stats.retrieval_failures += 1
            self.stats.note_loimi(False, packet.degraded)
        else:
            self.stats.note_loimi(True)
        self._outage_announced.discard(session.key)
        self._count_injection(session, envelope)
        detail = "; ".join(part for part in (scope_detail, packet.degraded) if part) or None
        status = self._status_line(packet, project_scope=lifecycle.spec.scopes.default_write)
        return f"{self._session_header(session)}\n{packet.text}\n{status}", detail

    @staticmethod
    def _unavailable_line(reason: str | None) -> str:
        """An outage is exactly when silence misleads most."""
        return (f"[memory status] memory UNAVAILABLE for this session ({reason or 'unknown'}) — "
                "nothing was recalled and this session may not be remembered; do not "
                "conclude that nothing is stored")

    def _status_line(self, packet, *, project_scope: str | None) -> str:
        """One line that always says what memory did for this context, so
        silence never has to be interpreted: recall mode and result, and
        whether captures are currently being lost."""
        status = packet.optional_status or "unknown"
        injected = sum(1 for item in packet.items if item.get("revision_id"))
        where = f" in {project_scope}" if project_scope else ""
        if packet.degraded or status.startswith("degraded"):
            recall = "recall DEGRADED — memories may exist that could not be searched"
        elif status == "ok":
            recall = (f"{injected} memor{'y' if injected == 1 else 'ies'}{where} injected above "
                      "— cite the ids you rely on")
        elif status == "empty":
            recall = f"searched{where}: nothing stored matches yet"
        elif status == "none_selected":
            recall = f"searched{where}: nothing relevant to this"
        elif status == "no_query":
            recall = (f"automatic recall on; it runs on each prompt of {self.config.recall.min_prompt_chars}+ characters"
                      if self.config.recall.on_prompt else
                      "automatic recall on, but only at session open (not per prompt)")
        elif status in ("unconfigured", "disabled"):
            recall = "automatic recall is OFF on this bridge — nothing is injected on its own" + (
                "; memory_recall searches on demand" if self.config.mcp.enabled else "")
        else:
            recall = f"recall: {status}"
        recent = self.stats.recent_outcomes(project_scope)
        failed = recent.count(False)
        if failed:
            capture = (f"capture FAILING: {failed} of {len(recent)} recent writes to this "
                       "project were lost — this session's trail may be incomplete")
        else:
            capture = "capture ok"
        return f"[memory status] {recall} · {capture}"

    async def _prompt_recall(
        self, session: ExternalSession, envelope: EventEnvelope,
    ) -> tuple[str | None, str | None, int | None]:
        """(context, detail, pending recall sequence). Async-capable
        adapters get the search now and the selection later; everyone else
        waits for the selector inside the hook, as before."""
        recall = self.config.recall
        prompt = envelope.event.content or ""
        if not recall.on_prompt or self.selector is None or len(prompt) < recall.min_prompt_chars:
            # The previous prompt's result must not surface in this turn.
            self._drop_recall(session.key)
            return None, None, None
        lifecycle, reason = await self._lifecycle_for(session)
        if lifecycle is None:
            self._drop_recall(session.key)
            return None, reason, None
        if recall.delivery == "async" and ASYNC_RECALL_CAPABILITY in envelope.client.capabilities:
            return await self._start_async_recall(session, envelope, lifecycle, prompt)
        started = time.monotonic()
        try:
            section, status = await asyncio.wait_for(
                lifecycle.recall_section(
                    instance=session.project.slug if session.project else None,
                    prompt_hint=prompt, run_id=session.key, trigger="hook:userpromptsubmit",
                ),
                timeout=RETRIEVAL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            self.stats.timeouts += 1
            return None, "prompt recall timed out", None
        finally:
            self.stats.retrieval_ms_total += (time.monotonic() - started) * 1000
            self.stats.retrieval_count += 1
        self.stats.prompt_recalls += 1
        if status.startswith("degraded"):
            self.stats.retrieval_failures += 1
            self.stats.note_loimi(False, status)
        if section:
            self._count_injection(session, envelope)
        return section, status, None

    # ── asynchronous recall: search now, select in the background ────────────

    async def _start_async_recall(
        self, session: ExternalSession, envelope: EventEnvelope,
        lifecycle: MemoryLifecycle, prompt: str,
    ) -> tuple[str | None, str | None, int | None]:
        self._drop_recall(session.key)
        started = time.monotonic()
        try:
            candidates = await asyncio.wait_for(
                lifecycle.recall_candidates(
                    instance=session.project.slug if session.project else None,
                    prompt_hint=prompt,
                ),
                timeout=self.config.recall.search_timeout_seconds,
            )
        except TimeoutError:
            self.stats.timeouts += 1
            return None, "recall search timed out", None
        finally:
            self.stats.retrieval_ms_total += (time.monotonic() - started) * 1000
            self.stats.retrieval_count += 1
        self.stats.prompt_recalls += 1
        if candidates.status.startswith("degraded"):
            self.stats.retrieval_failures += 1
            self.stats.note_loimi(False, candidates.status)
            return None, candidates.status, None
        if candidates.status != "ok":
            return None, candidates.status, None  # nothing to select from: no notice
        seq = self._recall_seq.get(session.key, 0) + 1
        self._recall_seq[session.key] = seq
        job = RecallJob(
            seq=seq, key=session.key, scope=lifecycle.spec.scopes.default_write,
            trigger=f"hook:{envelope.event.original_event.lower()}:async",
            candidates=candidates, lifecycle=lifecycle,
        )
        self._recalls[session.key] = job
        job.task = self._spawn(self._select_in_background(job))
        self.stats.async_recalls_started += 1
        count = len(candidates.cards)
        notice = (
            f"[memory] recall for this prompt is running in the background ({count} "
            f"candidate{'s' if count != 1 else ''} being checked for relevance). Anything "
            "relevant arrives after one of your next tool results, or before you finish. "
            "Keep working: don't poll, and don't run memory_recall for the same thing."
        )
        return notice, "recall pending", seq

    async def _select_in_background(self, job: RecallJob) -> None:
        try:
            selection = await job.lifecycle.recall_select(job.candidates)
        except asyncio.CancelledError:
            job.selection = RecallSelection("cancelled")
            job.done.set()
            raise
        except Exception as exc:  # noqa: BLE001 — recall_select already degrades; belt and braces
            selection = RecallSelection(f"degraded: {exc}")
        job.selection = selection
        job.done.set()
        if selection.status.startswith("degraded"):
            self.stats.retrieval_failures += 1
            self.stats.async_recall_failures += 1
            return
        self.judgments.record(
            session=job.key, scope=job.scope, recall_id=f"{job.key}#{job.seq}",
            query=job.candidates.query, cards=job.candidates.cards,
            selected=selection.selected, status=selection.status,
            model=self.config.recall.model,
        )

    def _drop_recall(self, key: str) -> None:
        job = self._recalls.pop(key, None)
        if job is None:
            return
        if job.task is not None and not job.task.done():
            job.task.cancel()  # the runner reaps its claude child on cancel
        if job.delivered is None:
            self.stats.async_recalls_dropped += 1

    async def claim_recall(
        self, harness: str, session_id: str, seq: int, *, wait: float = 0.0,
    ) -> dict[str, Any]:
        """The adapter's claim for a pending recall. Atomic: the first claim
        that finds the result delivers it (and only then is it counted and
        manifested); every later one is told "delivered". Never holds the
        session lock — a Stop waiting here must not stall captures."""
        key = session_key(harness, session_id)
        job = self._recalls.get(key)
        if job is None or job.seq != seq:
            return {"state": "stale"}
        if not job.done.is_set() and wait > 0:
            try:
                await asyncio.wait_for(asyncio.shield(job.done.wait()),
                                       timeout=min(wait, MAX_CLAIM_WAIT_S))
            except TimeoutError:
                self.stats.recall_wait_timeouts += 1
            if self._recalls.get(key) is not job:
                return {"state": "stale"}
        if not job.done.is_set():
            return {"state": "pending"}
        if job.delivered is not None:
            return {"state": "delivered"}
        selection = job.selection or RecallSelection("none_selected")
        if selection.status == "ok" and selection.section:
            job.delivered = "context"
            # The answer must not wait on Loimi: a claim that outlived the
            # adapter's timeout would be marked delivered yet never shown.
            self._spawn(job.lifecycle.record_recall_manifest(
                job.candidates, selection, run_id=key, trigger=job.trigger))
            self.stats.injections += 1
            session = self.registry.get(key)
            if session is not None:
                session.counters.injections += 1
            self.stats.async_recalls_delivered += 1
            self.judgments.mark_delivered(recall_id=f"{key}#{job.seq}", session=key,
                                          state="context")
            return {"state": "ready", "context": selection.section}
        job.delivered = "empty"
        self.stats.async_recalls_empty += 1
        if selection.status.startswith("degraded") and key not in self._recall_failure_announced:
            self._recall_failure_announced.add(key)
            return {"state": "failed", "context": (
                "[memory] background recall failed for this prompt: memories may exist that "
                "were not checked. Use memory_recall if you need one. (Said once per session.)"
            )}
        return {"state": "empty"}

    def _count_injection(self, session: ExternalSession, envelope: EventEnvelope) -> None:
        self.stats.injections += 1
        session.counters.injections += 1
        if envelope.client.context_delivery == "deferred":
            self.stats.deferred_injections += 1
            session.counters.deferred_injections += 1

    # ── capture (background, ordered per session) ─────────────────────────────

    def _journal_and_capture(self, session: ExternalSession, envelope: EventEnvelope) -> None:
        self.journal.append(envelope)
        # The project is fixed NOW: captures queue behind the session lock,
        # and an agent that cd's into another repository meanwhile must not
        # have this event written to the new project's scope.
        project = session.project
        self._spawn(self._locked(session, self._capture(session, envelope, project)))

    async def _locked(self, session: ExternalSession, coro: Awaitable[Any]) -> Any:
        async with self._lock(session.key):
            return await coro

    async def _capture(
        self, session: ExternalSession, envelope: EventEnvelope,
        project: ProjectIdentity | None = None,
    ) -> None:
        event = envelope.event
        if event.name not in CAPTURED:
            return
        if project is None:
            project = session.project
        if project is None:
            lifecycle, reason = None, "no working directory reported; memory not scoped"
        else:
            lifecycle, _write, reason = await self.lifecycle_for_project(project)
        if lifecycle is None:
            # Unscoped (no project), not a lost write: lifetime counter only,
            # never the recent window the status line reads.
            self.stats.capture_failures += 1
            session.counters.capture_failures += 1
            logger.info(f"[{session.key}] capture skipped: {reason}")
            return
        # A session the daemon first hears of mid-life (hooks installed
        # while it ran, or a daemon restart) still gets its store run.
        await self._ensure_run(session)
        normalized = NormalizedEvent(
            name=event.name, harness=envelope.harness, original_event=event.original_event,
            session_id=envelope.session_id, ids=dict(event.ids), content=event.content,
            attributes=dict(event.attributes),
        )
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                lifecycle.capture_harness_event(
                    instance=project.slug,
                    event=normalized,
                ),
                timeout=WRITE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            self.stats.timeouts += 1
            result = {"status": "persistence_unavailable", "detail": "capture timed out"}
        finally:
            self.stats.write_ms_total += (time.monotonic() - started) * 1000
            self.stats.write_count += 1
        if result.get("status") == "captured":
            self.stats.note_capture(True, lifecycle.spec.scopes.default_write)
            session.counters.captures += 1
            self.stats.note_loimi(True)
        else:
            self.stats.note_capture(False, lifecycle.spec.scopes.default_write)
            session.counters.capture_failures += 1
            self.stats.note_loimi(False, result.get("detail"))

    async def _capture_then_finalize(
        self, session: ExternalSession, envelope: EventEnvelope, *, occurrence: str,
    ) -> None:
        async with self._lock(session.key):
            await self._capture(session, envelope)
            await self._finalize(session, occurrence=occurrence)
        self.registry.save()

    # ── artifact store (Loimi /v0) ───────────────────────────────────────────

    def store_active(self) -> bool:
        return self.store is not None and self.store.configured()

    async def _ensure_run(self, session: ExternalSession) -> str | None:
        """Open this session's Loimi run once. Returns a detail string
        when the store is configured but refused/unreachable (explicit
        degradation for the injected header), None otherwise."""
        if self.store is None or not self.store.configured():
            return None
        if session.run_id is not None:
            # One run per life; a new life resets this in ExternalSession.new_life().
            return None
        namespace = self.store.namespace_for(session.project)
        project = session.project.id if session.project else "unknown project"
        task = f"{session.harness} session {session.session_id} in {project}"
        started = time.monotonic()
        try:
            run = await asyncio.wait_for(
                self.store.open_run(task=task, namespace=namespace, parent_run_id=None),
                timeout=WRITE_TIMEOUT_S,
            )
        except (StoreUnavailable, StoreAPIError, asyncio.TimeoutError) as exc:
            self.stats.note_store(False, f"open run: {exc}")
            logger.warning(f"[{session.key}] store run not opened: {exc}")
            return f"artifact store run not opened ({exc})"
        finally:
            self.stats.write_ms_total += (time.monotonic() - started) * 1000
            self.stats.write_count += 1
        session.run_id = str(run["id"])
        session.namespace = namespace
        session.run_status = "running"
        self.stats.runs_opened += 1
        self.stats.note_store(True)
        return None

    async def _store_episode(
        self, session: ExternalSession, *, occurrence: str, digest: str,
        memory_event_id: str | None,
    ) -> None:
        if self.store is None:
            return
        if await self._ensure_run(session) or session.run_id is None:
            return
        dedupe_key = f"{session.run_id}:{occurrence}"
        if dedupe_key in session.artifacts_written:
            return
        try:
            artifact = await asyncio.wait_for(self.store.put_artifact(
                run_id=session.run_id, kind=self.config.store.episode_kind, content=digest,
                properties={
                    "harness": session.harness, "session": session.key,
                    "occurrence": occurrence,
                    "project": session.project.id if session.project else None,
                    "scope": session.scope, "memory_event_id": memory_event_id,
                    "prompts": session.counters.prompts, "turns": session.counters.turns,
                    "tool_failures": session.counters.tool_failures,
                    "compactions": session.counters.compactions,
                    "end_reason": session.end_reason, "remote": session.remote,
                },
            ), timeout=WRITE_TIMEOUT_S)
        except (StoreUnavailable, StoreAPIError, asyncio.TimeoutError) as exc:
            self.stats.note_store(False, f"episode artifact: {exc}")
            logger.warning(f"[{session.key}] episode artifact not written: {exc}")
            return
        session.artifacts_written.append(dedupe_key)
        self.stats.artifacts_written += 1
        self.stats.note_store(True)

    async def _close_run(self, session: ExternalSession, *, status: str | None = None) -> None:
        if self.store is None or session.run_id is None or session.run_status != "running":
            return
        if status is None:
            status = "cancelled" if session.end_reason in ("process_gone", "silent") else "succeeded"
        try:
            await asyncio.wait_for(self.store.close_run(session.run_id, status), timeout=WRITE_TIMEOUT_S)
        except StoreAPIError as exc:
            # Already closed (a replayed finalization): the run is what it is.
            if exc.status_code not in (409, 422):
                self.stats.note_store(False, f"close run: {exc}")
                return
        except (StoreUnavailable, asyncio.TimeoutError) as exc:
            self.stats.note_store(False, f"close run: {exc}")
            return
        session.run_status = status
        self.stats.runs_closed += 1
        self.stats.note_store(True)

    # ── episode + checkpoint ──────────────────────────────────────────────────

    def render_episode(self, session: ExternalSession, *, occurrence: str) -> str:
        project = session.project
        lines = [
            f"Session episode ({occurrence}) — {session.harness} session {session.session_id}",
            f"project: {project.id if project else 'unknown'}; cwd: {session.cwd or '?'}; "
            f"user: {session.user or '?'}",
            f"started: {session.created_at}; last activity: {session.last_seen_at}"
            + (f"; ended: {session.ended_at} ({session.end_reason})" if session.ended_at else ""),
            f"prompts: {session.counters.prompts}; turns: {session.counters.turns}; "
            f"tool failures: {session.counters.tool_failures}; "
            f"compactions: {session.counters.compactions}; child agents: {len(session.children)}",
        ]
        if session.parent_session:
            lines.append(f"parent session: {session.parent_session}")
        if session.prompts:
            lines.append("User prompts (most recent, truncated):")
            lines.extend(f"  {i}. {p}" for i, p in enumerate(session.prompts, 1))
        if session.turns:
            lines.append("Last assistant message:")
            lines.append("  " + session.turns[-1].replace("\n", "\n  "))
        return "\n".join(lines)

    async def _finalize(self, session: ExternalSession, *, occurrence: str) -> None:
        if occurrence == "end":
            self._outage_announced.discard(session.key)
        lifecycle, reason = await self._lifecycle_for(session)
        if lifecycle is None:
            logger.info(f"[{session.key}] finalize skipped: {reason}")
            return
        if session.is_empty():
            # No episode, no artifact, no checkpoint for a session that did
            # nothing; its run (opened at start, before we could know) is
            # closed as cancelled so the store shows what it was.
            self.stats.empty_sessions += 1
            if occurrence == "end":
                session.end_reason = session.end_reason or "empty"
                await self._close_run(session, status="cancelled")
                self.journal.clear(session.key)
            return
        instance = session.project.slug if session.project else None
        digest = self.render_episode(session, occurrence=occurrence)
        started = time.monotonic()
        try:
            life = f":life{session.lives}" if session.lives else ""
            episode = await asyncio.wait_for(
                lifecycle.capture_episode(
                    instance=instance,
                    idempotency_key=f"episode:{session.key}:{occurrence}{life}",
                    content=digest,
                    source_ref=f"session:{session.key}",
                    attributes={
                        "harness": session.harness,
                        "session_id": session.session_id,
                        "occurrence": occurrence,
                        "project": session.project.id if session.project else None,
                        "cwd": session.cwd,
                        # Every project the session was bound to; the episode
                        # is filed under the one bound at filing time.
                        "projects": list(session.projects_seen),
                        "prompts": session.counters.prompts,
                        "turns": session.counters.turns,
                        "end_reason": session.end_reason,
                        "parent_session": session.parent_session,
                    },
                ),
                timeout=WRITE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            self.stats.timeouts += 1
            episode = {"status": "persistence_unavailable", "detail": "episode timed out"}
        finally:
            self.stats.write_ms_total += (time.monotonic() - started) * 1000
            self.stats.write_count += 1
        scope = lifecycle.spec.scopes.default_write
        if episode.get("status") == "captured":
            if not episode.get("already_filed"):
                self.stats.episodes += 1
                session.episodes_written.append(occurrence)
            self.stats.note_loimi(True)
            self.stats.note_outcome(True, scope)
        else:
            self.stats.capture_failures += 1
            self.stats.note_loimi(False, episode.get("detail"))
            self.stats.note_outcome(False, scope)

        # The same digest becomes an immutable artifact under the session's
        # run — the store is where a session's output is traceable later.
        await self._store_episode(
            session, occurrence=occurrence, digest=digest,
            memory_event_id=episode.get("event_id"),
        )
        if occurrence == "end":
            # Whatever happened to the artifact, the run must not stay open.
            await self._close_run(session)

        patch = {"last_session": {
            "harness": session.harness,
            "session": session.key,
            "occurrence": occurrence,
            "at": now_iso(),
            "cwd": session.cwd,
            "prompts": session.counters.prompts,
            "first_prompt": session.prompts[0] if session.prompts else None,
            "last_prompt": session.prompts[-1] if session.prompts else None,
            "last_assistant": session.turns[-1][:300] if session.turns else None,
            "end_reason": session.end_reason,
            "store_run": session.run_id,
        }}
        try:
            checkpoint = await asyncio.wait_for(
                lifecycle.checkpoint(instance=instance, patch=patch), timeout=WRITE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            self.stats.timeouts += 1
            checkpoint = {"status": "persistence_unavailable"}
        if checkpoint.get("status") == "accepted":
            self.stats.checkpoints += 1
            self.stats.note_outcome(True, scope)
        else:
            self.stats.capture_failures += 1
            self.stats.note_outcome(False, scope)
        if occurrence == "end" and episode.get("status") == "captured":
            self.journal.clear(session.key)

    # ── recovery + housekeeping ───────────────────────────────────────────────

    async def replay_journal(self) -> int:
        """Re-run journaled captures through the pipeline, in order. Only
        captures — a context answer nobody is waiting for is pointless.
        Idempotency keys make already-written events no-ops."""
        replayed = 0
        for envelope in self.journal.pending():
            session, created = self.registry.upsert(envelope)
            if created:
                self.stats.sessions_registered += 1
            if session.project is None:
                await self._attach_project(session, envelope)
            event = envelope.event
            if event.name == "context.closed":
                session.end(str(event.attributes.get("reason") or "session_end"))
                await self._locked(session, self._capture(session, envelope))
                await self._locked(session, self._finalize(session, occurrence="end"))
            elif event.name == "context.compacting":
                await self._locked(session, self._capture(session, envelope))
            elif event.name in CAPTURED:
                await self._locked(session, self._capture(session, envelope))
            replayed += 1
        self.stats.replayed += replayed
        if replayed:
            logger.info(f"replayed {replayed} journaled session event(s)")
        self.registry.save()
        return replayed

    async def sweep(self, *, now: datetime | None = None) -> int:
        stale, pruned = self.registry.sweep(
            now=now, is_alive=self._is_alive, local_host=self.local_host,
        )
        for session in stale:
            self.stats.finalized_by_sweep += 1
            logger.info(f"[{session.key}] finalizing: {session.end_reason}")
            self._drop_recall(session.key)
            self._spawn(self._locked(session, self._finalize(session, occurrence="end")))
        for key in pruned:
            self.journal.clear(key)
            self._locks.pop(key, None)
            self._drop_recall(key)
            self._recall_seq.pop(key, None)
            self._recall_failure_announced.discard(key)
        if stale or pruned:
            self.registry.save()
        return len(stale)

    async def _sweep_loop(self) -> None:
        interval = self.config.housekeeping.sweep_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep()
            except Exception:  # the sweeper must outlive any one failure
                logger.exception("session sweep failed")

    async def probe_loimi(self) -> bool:
        """Reachability, not authorization: a 404 for a made-up context id
        proves the service answered; only unreachability is a failure."""
        spec = MemorySpec(
            endpoint_env=self.config.endpoint_env, credential_env=self.config.credential_env,
            scopes=MemoryScopesSpec(read=[], propose=["probe"], default_write="probe"),
        )
        client = self._client_factory(spec) if self._client_factory else MemoryClient(spec)
        try:
            await asyncio.wait_for(client.get_context("miragend-probe"), timeout=5.0)
        except MemoryAPIError:
            self.stats.note_loimi(True)
            return True
        except (MemoryUnavailable, asyncio.TimeoutError) as exc:
            self.stats.note_loimi(False, f"probe: {exc}")
            return False
        self.stats.note_loimi(True)
        return True

    # ── observability ─────────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        active = self.registry.list(active_only=True)
        by_harness: dict[str, int] = {}
        for session in active:
            by_harness[session.harness] = by_harness.get(session.harness, 0) + 1
        return {
            "principal": self.config.principal,
            "principal_source": self.principal_source,
            "host": self.local_host,
            "active_sessions": len(active),
            "active_by_harness": by_harness,
            "known_sessions": len(self.registry.list()),
            "loimi": {
                "endpoint_configured": bool(self.environ.get(self.config.endpoint_env)),
                "credential_configured": bool(self.environ.get(self.config.credential_env)),
                "operator_configured": bool(self.environ.get(self.config.operator_token_env)),
                "shared_scopes": dict(self.shared_scopes_ready),
                "last_ok_at": self.stats.last_loimi_ok_at,
                "last_error": self.stats.last_loimi_error,
                "last_error_at": self.stats.last_loimi_error_at,
            },
            "store": {
                "enabled": self.store is not None,
                "configured": self.store_active(),
                "agent_id": self.config.store.agent_id if self.store else None,
                "default_namespace": self.config.store.namespace if self.store else None,
                "last_ok_at": self.stats.last_store_ok_at,
                "last_error": self.stats.last_store_error,
                "last_error_at": self.stats.last_store_error_at,
            },
            "mcp": {"enabled": self.config.mcp.enabled,
                    "default_project": self.config.mcp.default_project},
            "recall": {
                "enabled": self.config.recall.enabled,
                "selector_configured": self.selector is not None,
                # Backend label + model string only: base_url (a hostname)
                # and credentials never reach this unguarded endpoint.
                "selector_backend": getattr(self.selector, "backend", "custom")
                                    if self.selector else None,
                "selector_model": getattr(self.selector, "model", None),
                "selector_base_url": bool(getattr(self.selector, "base_url_configured", False)),
                "selector_timeout_s": self.config.recall.timeout_s,
                "on_prompt": self.config.recall.on_prompt,
                "delivery": self.config.recall.delivery,
                "pending": sum(1 for job in self._recalls.values() if job.delivered is None),
            },
            "judgments": self.judgments.describe(),
            "scopes": {
                "policy": self.config.scopes.provision,
                "provisioned": sorted(self._provisioned),
                "unprovisionable": dict(self._unprovisionable),
            },
            "pending_tasks": len(self._tasks),
            "stats": self.stats.snapshot(),
        }
