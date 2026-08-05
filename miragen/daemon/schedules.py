"""
One-shot prompt retriggers, moved here from miragen-mcp so the daemon owns
the only persistent state in the management plane (retriggers.sqlite lives
in the swarm workspace, which is the daemon's volume).

The scheduler fires POST /run at the target agent over miragen-net. Jobs
persist across daemon restarts in a SQLAlchemy job store; a fire time missed
while the daemon was down still fires if it comes back within the grace
period.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from miragen.daemon.core import DaemonError, InvalidInput

logger = logging.getLogger(__name__)

# Grace period for retriggers whose fire time passed while the daemon was
# down: on restart within this many seconds of the missed fire time the job
# still runs; older misses are dropped (APScheduler default behaviour).
RETRIGGER_MISFIRE_GRACE = 3600

# Scheduled retrigger job ids are "retrigger-<agent>-<unix_ts>". Agent names
# may contain hyphens, so parse the agent as everything between the fixed
# prefix and the trailing "-<digits>" timestamp.
_RETRIGGER_ID_RE = re.compile(r"^retrigger-(?P<agent>.+)-\d+$")

# Test seam: when set (httpx.MockTransport in tests), the fire request routes
# through it instead of the docker network.
_agent_transport = None


class JobNotFound(DaemonError):
    status = 404
    code = "job_not_found"


async def _fire_trigger(agent: str, prompt: str) -> None:
    # Module-level by contract: the persistent SQLAlchemy job store pickles
    # this callable by reference (module path + qualname). Keep it top-level
    # so jobs scheduled before a restart can be unpickled and fired
    # afterwards. Token read at fire time, not schedule time, so a rotated
    # MIRAGEN_INTERNAL_TOKEN applies to already-scheduled jobs.
    token = os.getenv("MIRAGEN_INTERNAL_TOKEN", "")
    headers = {"X-Miragen-Token": token} if token else {}
    try:
        async with httpx.AsyncClient(transport=_agent_transport) as client:
            resp = await client.post(
                f"http://{agent}:8000/run",
                json={"prompt": prompt},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.error("retrigger POST to %s failed: %s", agent, exc)


def retrigger_agent(job_id: str) -> str | None:
    """Extract the agent name from a 'retrigger-<agent>-<ts>' job id, or None."""
    m = _RETRIGGER_ID_RE.fullmatch(job_id)
    return m.group("agent") if m else None


def build_scheduler(db_path: Path):
    """The production AsyncIOScheduler with a persistent job store.

    Imported lazily so the daemon extra's sqlalchemy dependency is only
    needed when actually running the daemon, not to import this module.
    """
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")}
    )


class ScheduleStore:
    """Thin wrapper over an APScheduler instance holding retrigger jobs.

    The scheduler is injected so tests can pass a stub; production uses
    build_scheduler(workspace / 'retriggers.sqlite').
    """

    def __init__(self, scheduler) -> None:
        self._scheduler = scheduler

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    def set(
        self,
        agent: str,
        prompt: str,
        *,
        delay_seconds: int | None = None,
        at: str | None = None,
    ) -> dict:
        if (delay_seconds is None) == (at is None):
            raise InvalidInput("provide exactly one of delay_seconds or at")
        if delay_seconds is not None:
            if delay_seconds < 1:
                raise InvalidInput("delay_seconds must be >= 1")
            fire_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        else:
            try:
                fire_at = datetime.fromisoformat(at)  # type: ignore[arg-type]
            except ValueError:
                raise InvalidInput(
                    f"'{at}' is not a valid ISO 8601 datetime"
                ) from None
            if fire_at.tzinfo is None:
                fire_at = fire_at.replace(tzinfo=timezone.utc)
            if fire_at <= datetime.now(timezone.utc):
                raise InvalidInput(f"{fire_at.isoformat()} is in the past")

        from apscheduler.triggers.date import DateTrigger

        job_id = f"retrigger-{agent}-{fire_at.timestamp():.0f}"
        self._scheduler.add_job(
            _fire_trigger,
            trigger=DateTrigger(run_date=fire_at),
            args=[agent, prompt],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=RETRIGGER_MISFIRE_GRACE,
        )
        return {"job_id": job_id, "agent": agent, "fire_at": fire_at.isoformat()}

    def list(self, agent: str | None = None) -> list[dict]:
        prefix = f"retrigger-{agent}-" if agent is not None else "retrigger-"
        retriggers = []
        for job in self._scheduler.get_jobs():
            if not job.id.startswith(prefix):
                continue
            prompt_preview = ""
            if job.args and len(job.args) > 1:
                prompt_preview = str(job.args[1])[:200]
            next_run = getattr(job, "next_run_time", None)
            retriggers.append(
                {
                    "job_id": job.id,
                    "agent": retrigger_agent(job.id),
                    "fire_at": next_run.isoformat() if next_run else None,
                    "prompt_preview": prompt_preview,
                }
            )
        return retriggers

    def cancel(self, job_id: str) -> None:
        from apscheduler.jobstores.base import JobLookupError

        try:
            self._scheduler.remove_job(job_id)
        except JobLookupError:
            raise JobNotFound(f"no retrigger '{job_id}'") from None
