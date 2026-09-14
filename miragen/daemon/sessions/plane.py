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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from miragen.daemon.sessions.config import SessionsConfig
from miragen.daemon.sessions.models import (
    ChildAgent,
    EventEnvelope,
    ExternalSession,
    ProjectIdentity,
    now_iso,
)
from miragen.daemon.sessions.projects import ScopeAssignment, assign_scopes, resolve_project
from miragen.daemon.sessions.registry import EventJournal, SessionRegistry, pid_alive
from miragen.memory.client import MemoryAPIError, MemoryClient, MemoryUnavailable
from miragen.memory.lifecycle import MemoryLifecycle
from miragen.models import MemoryRecallSpec, MemoryScopesSpec, MemorySpec
from miragen_hook.normalize import CAPTURED, CONTEXT_OPENING, NormalizedEvent

logger = logging.getLogger("miragend.sessions")

# Daemon-side bounds UNDER the adapter's (client.py: 10 s for context).
RETRIEVAL_TIMEOUT_S = 8.0
WRITE_TIMEOUT_S = 15.0
_PROVISION_VERBS = ["read", "propose", "resolve", "retract"]


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

    def note_loimi(self, ok: bool, error: str | None = None) -> None:
        if ok:
            self.last_loimi_ok_at = now_iso()
        else:
            self.last_loimi_error = (error or "unknown")[:300]
            self.last_loimi_error_at = now_iso()

    def snapshot(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if not k.endswith("_total")
                and k not in ("retrieval_count", "write_count")}
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
    ) -> None:
        import os

        self.config = config
        self.environ = os.environ if environ is None else environ
        self.state_dir = state_dir or config.resolved_state_dir(self.environ)
        self.registry = SessionRegistry(
            self.state_dir,
            retention_hours=config.housekeeping.retention_hours,
            stale_after_minutes=config.housekeeping.stale_after_minutes,
        )
        self.journal = EventJournal(self.state_dir)
        self.stats = PlaneStats()
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
        self._tasks: set[asyncio.Task] = set()
        self._sweeper: asyncio.Task | None = None

    # ── lifecycle of the plane itself ──────────────────────────────────────

    async def start(self) -> None:
        await self.replay_journal()
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="miragend-sessions-sweeper")

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
        if session.project is None:
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
        async with self._lock(session.key):
            if event.name in CONTEXT_OPENING:
                context, detail = await self._open_context(session, envelope)
            elif event.name == "input.received":
                session.note_prompt(event.content)
                self._journal_and_capture(session, envelope)
                context, detail = await self._prompt_recall(session, envelope)
            elif event.name == "turn.finished":
                session.note_turn(event.content)
                self._journal_and_capture(session, envelope)
            elif event.name == "tool.finished":
                session.counters.tool_failures += 1
                self._journal_and_capture(session, envelope)
            elif event.name == "context.compacting":
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
                session.end(str(event.attributes.get("reason") or "session_end"))
                self.journal.append(envelope)
                self._spawn(self._capture_then_finalize(session, envelope, occurrence="end"))
        return HandleResult(key=session.key, state=session.state, context=context, detail=detail)

    # ── project + scopes ─────────────────────────────────────────────────────

    async def _attach_project(self, session: ExternalSession, envelope: EventEnvelope) -> None:
        cwd = envelope.client.cwd or envelope.client.project_dir
        if not cwd:
            return
        project = self._projects.get(cwd)
        if project is None:
            try:
                project = await asyncio.to_thread(self._resolve, cwd)
            except Exception as exc:  # resolution must never fail an event
                logger.warning(f"project resolution failed for {cwd}: {exc}")
                return
            self._projects[cwd] = project
        session.project = project
        assignment = self._assignment_for(project)
        session.scope = assignment.write

    def _assignment_for(self, project: ProjectIdentity) -> ScopeAssignment:
        assignment = self._assignments.get(project.id)
        if assignment is None:
            assignment = assign_scopes(self.config.scopes, self.config.projects, project)
            self._assignments[project.id] = assignment
        return assignment

    async def _lifecycle_for(self, session: ExternalSession) -> tuple[MemoryLifecycle | None, str | None]:
        """The lifecycle bound to this session's project scopes, provisioning
        the project scope first when policy says so. (None, reason) only when
        the session has no project at all; a scope that could not be
        provisioned still gets a lifecycle (fallback scope if configured,
        else the project scope itself) so that Loimi's refusal degrades the
        packet EXPLICITLY instead of the session silently getting nothing."""
        if session.project is None:
            return None, "no working directory reported; memory not scoped"
        assignment = self._assignment_for(session.project)
        write = assignment.write
        read = assignment.read
        policy = self.config.scopes
        detail: str | None = None
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
                tools_available=False, selector=self.selector,
            )
            self._lifecycles[write] = lifecycle
        session.scope = write
        return lifecycle, detail

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
            f"project={project.id if project else 'unknown'}",
            f"cwd={session.cwd or '?'}",
            f"scope={session.scope or 'none'}",
        ]
        if session.parent_session:
            parts.append(f"parent={session.parent_session}")
        if session.agent:
            parts.append(f"agent={session.agent}")
        return "[session context — attributed reference data] " + " ".join(parts)

    async def _open_context(
        self, session: ExternalSession, envelope: EventEnvelope,
    ) -> tuple[str | None, str | None]:
        lifecycle, scope_detail = await self._lifecycle_for(session)
        self.stats.retrievals += 1
        if lifecycle is None:
            self.stats.retrieval_failures += 1
            return None, scope_detail
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
            return None, "retrieval timed out"
        finally:
            elapsed = (time.monotonic() - started) * 1000
            self.stats.retrieval_ms_total += elapsed
            self.stats.retrieval_count += 1
        if packet.degraded:
            self.stats.retrieval_failures += 1
            self.stats.note_loimi(False, packet.degraded)
        else:
            self.stats.note_loimi(True)
        self.stats.injections += 1
        session.counters.injections += 1
        detail = "; ".join(part for part in (scope_detail, packet.degraded) if part) or None
        return f"{self._session_header(session)}\n{packet.text}", detail

    async def _prompt_recall(
        self, session: ExternalSession, envelope: EventEnvelope,
    ) -> tuple[str | None, str | None]:
        recall = self.config.recall
        prompt = envelope.event.content or ""
        if not recall.on_prompt or self.selector is None or len(prompt) < recall.min_prompt_chars:
            return None, None
        lifecycle, reason = await self._lifecycle_for(session)
        if lifecycle is None:
            return None, reason
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
            return None, "prompt recall timed out"
        finally:
            self.stats.retrieval_ms_total += (time.monotonic() - started) * 1000
            self.stats.retrieval_count += 1
        self.stats.prompt_recalls += 1
        if status.startswith("degraded"):
            self.stats.retrieval_failures += 1
            self.stats.note_loimi(False, status)
        if section:
            self.stats.injections += 1
            session.counters.injections += 1
        return section, status

    # ── capture (background, ordered per session) ─────────────────────────────

    def _journal_and_capture(self, session: ExternalSession, envelope: EventEnvelope) -> None:
        self.journal.append(envelope)
        self._spawn(self._locked(session, self._capture(session, envelope)))

    async def _locked(self, session: ExternalSession, coro: Awaitable[Any]) -> Any:
        async with self._lock(session.key):
            return await coro

    async def _capture(self, session: ExternalSession, envelope: EventEnvelope) -> None:
        event = envelope.event
        if event.name not in CAPTURED:
            return
        lifecycle, reason = await self._lifecycle_for(session)
        if lifecycle is None:
            self.stats.capture_failures += 1
            session.counters.capture_failures += 1
            logger.info(f"[{session.key}] capture skipped: {reason}")
            return
        normalized = NormalizedEvent(
            name=event.name, harness=envelope.harness, original_event=event.original_event,
            session_id=envelope.session_id, ids=dict(event.ids), content=event.content,
            attributes=dict(event.attributes),
        )
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                lifecycle.capture_harness_event(
                    instance=session.project.slug if session.project else None,
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
            self.stats.captures += 1
            session.counters.captures += 1
            self.stats.note_loimi(True)
        else:
            self.stats.capture_failures += 1
            session.counters.capture_failures += 1
            self.stats.note_loimi(False, result.get("detail"))

    async def _capture_then_finalize(
        self, session: ExternalSession, envelope: EventEnvelope, *, occurrence: str,
    ) -> None:
        async with self._lock(session.key):
            await self._capture(session, envelope)
            await self._finalize(session, occurrence=occurrence)
        self.registry.save()

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
        lifecycle, reason = await self._lifecycle_for(session)
        if lifecycle is None:
            logger.info(f"[{session.key}] finalize skipped: {reason}")
            return
        instance = session.project.slug if session.project else None
        digest = self.render_episode(session, occurrence=occurrence)
        started = time.monotonic()
        try:
            episode = await asyncio.wait_for(
                lifecycle.capture_episode(
                    instance=instance,
                    idempotency_key=f"episode:{session.key}:{occurrence}",
                    content=digest,
                    source_ref=f"session:{session.key}",
                    attributes={
                        "harness": session.harness,
                        "session_id": session.session_id,
                        "occurrence": occurrence,
                        "project": session.project.id if session.project else None,
                        "cwd": session.cwd,
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
        if episode.get("status") == "captured":
            self.stats.episodes += 1
            session.episodes_written.append(occurrence)
            self.stats.note_loimi(True)
        else:
            self.stats.capture_failures += 1
            self.stats.note_loimi(False, episode.get("detail"))

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
        else:
            self.stats.capture_failures += 1
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
        stale, pruned = self.registry.sweep(now=now, is_alive=self._is_alive)
        for session in stale:
            self.stats.finalized_by_sweep += 1
            logger.info(f"[{session.key}] finalizing: {session.end_reason}")
            self._spawn(self._locked(session, self._finalize(session, occurrence="end")))
        for key in pruned:
            self.journal.clear(key)
            self._locks.pop(key, None)
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
            "active_sessions": len(active),
            "active_by_harness": by_harness,
            "known_sessions": len(self.registry.list()),
            "loimi": {
                "endpoint_configured": bool(self.environ.get(self.config.endpoint_env)),
                "credential_configured": bool(self.environ.get(self.config.credential_env)),
                "operator_configured": bool(self.environ.get(self.config.operator_token_env)),
                "last_ok_at": self.stats.last_loimi_ok_at,
                "last_error": self.stats.last_loimi_error,
                "last_error_at": self.stats.last_loimi_error_at,
            },
            "recall": {
                "enabled": self.config.recall.enabled,
                "selector_configured": self.selector is not None,
                "on_prompt": self.config.recall.on_prompt,
            },
            "scopes": {
                "policy": self.config.scopes.provision,
                "provisioned": sorted(self._provisioned),
                "unprovisionable": dict(self._unprovisionable),
            },
            "pending_tasks": len(self._tasks),
            "stats": self.stats.snapshot(),
        }
