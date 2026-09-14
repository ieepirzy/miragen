"""Agent-facing scheduling — the runtime tool library's first member.

`schedule_wakeup` gives an agent the CronCreate shape: "wake me at T /
in N minutes / every N minutes / on this cron" — implemented as managed
schedule bindings, so scheduled fires are ordinary managed runs with full
provenance, budget checks, admission control and (one-shot `at`) self-
deletion after firing.

Boundaries, deliberately:
- Interactive-mode agents never get these tools: a self-scheduled fire is
  self-activation, the exact thing interactive mode promises not to do
  (the same rule PUT /schedules already enforces).
- An agent may CANCEL only schedules it created itself (provenance
  `scheduled_by: agent`). Operator bindings are listed for transparency
  but are not the agent's to remove.
- Creation floors: one-shots at least ~1 minute out unless explicitly
  immediate; recurring intervals inherit the managed-schedule 10s floor.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from miragen.models import RunProvenance
from miragen.schedules import BindingConflictError, ScheduleSpec, ScheduleStore

logger = logging.getLogger("miragen.runtime_tools.scheduling")

AGENT_SCHEDULED_MARKER = "agent"


class SchedulingBackend:
    """The seam the app wires: the schedule store plus the reconcile/drop
    callables that keep APScheduler in step (the same pair PUT /schedules
    uses, so tool-created bindings behave identically to API-created)."""

    def __init__(
        self,
        store: ScheduleStore,
        reconcile: Callable[..., None],
        drop_job: Callable[[str], None],
        next_fire_at: Callable[[str], str | None],
    ) -> None:
        self.store = store
        self.reconcile = reconcile
        self.drop_job = drop_job
        self.next_fire_at = next_fire_at

    # -- operations --------------------------------------------------------

    def create(
        self,
        *,
        prompt: str,
        run_id: str | None,
        name: str | None = None,
        at: datetime | None = None,
        in_minutes: float | None = None,
        every_minutes: float | None = None,
        cron: str | None = None,
        instance: str | None = None,
    ) -> dict:
        chosen = [v for v in (at, in_minutes, every_minutes, cron) if v is not None]
        if len(chosen) != 1:
            return {"status": "rejected",
                    "detail": "choose exactly one of at / in_minutes / every_minutes / cron"}
        if in_minutes is not None:
            at = datetime.now(timezone.utc) + timedelta(minutes=in_minutes)
        if at is not None:
            spec = ScheduleSpec(at=at)
        elif every_minutes is not None:
            spec = ScheduleSpec(every_s=max(int(every_minutes * 60), 10))
        else:
            spec = ScheduleSpec(cron=cron)

        name = name or f"wakeup-{uuid.uuid4().hex[:8]}"
        provenance = RunProvenance.model_validate({
            "scheduled_by": AGENT_SCHEDULED_MARKER,
            "requested_by": f"run:{run_id}" if run_id else None,
        })
        try:
            binding = self.store.upsert(
                name, schedule=spec, prompt=prompt, enabled=True,
                instance=instance, provenance=provenance, metadata={},
            )
        except BindingConflictError:
            return {"status": "conflict",
                    "detail": f"a schedule named '{name}' already exists"}
        except ValueError as exc:
            return {"status": "rejected", "detail": str(exc)}
        try:
            self.reconcile(binding)
        except Exception as exc:  # roll the store back, same as the API path
            try:
                self.store.delete(name)
            except KeyError:
                pass
            logger.warning(f"scheduler rejected tool-created binding '{name}': {exc}")
            return {"status": "rejected", "detail": f"scheduler rejected the binding: {exc}"}
        return {
            "status": "accepted",
            "name": name,
            "one_shot": spec.one_shot,
            "next_fire_at": self.next_fire_at(name),
        }

    def list(self) -> list[dict]:
        entries = []
        for binding in self.store.list():
            provenance = binding.provenance.model_dump() if binding.provenance else {}
            entries.append({
                "name": binding.name,
                "prompt_preview": binding.prompt[:120],
                "enabled": binding.enabled,
                "one_shot": binding.schedule.one_shot,
                "next_fire_at": self.next_fire_at(binding.name),
                "cancellable": provenance.get("scheduled_by") == AGENT_SCHEDULED_MARKER,
            })
        return entries

    def cancel(self, name: str) -> dict:
        binding = self.store.get(name)
        if binding is None:
            return {"status": "not_found", "detail": f"no schedule named '{name}'"}
        provenance = binding.provenance.model_dump() if binding.provenance else {}
        if provenance.get("scheduled_by") != AGENT_SCHEDULED_MARKER:
            return {
                "status": "rejected",
                "detail": f"'{name}' was not agent-scheduled — operator bindings "
                "are not yours to cancel; ask the operator",
            }
        try:
            self.store.delete(name)
        except KeyError:
            return {"status": "not_found", "detail": f"no schedule named '{name}'"}
        self.drop_job(name)
        return {"status": "cancelled", "name": name}


def build_scheduling_tools(
    backend: SchedulingBackend,
    current_run_id: Callable[[], str | None],
    current_instance: Callable[[], str | None],
) -> list[Callable]:
    """Model-tier closures, mirroring the memory/voice tool pattern."""

    async def schedule_wakeup(
        prompt: str,
        in_minutes: float | None = None,
        at: str | None = None,
        cron: str | None = None,
        every_minutes: float | None = None,
        name: str | None = None,
    ) -> str:
        """Schedule a future run of yourself with `prompt` — exactly one of
        the timing arguments. One-shots (`at`/`in_minutes`) fire once and
        delete themselves; `cron` (5-field, UTC) and `every_minutes`
        recur until cancelled. The fire arrives as a normal scheduled run
        of this agent, on this instance.

        Args:
            prompt: The complete prompt the future run receives.
            in_minutes: Fire once, this many minutes from now.
            at: Fire once at this ISO-8601 UTC time.
            cron: Recur on this 5-field cron expression (UTC).
            every_minutes: Recur every N minutes (min 1/6).
            name: Optional stable name (needed to cancel a recurring one).
        """
        parsed_at = datetime.fromisoformat(at) if at else None
        result = backend.create(
            prompt=prompt, run_id=current_run_id(), name=name,
            at=parsed_at, in_minutes=in_minutes,
            every_minutes=every_minutes, cron=cron,
            instance=current_instance(),
        )
        return json.dumps(result)

    async def list_schedules() -> str:
        """List this agent's managed schedules (yours are cancellable;
        operator-created ones are shown for transparency only)."""
        return json.dumps({"schedules": backend.list()})

    async def cancel_schedule(name: str) -> str:
        """Cancel a schedule YOU created (by its name from schedule_wakeup
        or list_schedules). Operator bindings cannot be cancelled here.

        Args:
            name: The schedule's name.
        """
        return json.dumps(backend.cancel(name))

    return [schedule_wakeup, list_schedules, cancel_schedule]


def build_scheduling_mcp(get_state) -> Any:
    """The executor-tier surface: /mcp/schedule, ask-human/voice/memory
    pattern. `get_state` -> (SchedulingBackend | None, run_store | None)."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "miragen-schedule",
        instructions=(
            "miragen's scheduling tools: schedule_wakeup books a future run "
            "of this agent (one-shot or recurring); list_schedules and "
            "cancel_schedule manage them. You can only cancel schedules "
            "you created."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def _backend_and_run(run_id: str | None):
        backend, run_store = get_state()
        if backend is None:
            raise RuntimeError(
                "scheduling tools are not enabled for this agent "
                "(interactive mode, or runtime_tools.schedule: false)"
            )
        record = None
        if run_store is not None:
            running = run_store.list(limit=100, status="running")
            if run_id is not None:
                record = run_store.get(run_id)
            elif len(running) == 1:
                record = run_store.get(running[0].run_id)
        return backend, record

    @mcp.tool()
    async def schedule_wakeup(
        prompt: str,
        in_minutes: float | None = None,
        at: str | None = None,
        cron: str | None = None,
        every_minutes: float | None = None,
        name: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Schedule a future run of this agent with `prompt` — exactly one
        timing argument. One-shots fire once then delete themselves.

        Args:
            prompt: The complete prompt the future run receives.
            in_minutes: Fire once, this many minutes from now.
            at: Fire once at this ISO-8601 UTC time.
            cron: Recur on this 5-field cron expression (UTC).
            every_minutes: Recur every N minutes.
            name: Optional stable name (needed to cancel recurring ones).
            run_id: Only needed when several runs are active.
        """
        backend, record = _backend_and_run(run_id)
        result = backend.create(
            prompt=prompt,
            run_id=record.run_id if record else None,
            name=name,
            at=datetime.fromisoformat(at) if at else None,
            in_minutes=in_minutes, every_minutes=every_minutes, cron=cron,
            instance=record.instance if record else None,
        )
        return json.dumps(result)

    @mcp.tool()
    async def list_schedules() -> str:
        """List managed schedules (agent-created ones are cancellable)."""
        backend, _ = _backend_and_run(None)
        return json.dumps({"schedules": backend.list()})

    @mcp.tool()
    async def cancel_schedule(name: str) -> str:
        """Cancel a schedule this agent created.

        Args:
            name: The schedule's name.
        """
        backend, _ = _backend_and_run(None)
        return json.dumps(backend.cancel(name))

    return mcp
