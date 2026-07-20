from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger as APIntervalTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.usage import UsageLimits

from miragen.broker import PendingApproval, get_broker
from miragen.edf import (
    EDFValidationError,
    ResolutionContext,
    ResolvedEDF,
    build_run_snapshot,
    resolve_edf,
)
from miragen.executor import ExecutorBackend, ExecutorResult, RepositoryCheckout, build_executor
from miragen.executor.sink import build_sink
from miragen.factory import build_agent, registered_handlers
from miragen.load import load_profile
from miragen.models import (
    AgentProfile,
    ApprovalResponse,
    CronTrigger as ProfileCronTrigger,
    InterventionAnswer,
    InterventionRequest,
    IntervalTrigger as ProfileIntervalTrigger,
    RepositoryRevision,
    RunProvenance,
    RunRecord,
    RunSummary,
    StartupTrigger as ProfileStartupTrigger,
    sum_usage,
)
from miragen.runs import (
    AmbiguousRunIdError,
    RunStore,
    extract_run_details,
    run_retention_from_env,
    simplify_history_messages,
    tokens_used_since,
)
from miragen.schedules import (
    BindingConflictError,
    ScheduleBinding,
    ScheduleSpec,
    ScheduleStore,
)

logger = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────────────────────────────────

_profile: AgentProfile | None = None
_agent: Agent | None = None
_limits: UsageLimits | None = None
_scheduler: AsyncIOScheduler = AsyncIOScheduler()
_run_store: RunStore | None = None
_executor: "ExecutorBackend | None" = None
_schedule_store: ScheduleStore | None = None

HISTORY_FILE = Path("/agent/history.json")
HISTORY_SIDECAR = Path("/agent/history.runs.jsonl")

# Keeps references to fire-and-forget /run/async tasks so they aren't
# garbage-collected mid-run (a well-known asyncio.create_task gotcha).
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ── Helpers ─────────────────────────────────────────────────────────────────────────────

def _stamp_prompt(prompt: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[{ts}]\n{prompt}"


def _cap_history(messages: list) -> list:
    """Trim to the newest `history_max_messages` if the profile sets a cap (oldest dropped)."""
    cap = _profile.history_max_messages if _profile else None
    if cap is not None and len(messages) > cap:
        return messages[-cap:]
    return messages


def _load_history_messages() -> list:
    """Read + validate HISTORY_FILE; a missing or unparsable file behaves as empty history."""
    if not HISTORY_FILE.exists():
        return []
    try:
        return ModelMessagesTypeAdapter.validate_json(HISTORY_FILE.read_bytes())
    except Exception:
        logger.warning("Failed to parse history.json")
        return []


def _sidecar_message_count(run_id: str) -> int | None:
    """
    message_count recorded in HISTORY_SIDECAR the last time `run_id` saved history,
    or None if that run never appears there. History only ever grows by appending,
    so that count is also the prefix length of the current history at save time.
    """
    if not HISTORY_SIDECAR.exists():
        return None
    count = None
    for line in HISTORY_SIDECAR.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("run_id") == run_id:
            count = entry.get("message_count")
    return count


# ── Agent runner ──────────────────────────────────────────────────────────────────

def _append_history_sidecar(run_id: str | None, message_count: int) -> None:
    """Correlate a history.json save with the run that produced it (best effort —
    a failed sidecar write logs a warning, never fails the run)."""
    try:
        line = json.dumps({
            "run_id": run_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "message_count": message_count,
        })
        HISTORY_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_SIDECAR, "a") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("Failed to append history sidecar entry")


async def run_agent(
    prompt: str,
    use_history: bool = False,
    record: RunRecord | None = None,
    repositories: list[RepositoryCheckout] | None = None,
) -> str:
    """
    Core agent execution. Called by cron/interval/startup and HTTP triggers.

    If `record` is given (from _run_store.start()), the run's outcome — success
    with usage/tool-calls, or failure with the error — is written back to it via
    _run_store.finish() before returning (or before the exception propagates).

    Executor-tier profiles dispatch to the executor backend instead of the
    pydantic-ai agent; `use_history` does not apply there (the executor thread
    is the history). `repositories` (executor tier only) is the launch-time
    checkout plan carrying ephemeral bindings.
    """
    if _executor is not None:
        return await _run_executor_turn(prompt, record, repositories=repositories)

    assert _agent is not None, "Agent not initialized"

    history = None
    if use_history:
        try:
            if HISTORY_FILE.exists():
                history = _cap_history(ModelMessagesTypeAdapter.validate_json(HISTORY_FILE.read_bytes()))
        except Exception:
            logger.warning("Failed to load history, starting fresh")

    try:
        result = await _agent.run(prompt, usage_limits=_limits, message_history=history)
    except Exception as e:
        if record is not None and _run_store is not None:
            _run_store.finish(record, status="failed", error=str(e))
        raise

    if use_history:
        try:
            messages = result.all_messages()
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_bytes(ModelMessagesTypeAdapter.dump_json(messages))
            _append_history_sidecar(record.run_id if record else None, len(messages))
        except Exception:
            logger.warning("Failed to save history")

    output = str(result.output)

    if record is not None and _run_store is not None:
        usage, tool_calls = extract_run_details(result)
        _run_store.finish(record, status="succeeded", output=output, usage=usage, tool_calls=tool_calls)

    return output


async def _run_executor_turn(
    prompt: str,
    record: RunRecord | None,
    *,
    resume: bool = False,
    repositories: list[RepositoryCheckout] | None = None,
) -> str:
    """One executor turn bound to an agent-run. Terminal-state bookkeeping:
    succeeded harvests the diff; suspended (budget) and failed (crash) keep
    thread + workspace as resume state; failed raises so callers see the
    same error contract as model-tier runs."""
    assert _executor is not None
    run_id = record.run_id if record is not None else uuid.uuid4().hex

    if repositories is None and record is not None and record.repositories:
        # Resume (or a retried turn) of a multi-repo run: bindings are
        # ephemeral and gone, but the workspace is already prepared —
        # binding-less checkouts let preparation re-read recorded state and
        # keep the multi-repo harvest semantics.
        repositories = [
            RepositoryCheckout(
                name=r.name,
                ref=r.ref,
                mount_path=r.mount_path or r.name,
                writable=r.writable,
            )
            for r in record.repositories
        ]

    turn = _executor.run_job(
        prompt,
        run_id,
        thread_id=record.thread_id if record is not None else None,
        workspace=record.workspace if record is not None else None,
        first_turn=not resume,
        prior_usage=record.usage if record is not None else None,
        repositories=repositories,
    )
    timeout_s = _executor.spec.turn_timeout_s
    try:
        result = await (asyncio.wait_for(turn, timeout=timeout_s) if timeout_s else turn)
    except asyncio.TimeoutError:
        # Wall-clock cap, enforced here so it holds regardless of the
        # backend's cooperation. Suspended, not terminal: workspace and (if
        # the stream got far enough to record one) thread survive as resume
        # state — a wedged executor is exactly the case where resume-or-
        # abandon is the right human decision.
        result = ExecutorResult(
            status="suspended",
            exit_reason="timeout",
            thread_id=_executor.latest_thread_id(run_id)
            or (record.thread_id if record is not None else None),
            error=f"turn exceeded turn_timeout_s={timeout_s}s and was cancelled",
        )

    if record is not None and _run_store is not None:
        usage = sum_usage(record.usage, result.usage)
        prepared_revisions = (
            [RepositoryRevision(**entry) for entry in result.repositories]
            if result.repositories
            else None
        )

        def _accumulate(prior, this_turn):
            # None + None stays None (metric not reportable); any reported
            # value starts/extends the accumulated total.
            if this_turn is None:
                return prior
            return (prior or 0) + this_turn

        record = _run_store.finish(
            record,
            status=result.status,
            output=result.output,
            error=result.error,
            usage=usage,
            thread_id=result.thread_id,
            workspace=str(Path(_executor.spec.workspace_root) / run_id)
            if record.workspace is None else record.workspace,
            exit_reason=result.exit_reason,
            diff_path=result.diff_path,
            repositories=prepared_revisions,
            setup_s=_accumulate(record.setup_s, result.setup_s),
            tool_call_count=_accumulate(record.tool_call_count, result.tool_call_count),
            tool_call_failures=_accumulate(record.tool_call_failures, result.tool_call_failures),
            pending_intervention=InterventionRequest.model_validate(result.intervention)
            if result.intervention is not None else None,
        )
        if prepared_revisions:
            _record_snapshot_commits(record.run_id, prepared_revisions)

    if result.status == "succeeded" and _executor.spec.artifact_sink is not None:
        await _store_artifact(result, run_id, record)

    if result.status == "failed":
        raise RuntimeError(result.error or "executor turn failed")
    if result.status == "suspended":
        return (
            f"[suspended: {result.exit_reason}] turn completed but the run is suspended; "
            f"resume via POST /runs/{run_id}/resume"
        )
    return result.output or ""


def _record_snapshot_commits(run_id: str, revisions: list[RepositoryRevision]) -> None:
    """Fill the concrete commit SHAs into the run snapshot's repository plan
    once workspace preparation has resolved the refs — this is the 'concrete
    revisions used by one run' half of the snapshot contract. Best effort:
    the run record already carries the same revisions authoritatively."""
    if _run_store is None:
        return
    snapshot = _run_store.read_snapshot(run_id)
    if snapshot is None:
        return
    commits = {r.name: r.commit for r in revisions}
    changed = False
    for entry in snapshot.get("repository_plan", []):
        commit = commits.get(entry.get("name"))
        if commit and entry.get("commit") != commit:
            entry["commit"] = commit
            changed = True
    if changed:
        _run_store.write_snapshot(run_id, snapshot)


async def _store_artifact(result: "ExecutorResult", run_id: str, record: RunRecord | None) -> None:
    """Push the harvested diff to the configured artifact sink. Advisory by
    construction: failure logs and marks artifact_stored=False on the record,
    but never raises into the run path — the diff on disk is the source of
    truth either way."""
    assert _executor is not None and _executor.spec.artifact_sink is not None
    sink_spec = _executor.spec.artifact_sink
    stored = False
    try:
        diff = Path(result.diff_path).read_text() if result.diff_path else ""
        token = os.environ.get(sink_spec.bearer_token_env) if sink_spec.bearer_token_env else None
        sink = build_sink(sink_spec, bearer_token=token)
        await sink.store(
            diff=diff,
            metadata={
                "run_id": run_id,
                "agent": _profile.name if _profile else None,
                "thread_id": result.thread_id,
            },
        )
        stored = True
        logger.info(f"[{_profile.name}] artifact sink stored diff for run {run_id}")
    except Exception as e:
        logger.error(f"[{_profile.name}] artifact sink failed for run {run_id}: {e}", exc_info=True)
    if record is not None and _run_store is not None:
        _run_store.annotate(record, artifact_stored=stored)


def _daily_budget_status() -> tuple[int, int] | None:
    """(used, limit) today (UTC) if profile.limits.tokens_per_day is configured, else None."""
    if _profile is None or _profile.limits is None or _profile.limits.tokens_per_day is None:
        return None
    if _run_store is None:
        return None
    midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    used = tokens_used_since(_run_store, midnight_utc)
    return used, _profile.limits.tokens_per_day


async def _notify_budget_exceeded(used: int, limit: int) -> None:
    """Dispatch through on_complete.notify only — this isn't a run output, just a heads-up."""
    if not _profile or not _profile.on_complete or not _profile.on_complete.notify:
        return
    handlers = registered_handlers()
    handler = handlers.get(_profile.on_complete.notify)
    if handler is not None:
        message = f"[{_profile.name}] scheduled run skipped: daily token budget exceeded ({used}/{limit})."
        await handler(_profile.name, message)


async def run_agent_scheduled(
    prompt: str,
    *,
    trigger: str = "cron",
    provenance: RunProvenance | None = None,
    stamp: bool = True,
) -> None:
    """Scheduler-triggered run (cron, interval, startup, or a managed
    binding). Handles on_complete side effects — for managed fires too, by
    decision: uniform with profile fires. The daily budget applies to every
    trigger source; `stamp=False` (managed fires) keeps completed prompts
    verbatim."""
    assert _profile is not None
    logger.info(f"[{_profile.name}] scheduled run triggered ({trigger})")

    budget = _daily_budget_status()
    if budget is not None and budget[0] >= budget[1]:
        used, limit = budget
        logger.warning(f"[{_profile.name}] daily token budget exceeded ({used}/{limit}) — skipping scheduled run")
        if _profile.limits.on_exceeded == "notify":
            try:
                await _notify_budget_exceeded(used, limit)
            except Exception as e:
                logger.error(f"[{_profile.name}] budget-exceeded notify failed: {e}", exc_info=True)
        return

    if stamp and _profile.inject_timestamp:
        prompt = _stamp_prompt(prompt)

    record = (
        _run_store.start(agent_name=_profile.name, trigger=trigger, prompt=prompt, provenance=provenance)
        if _run_store is not None
        else None
    )

    try:
        output = await run_agent(prompt, record=record)
        logger.info(f"[{_profile.name}] scheduled run complete")
    except Exception as e:
        logger.error(f"[{_profile.name}] scheduled run failed: {e}", exc_info=True)
        return

    if record is not None and _run_store is not None:
        # Executor-tier suspended runs (budget) return normally from
        # run_agent instead of raising, so a status check is the only way
        # to tell "truly done" from "one resumable turn completed" — an
        # unconditional on_complete would fire notifications/webhooks as if
        # a still-unfinished autonomous job had succeeded.
        final = _run_store.get(record.run_id)
        if final is not None and final.status != "succeeded":
            logger.info(
                f"[{_profile.name}] scheduled run ended in status '{final.status}', "
                "not 'succeeded' — skipping on_complete"
            )
            return

    try:
        await _handle_on_complete(output)
    except Exception as e:
        logger.error(f"[{_profile.name}] on_complete failed: {e}", exc_info=True)


# Alias — kept so existing imports of `run_agent_cron` (e.g. in tests) don't break.
run_agent_cron = run_agent_scheduled


# ── Managed schedules (issue #33 Phase F) ────────────────────────────────────
#
# API-owned schedule bindings, reconciled by a control plane. Distinct
# APScheduler job namespace (managed:<name>) so profile-trigger jobs can
# never collide. Design record: docs/design/managed-schedules.md.


def _managed_job_id(name: str) -> str:
    return f"managed:{name}"


def _apscheduler_trigger(spec: ScheduleSpec):
    if spec.cron is not None:
        # Managed cron is UTC by contract — timezone rendering is the
        # control plane's problem, not a per-binding knob.
        return CronTrigger.from_crontab(spec.cron, timezone=timezone.utc)
    return APIntervalTrigger(seconds=spec.every_s)


async def _run_managed_schedule(name: str) -> None:
    """One fire of a managed binding. Reads the binding fresh so a
    just-disabled or just-deleted binding never fires stale."""
    if _schedule_store is None:
        return
    binding = _schedule_store.get(name)
    if binding is None or not binding.enabled:
        return
    prov = binding.provenance.model_dump() if binding.provenance is not None else {}
    prov["schedule_name"] = name
    prov["fired_at"] = datetime.now(timezone.utc).isoformat()
    if binding.metadata:
        prov["schedule_metadata"] = dict(binding.metadata)
    await run_agent_scheduled(
        binding.prompt,
        trigger="managed",
        provenance=RunProvenance.model_validate(prov),
        stamp=False,  # completed prompt, dispatched verbatim
    )


def _reconcile_managed_job(binding: ScheduleBinding) -> None:
    """Make the scheduler match one binding: enabled → (re)registered job,
    disabled → no job. Raises on scheduler failure (callers roll back)."""
    if binding.enabled:
        _scheduler.add_job(
            _run_managed_schedule,
            _apscheduler_trigger(binding.schedule),
            args=[binding.name],
            id=_managed_job_id(binding.name),
            replace_existing=True,
        )
    else:
        _drop_managed_job(binding.name)


def _drop_managed_job(name: str) -> None:
    if _scheduler.get_job(_managed_job_id(name)) is not None:
        _scheduler.remove_job(_managed_job_id(name))


def _register_managed_schedules() -> int:
    """Startup reconciliation: register every enabled binding from disk.
    Unparsable files were already skipped (loudly) by the store."""
    if _schedule_store is None:
        return 0
    count = 0
    for binding in _schedule_store.list():
        try:
            _reconcile_managed_job(binding)
            count += binding.enabled
        except Exception as e:
            logger.error(f"failed to register managed schedule '{binding.name}': {e}", exc_info=True)
    return count


def _next_fire_at(name: str) -> str | None:
    job = _scheduler.get_job(_managed_job_id(name))
    next_run = getattr(job, "next_run_time", None) if job is not None else None
    return next_run.isoformat() if next_run else None


async def _handle_on_complete(output: str) -> None:
    """Dispatch on_complete side effects after an autonomous run."""
    if not _profile or not _profile.on_complete:
        return

    oc = _profile.on_complete
    handlers = registered_handlers()

    if oc.log_to and oc.log_to in handlers:
        await handlers[oc.log_to](_profile.name, output)
        logger.info(f"[{_profile.name}] logged output via '{oc.log_to}'")

    if oc.notify and oc.notify in handlers:
        await handlers[oc.notify](_profile.name, output)
        logger.info(f"[{_profile.name}] notified via '{oc.notify}'")

    if oc.post_to:
        async with httpx.AsyncClient() as client:
            resp = await client.post(str(oc.post_to), json={"output": output})
            resp.raise_for_status()
            logger.info(f"[{_profile.name}] posted output to {oc.post_to}")


# ── Secrets loader ───────────────────────────────────────────────────────────────────

def _load_file_secrets() -> None:
    """
    Resolve *_FILE env vars into their plain counterparts.

    For every var named FOO_FILE whose value is a readable path, reads that
    file and sets FOO to its contents (stripped). The _FILE var is then
    removed so it is not visible to the agent process going forward.

    Example: ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic_key
             → os.environ["ANTHROPIC_API_KEY"] = <file contents>
    """
    file_vars = {k: v for k, v in os.environ.items() if k.endswith("_FILE")}
    for file_var, path in file_vars.items():
        secret_path = Path(path)
        if not secret_path.exists():
            logger.warning(f"Secret file referenced by {file_var} not found: {path}")
            continue
        target_var = file_var[: -len("_FILE")]
        try:
            os.environ[target_var] = secret_path.read_text().strip()
            del os.environ[file_var]
            logger.info(f"Loaded secret {target_var} from {file_var}")
        except OSError as e:
            logger.error(f"Failed to read secret file {path} for {file_var}: {e}")


# ── Lifespan ────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _profile, _agent, _limits, _run_store, _executor, _schedule_store

    _load_file_secrets()

    profile_path = os.environ.get("AGENT_PROFILE", "agent.yaml")
    logger.info(f"Loading agent profile: {profile_path}")

    _profile = load_profile(profile_path)

    _run_store = RunStore(retention=run_retention_from_env())
    interrupted = _run_store.sweep_interrupted()
    if interrupted:
        logger.warning(f"Marked {interrupted} stale 'running' record(s) as interrupted")

    if _profile.is_executor:
        _executor = build_executor(_profile, runs_root=_run_store.root)
        _executor.prepare()
        logger.info(f"Agent '{_profile.name}' built in {_profile.mode} mode (executor tier: {_profile.executor.executor})")
    else:
        _agent, _limits = build_agent(_profile)
        logger.info(f"Agent '{_profile.name}' built in {_profile.mode} mode")

    # Register self-activating triggers (cron, interval, startup)
    interval_i = 0
    startup_i = 0
    for trigger in _profile.triggers:
        if isinstance(trigger, ProfileCronTrigger):
            prompt = trigger.default_prompt or "Run."
            _scheduler.add_job(
                run_agent_scheduled,
                CronTrigger.from_crontab(trigger.schedule),
                args=[prompt],
                id=f"{_profile.name}:cron",
                replace_existing=True,
            )
            logger.info(f"Registered cron trigger: {trigger.schedule}")
        elif isinstance(trigger, ProfileIntervalTrigger):
            prompt = trigger.default_prompt or "Run."
            _scheduler.add_job(
                run_agent_scheduled,
                APIntervalTrigger(seconds=trigger.every_s),
                args=[prompt],
                id=f"{_profile.name}:interval:{interval_i}",
                replace_existing=True,
            )
            logger.info(f"Registered interval trigger: every {trigger.every_s}s")
            interval_i += 1
        elif isinstance(trigger, ProfileStartupTrigger):
            prompt = trigger.default_prompt or "Run."
            run_date = datetime.now(timezone.utc) + timedelta(seconds=trigger.delay_s)
            _scheduler.add_job(
                run_agent_scheduled,
                DateTrigger(run_date=run_date),
                args=[prompt],
                id=f"{_profile.name}:startup:{startup_i}",
                replace_existing=True,
            )
            logger.info(f"Registered startup trigger: delay {trigger.delay_s}s")
            startup_i += 1

    _schedule_store = ScheduleStore()
    managed = _register_managed_schedules()
    if managed:
        logger.info(f"Registered {managed} managed schedule binding(s)")

    _scheduler.start()
    logger.info("Scheduler started")

    yield

    _scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


# ── App ────────────────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)


# ── HTTP trigger schemas ────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    prompt: str
    use_history: bool = False


class RunResponse(BaseModel):
    output: str
    run_id: Optional[str] = None


class RunListResponse(BaseModel):
    count: int
    runs: list[RunSummary]


class HistoryResponse(BaseModel):
    message_count: int
    messages: list[dict]
    run_id: Optional[str] = None


def _apply_trigger_prompt(prompt: str) -> str:
    """Stamp the timestamp and prepend a header_prompt, exactly as /run always has."""
    if _profile and _profile.inject_timestamp:
        prompt = _stamp_prompt(prompt)
    for trigger in (_profile.triggers if _profile else []):
        if hasattr(trigger, "header_prompt") and trigger.header_prompt:
            prompt = f"{trigger.header_prompt.strip()}\n\n{prompt}"
            break
    return prompt


def _require_internal_token(x_miragen_token: Optional[str] = Header(default=None, alias="X-Miragen-Token")) -> None:
    """Guard for /run* and /approvals*: when MIRAGEN_INTERNAL_TOKEN is set, require
    a matching X-Miragen-Token header. Unset (the default) means no enforcement,
    keeping single-container deployments zero-config."""
    expected = os.environ.get("MIRAGEN_INTERNAL_TOKEN")
    if expected and not hmac.compare_digest(x_miragen_token or "", expected):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


_internal_auth = Depends(_require_internal_token)


# ── Routes ───────────────────────────────────────────────────────────────────────────

# Contract capabilities this build serves, for client feature detection
# (miragen-mcp / MiraRun): each entry is a versioned surface a caller may
# rely on. Additions are backwards-compatible; removals/renames are breaking.
CONTRACT_CAPABILITIES = [
    "edf-resolve/mirarun.io-v1alpha1",   # POST /profiles/resolve
    "executor-launch/v1",                # POST /executor-runs (idempotency + provenance)
    "run-snapshot/v1",                   # GET /runs/{id}/snapshot
    "events-cursor/v1",                  # GET /runs/{id}/events?after=
    "managed-schedules/v1",              # GET/PUT/DELETE /schedules (CAS reconciliation)
    "interventions/v1",                  # structured question suspension + answered resume
]


def _installed_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("miragen")
    except Exception:
        return None


@app.get("/health")
async def health():
    last_run = None
    if _run_store is not None:
        recent = _run_store.list(limit=1)
        last_run = recent[0] if recent else None
    return {
        "status": "ok",
        "agent": _profile.name if _profile else None,
        "last_run": last_run,
        "pending_approvals": len(get_broker().pending()),
        "version": _installed_version(),
        "capabilities": CONTRACT_CAPABILITIES,
    }


def _raise_if_daily_budget_exceeded() -> None:
    budget = _daily_budget_status()
    if budget is not None and budget[0] >= budget[1]:
        used, limit = budget
        raise HTTPException(
            status_code=429,
            detail=f"daily token budget exceeded ({used}/{limit} tokens), resets at 00:00 UTC",
        )


@app.post("/run", response_model=RunResponse, dependencies=[_internal_auth])
async def run(request: RunRequest):
    """
    HTTP trigger endpoint. Available for interactive and hybrid agents,
    and for manually triggering autonomous agents outside their cron schedule.
    """
    if _agent is None and _executor is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    _raise_if_daily_budget_exceeded()

    prompt = _apply_trigger_prompt(request.prompt)

    record = (
        _run_store.start(agent_name=_profile.name, trigger="http", prompt=prompt, use_history=request.use_history)
        if _run_store is not None and _profile is not None
        else None
    )

    try:
        output = await run_agent(prompt, use_history=request.use_history, record=record)
        return RunResponse(output=output, run_id=record.run_id if record else None)
    except Exception as e:
        logger.error(f"[{_profile.name if _profile else '?'}] run failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/run/async", status_code=202, dependencies=[_internal_auth])
async def run_async(request: RunRequest):
    """
    Non-blocking variant of /run: starts the run in the background and returns
    immediately with a run_id. Poll GET /runs/{run_id} for the outcome.
    """
    if _agent is None and _executor is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    if _run_store is None or _profile is None:
        raise HTTPException(status_code=503, detail="Run store not ready")
    _raise_if_daily_budget_exceeded()

    prompt = _apply_trigger_prompt(request.prompt)
    record = _run_store.start(
        agent_name=_profile.name, trigger="http_async", prompt=prompt, use_history=request.use_history
    )

    async def _background_run() -> None:
        try:
            await run_agent(prompt, use_history=request.use_history, record=record)
        except Exception as e:
            # run_agent already wrote the failure to the record; this is just
            # so an async-run exception never propagates into a bare task error.
            logger.error(f"[{_profile.name}] async run failed: {e}", exc_info=True)

    _spawn_background(_background_run())

    return {"run_id": record.run_id, "status": "running"}


@app.get("/runs", response_model=RunListResponse, dependencies=[_internal_auth])
async def list_runs(limit: int = 20, status: Optional[str] = None):
    if _run_store is None:
        raise HTTPException(status_code=503, detail="Run store not ready")
    runs = _run_store.list(limit=min(limit, 100), status=status)
    return RunListResponse(count=len(runs), runs=runs)


@app.get("/runs/{run_id}", response_model=RunRecord, dependencies=[_internal_auth])
async def get_run(run_id: str):
    if _run_store is None:
        raise HTTPException(status_code=503, detail="Run store not ready")

    try:
        record = _run_store.get(run_id)
    except AmbiguousRunIdError as e:
        raise HTTPException(
            status_code=404,
            detail={"error": f"ambiguous run_id prefix '{run_id}'", "candidates": e.candidates},
        )

    if record is None:
        recent = [s.run_id for s in _run_store.list(limit=10)]
        raise HTTPException(
            status_code=404,
            detail={"error": f"unknown run_id '{run_id}'", "recent": recent},
        )

    return record


# ── Executor-tier routes ─────────────────────────────────────────────────────
#
# Resume/abandon drive the executor state machine; diff/events expose the
# workspace-in / diff-and-events-out contract to the layers above.

class ResumeRequest(BaseModel):
    prompt: Optional[str] = Field(default=None, min_length=1)
    answer: Optional[InterventionAnswer] = Field(
        default=None,
        description="Structured answer to the run's pending intervention; bound by "
        "intervention_id. May accompany or replace `prompt`.",
    )

    @model_validator(mode="after")
    def validate_has_input(self) -> "ResumeRequest":
        if self.prompt is None and self.answer is None:
            raise ValueError("resume requires `prompt`, `answer`, or both")
        return self


def _render_intervention_answer(pending: InterventionRequest, answer: InterventionAnswer) -> str:
    """Deterministic rendering of a structured answer into the resume prompt
    when the caller supplies no prompt of their own. Mechanical, not
    templated — real prompt rendering stays in the control plane."""
    lines = [f"Your question (intervention {answer.intervention_id}) has been answered."]
    if answer.decision is not None:
        label = next(
            (opt.label for opt in pending.options if opt.id == answer.decision and opt.label),
            None,
        )
        lines.append(f"Decision: {answer.decision}" + (f" ({label})" if label else ""))
    if answer.text is not None:
        lines.append(answer.text)
    lines.append("Continue the task accordingly.")
    return "\n".join(lines)


def _get_executor_record(run_id: str) -> RunRecord:
    if _executor is None:
        raise HTTPException(status_code=400, detail="this agent is not executor-backed")
    if _run_store is None:
        raise HTTPException(status_code=503, detail="Run store not ready")
    try:
        record = _run_store.get(run_id)
    except AmbiguousRunIdError as e:
        raise HTTPException(
            status_code=404,
            detail={"error": f"ambiguous run_id prefix '{run_id}'", "candidates": e.candidates},
        )
    if record is None:
        raise HTTPException(status_code=404, detail={"error": f"unknown run_id '{run_id}'"})
    return record


@app.post("/runs/{run_id}/resume", response_model=RunRecord, dependencies=[_internal_auth])
async def resume_run(run_id: str, request: ResumeRequest):
    """Re-open the executor thread bound to a suspended/failed run and give it
    another turn. The workspace (with any partial diff) is the resume state.

    Structured interventions (issue #33 Phase G): a run suspended with
    exit_reason 'intervention' carries `pending_intervention`; answering it
    means passing `answer` bound to that intervention_id — recorded as an
    `intervention.answered` event (with any approval_ref) before the turn
    runs, so authorization evidence is durable API state, never prompt text.
    A plain-prompt resume of such a run records `intervention.superseded`.
    """
    record = _get_executor_record(run_id)
    if record.status not in ("suspended", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"run is '{record.status}'; only suspended/failed runs are resumable",
        )
    if record.thread_id is None:
        raise HTTPException(
            status_code=409,
            detail="run has no executor thread handle; it cannot be resumed",
        )
    _raise_if_daily_budget_exceeded()

    pending = record.pending_intervention
    if request.answer is not None:
        if pending is None:
            raise HTTPException(
                status_code=409,
                detail="run has no pending intervention to answer; resume with `prompt` instead",
            )
        if request.answer.intervention_id != pending.intervention_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": f"answer targets intervention '{request.answer.intervention_id}' "
                    f"but the pending intervention is '{pending.intervention_id}'",
                    "pending_intervention_id": pending.intervention_id,
                },
            )
        _executor.append_event(record.run_id, {
            "type": "intervention.answered",
            "intervention_id": pending.intervention_id,
            "answer": request.answer.model_dump(mode="json", exclude_none=True),
        })
        prompt = request.prompt or _render_intervention_answer(pending, request.answer)
    else:
        prompt = request.prompt
        if pending is not None:
            _executor.append_event(record.run_id, {
                "type": "intervention.superseded",
                "intervention_id": pending.intervention_id,
            })

    record = _run_store.reopen(record)
    try:
        await _run_executor_turn(prompt, record, resume=True)
    except RuntimeError:
        pass  # outcome (failed:crash) is already on the record; return it
    return _run_store.get(record.run_id)


@app.post("/runs/{run_id}/abandon", response_model=RunRecord, dependencies=[_internal_auth])
async def abandon_run(run_id: str, discard_workspace: bool = False):
    """Human gives up on a suspended/failed run — the only human-terminal
    state, and the only place keep-for-forensics vs discard is decided."""
    record = _get_executor_record(run_id)
    if record.status not in ("suspended", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"run is '{record.status}'; only suspended/failed runs can be abandoned",
        )
    updated = _run_store.finish(
        record,
        status="abandoned",
        output=record.output,
        usage=record.usage,
        exit_reason="abandoned",
    )
    if discard_workspace and record.workspace:
        shutil.rmtree(record.workspace, ignore_errors=True)
        logger.info(f"[{_profile.name}] workspace discarded: {record.workspace}")
    return updated


@app.get("/runs/{run_id}/diff", dependencies=[_internal_auth])
async def get_run_diff(run_id: str, repository: Optional[str] = None):
    """The harvested diff — set exactly once, on terminal success.

    Multi-repo runs: without `repository`, the bundle (all writable repos with
    section markers); with `?repository=<name>`, that repository's own
    apply-able patch from .miragen/diffs/<name>.patch."""
    record = _get_executor_record(run_id)
    if record.diff_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"run '{record.run_id}' has no harvested diff (status: {record.status})",
        )
    path = Path(record.diff_path)
    if repository is not None:
        known = {
            r.name for r in (record.repositories or []) if r.writable
        }
        if repository not in known:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"no harvested per-repository diff for '{repository}'",
                    "writable_repositories": sorted(known),
                },
            )
        path = path.parent / "diffs" / f"{repository}.patch"
    if not path.exists():
        raise HTTPException(status_code=404, detail="diff file missing from workspace volume")
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(path.read_text())


@app.get("/runs/{run_id}/events", dependencies=[_internal_auth])
async def get_run_events(run_id: str, limit: int = 200, after: Optional[int] = None):
    """The executor event stream (turn events, item/tool events, lifecycle
    timing, errors) — what this tier owes the layers above, alongside the
    exit reason.

    Two read modes over the same durable sequenced stream:
    - without `after`: tail read (newest `limit` events), original contract;
    - with `after`: cursor replay — events with seq > after, oldest first,
      plus `next_after`/`has_more` for paging. (run_id, seq) is the
      deduplication key; replaying any cursor is idempotent, so a projector
      can rebuild its projection from after=0 at any time.
    """
    record = _get_executor_record(run_id)
    limit = min(limit, 1000)
    if after is not None:
        page = _executor.read_events_page(record.run_id, after=after, limit=limit)
        return {
            "run_id": record.run_id,
            "count": len(page.events),
            "events": page.events,
            "next_after": page.next_after,
            "has_more": page.has_more,
        }
    events = _executor.read_events(record.run_id, limit=limit)
    return {"run_id": record.run_id, "count": len(events), "events": events}


@app.get("/runs/{run_id}/snapshot", dependencies=[_internal_auth])
async def get_run_snapshot(run_id: str):
    """The immutable resolved-EDF snapshot persisted when the run was
    launched via POST /executor-runs — canonical document, hash, resolved
    profile, and repository/secret plans. 404 for runs launched without an
    EDF."""
    record = _get_executor_record(run_id)
    snapshot = _run_store.read_snapshot(record.run_id)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"run '{record.run_id}' has no resolved snapshot (launched without an EDF)",
        )
    return snapshot


# ── EDF resolution + provenance-carrying launch (issue #33 Phases A/B) ───────


class ResolveRequest(BaseModel):
    edf: dict
    context: Optional[ResolutionContext] = None


class ExecutorLaunchRequest(BaseModel):
    prompt: str = Field(
        description="COMPLETED prompt — persisted and dispatched verbatim: no "
        "timestamp stamping, no header_prompt. Prompt rendering is a "
        "control-plane concern.",
    )
    idempotency_key: str = Field(min_length=1, max_length=200)
    edf: Optional[dict] = None
    context: Optional[ResolutionContext] = None
    expected_sha256: Optional[str] = Field(
        default=None,
        description="Caller's previously resolved canonical hash; launch is "
        "refused (409) if re-resolution at this trust boundary disagrees.",
    )
    provenance: Optional[RunProvenance] = None


def _edf_compatibility(resolved: ResolvedEDF) -> dict | None:
    """Compare a resolved EDF against the agent this service actually runs.
    Informational on /profiles/resolve; enforced (409) on /executor-runs —
    Stage 1 keeps the current one-agent-per-service topology, so a launch
    executes with the CONFIGURED executor spec and an EDF that resolves to a
    different executor/model/sandbox belongs to a different deployment."""
    if _profile is None:
        return None
    issues: list[str] = []
    if _profile.executor is None:
        issues.append("configured agent is model-tier; EDF launches require an executor-tier agent")
    else:
        want = resolved.resolved_profile["executor"]
        have = _profile.executor
        if want["executor"] != have.executor:
            issues.append(f"EDF resolves to executor '{want['executor']}' but this agent runs '{have.executor}'")
        if want["model"] is not None and want["model"] != have.model:
            issues.append(f"EDF resolves to model '{want['model']}' but this agent is configured for '{have.model}'")
        if want["sandbox_mode"] != have.sandbox_mode:
            issues.append(
                f"EDF resolves to sandbox_mode '{want['sandbox_mode']}' but this agent "
                f"runs '{have.sandbox_mode}'"
            )
    return {"compatible": not issues, "issues": issues}


@app.post("/profiles/resolve", dependencies=[_internal_auth])
async def resolve_profile(request: ResolveRequest):
    """Validate and resolve an EDF WITHOUT starting a run: strict validation,
    deterministic default/preset expansion, canonical document + SHA-256, the
    resolved executable profile, and repository/secret binding plans. Pure and
    deterministic — safe to call repeatedly to reproduce a hash."""
    try:
        resolved = resolve_edf(request.edf, context=request.context)
    except EDFValidationError as e:
        raise HTTPException(status_code=422, detail={"error": "invalid EDF", "errors": e.errors})
    body = resolved.model_dump(mode="json")
    body["agent_compatibility"] = _edf_compatibility(resolved)
    return body


@app.post("/executor-runs", status_code=202, dependencies=[_internal_auth])
async def launch_executor_run(request: ExecutorLaunchRequest, response: Response):
    """Idempotent, provenance-carrying executor launch.

    Acceptance is DURABLE-FIRST: the run record (with provenance and, when an
    EDF is supplied, the resolved snapshot + hash) is persisted before this
    endpoint acknowledges — and before dispatch. Recovery contract for the
    ambiguity window: if the process dies after acceptance but before/while
    dispatching, the startup sweep marks the record 'interrupted'; retrying
    the same idempotency_key then returns that original run (200, duplicate:
    true) instead of launching twice, and the caller decides how to proceed
    with full knowledge of its status.
    """
    if _executor is None:
        raise HTTPException(
            status_code=400,
            detail="this agent is not executor-backed; /executor-runs requires an executor-tier profile",
        )
    if _run_store is None or _profile is None:
        raise HTTPException(status_code=503, detail="Run store not ready")

    existing = _run_store.find_by_idempotency_key(request.idempotency_key)
    if existing is not None:
        response.status_code = 200
        return {
            "run_id": existing.run_id,
            "status": existing.status,
            "snapshot_sha256": existing.snapshot_sha256,
            "duplicate": True,
        }

    _raise_if_daily_budget_exceeded()

    resolved: ResolvedEDF | None = None
    if request.edf is not None:
        try:
            resolved = resolve_edf(request.edf, context=request.context)
        except EDFValidationError as e:
            raise HTTPException(status_code=422, detail={"error": "invalid EDF", "errors": e.errors})
        if request.expected_sha256 is not None and resolved.sha256 != request.expected_sha256:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "canonical snapshot hash mismatch",
                    "expected_sha256": request.expected_sha256,
                    "resolved_sha256": resolved.sha256,
                },
            )
        compatibility = _edf_compatibility(resolved)
        if compatibility is not None and not compatibility["compatible"]:
            raise HTTPException(
                status_code=409,
                detail={"error": "EDF incompatible with the configured agent", "issues": compatibility["issues"]},
            )
    elif request.expected_sha256 is not None:
        raise HTTPException(status_code=422, detail="expected_sha256 requires an edf to resolve")

    checkouts: list[RepositoryCheckout] | None = None
    if resolved is not None and resolved.repository_plan:
        # Workspace preparation needs an authorized runtime binding for every
        # opaque connectionRef. Refusing up front beats a mid-run clone
        # failure — and bindings are ephemeral, so they must arrive with the
        # launch. They are consumed here and never persisted.
        bindings = request.context.repositories if request.context is not None else {}
        missing = [
            entry.connection_ref
            for entry in resolved.repository_plan
            if entry.connection_ref not in bindings
        ]
        if missing:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "missing repository bindings",
                    "missing_connection_refs": sorted(set(missing)),
                    "hint": "supply context.repositories[connectionRef].clone_url for each repository",
                },
            )
        checkouts = [
            RepositoryCheckout(
                name=entry.name,
                ref=entry.ref,
                mount_path=entry.mount_path,
                writable=entry.writable,
                clone_url=bindings[entry.connection_ref].clone_url,
            )
            for entry in resolved.repository_plan
        ]

    provenance = (request.provenance or RunProvenance()).model_copy(
        update={
            "idempotency_key": request.idempotency_key,
            **(
                {"edf_api_version": resolved.api_version}
                if resolved is not None
                else {}
            ),
        }
    )

    # Durable acceptance point — no awaits between the idempotency lookup
    # above and this write, so a same-key race cannot slip between them.
    record = _run_store.start(
        agent_name=_profile.name,
        trigger="launch",
        prompt=request.prompt,
        executor=_profile.executor.executor,
        model=_profile.executor.model,
        snapshot_sha256=resolved.sha256 if resolved is not None else None,
        provenance=provenance,
        repositories=[
            RepositoryRevision(
                name=entry.name,
                ref=entry.ref,
                mount_path=entry.mount_path,
                writable=entry.writable,
                commit=entry.commit,
            )
            for entry in resolved.repository_plan
        ]
        if resolved is not None
        else None,
    )
    if resolved is not None:
        _run_store.write_snapshot(
            record.run_id,
            build_run_snapshot(resolved, run_id=record.run_id, created_at=record.started_at.isoformat()),
        )

    async def _background_run() -> None:
        try:
            await run_agent(request.prompt, record=record, repositories=checkouts)
        except Exception as e:
            # run_agent already wrote the failure to the record; this is just
            # so a launch exception never propagates into a bare task error.
            logger.error(f"[{_profile.name}] launched run failed: {e}", exc_info=True)

    _spawn_background(_background_run())

    return {
        "run_id": record.run_id,
        "status": "running",
        "snapshot_sha256": record.snapshot_sha256,
        "duplicate": False,
    }


# ── Managed schedule reconciliation API (issue #33 Phase F) ──────────────────


class ScheduleBindingRequest(BaseModel):
    schedule: ScheduleSpec
    prompt: str = Field(min_length=1)
    enabled: bool = True
    provenance: Optional[RunProvenance] = None
    metadata: dict[str, str] = Field(default_factory=dict)
    expected_version: Optional[int] = Field(
        default=None,
        ge=1,
        description="Omit to CREATE (409 if the binding exists); pass the last-read "
        "version to UPDATE (409 on mismatch, carrying current state).",
    )


def _binding_response(binding: ScheduleBinding) -> dict:
    body = binding.model_dump(mode="json")
    body["next_fire_at"] = _next_fire_at(binding.name)
    return body


def _require_schedule_store() -> ScheduleStore:
    if _schedule_store is None:
        raise HTTPException(status_code=503, detail="Schedule store not ready")
    return _schedule_store


@app.get("/schedules", dependencies=[_internal_auth])
async def list_schedules():
    store = _require_schedule_store()
    bindings = [_binding_response(b) for b in store.list()]
    return {"count": len(bindings), "schedules": bindings}


@app.get("/schedules/{name}", dependencies=[_internal_auth])
async def get_schedule(name: str):
    store = _require_schedule_store()
    binding = store.get(name)
    if binding is None:
        raise HTTPException(status_code=404, detail=f"unknown schedule binding '{name}'")
    return _binding_response(binding)


@app.put("/schedules/{name}", dependencies=[_internal_auth])
async def put_schedule(name: str, request: ScheduleBindingRequest, response: Response):
    """Create (no expected_version) or update (compare-and-swap) one managed
    schedule binding, then reconcile the scheduler job. File first, scheduler
    second — a scheduler failure rolls the file back so the store never
    claims a binding the scheduler doesn't hold."""
    store = _require_schedule_store()
    if _profile is not None and _profile.mode == "interactive":
        # Same rule as profile triggers: a managed binding is still
        # self-activation. Hybrid mode is the intended shape.
        raise HTTPException(
            status_code=409,
            detail="interactive agents cannot have schedule bindings; run the agent in hybrid mode",
        )

    prior = store.get(name)
    try:
        binding = store.upsert(
            name,
            schedule=request.schedule,
            prompt=request.prompt,
            enabled=request.enabled,
            provenance=request.provenance,
            metadata=request.metadata,
            expected_version=request.expected_version,
        )
    except BindingConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(e),
                "current": e.current.model_dump(mode="json") if e.current else None,
            },
        )
    except ValueError as e:  # binding name pattern violations etc.
        raise HTTPException(status_code=422, detail=str(e))

    try:
        _reconcile_managed_job(binding)
    except Exception as e:
        # roll the store back to what the scheduler still holds
        if prior is not None:
            store.save(prior)
        else:
            try:
                store.delete(name)
            except KeyError:
                pass
        logger.error(f"scheduler reconciliation failed for '{name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"scheduler rejected the binding: {e}")

    response.status_code = 200 if prior is not None else 201
    return _binding_response(binding)


@app.delete("/schedules/{name}", dependencies=[_internal_auth])
async def delete_schedule(name: str, expected_version: Optional[int] = None):
    """Remove a binding and unregister its job. In-flight runs it fired are
    untouched — deletion only stops future firings."""
    store = _require_schedule_store()
    try:
        removed = store.delete(name, expected_version=expected_version)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown schedule binding '{name}'")
    except BindingConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(e),
                "current": e.current.model_dump(mode="json") if e.current else None,
            },
        )
    _drop_managed_job(name)
    return {"deleted": removed.model_dump(mode="json")}


@app.get("/history", response_model=HistoryResponse, dependencies=[_internal_auth])
async def get_history(limit: int = 20, run_id: Optional[str] = None):
    """
    Read-only view of the conversation history persisted at HISTORY_FILE.

    Without run_id: newest `limit` messages (default 20, max 200).
    With run_id: the message slice history.json held right after that run saved
    it, recovered via history.runs.jsonl. 404 if that run never saved history.
    """
    messages = _load_history_messages()

    if run_id is not None:
        message_count = _sidecar_message_count(run_id)
        if message_count is None:
            raise HTTPException(status_code=404, detail={"error": f"no history entry for run_id '{run_id}'"})
        sliced = messages[:message_count]
        return HistoryResponse(
            message_count=len(sliced),
            messages=simplify_history_messages(sliced),
            run_id=run_id,
        )

    capped = max(0, min(limit, 200))
    sliced = messages[-capped:] if capped else []
    return HistoryResponse(
        message_count=len(sliced),
        messages=simplify_history_messages(sliced),
        run_id=None,
    )


class ApprovalListResponse(BaseModel):
    count: int
    approvals: list[PendingApproval]


class ResolveApprovalResponse(BaseModel):
    resolved: bool


@app.get("/approvals", response_model=ApprovalListResponse, dependencies=[_internal_auth])
async def list_approvals():
    pending = get_broker().pending()
    return ApprovalListResponse(count=len(pending), approvals=pending)


@app.post("/approvals/{request_id}", response_model=ResolveApprovalResponse, dependencies=[_internal_auth])
async def resolve_approval(request_id: str, response: ApprovalResponse):
    broker = get_broker()
    if not broker.resolve(request_id, response):
        pending_ids = [p.request.request_id for p in broker.pending()]
        raise HTTPException(
            status_code=404,
            detail={"error": f"unknown, already resolved, or expired approval '{request_id}'", "pending": pending_ids},
        )
    return ResolveApprovalResponse(resolved=True)


@app.post("/run/stream", dependencies=[_internal_auth])
async def run_stream(request: RunRequest):
    """
    Streaming variant of /run for interactive and hybrid agents.
    Returns a text/event-stream response.
    """
    if _executor is not None:
        raise HTTPException(
            status_code=400,
            detail="executor-backed agents do not stream text; poll GET /runs/{run_id}/events instead",
        )
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")

    prompt = _apply_trigger_prompt(request.prompt)

    history = None
    if request.use_history:
        try:
            if HISTORY_FILE.exists():
                history = _cap_history(ModelMessagesTypeAdapter.validate_json(HISTORY_FILE.read_bytes()))
        except Exception:
            logger.warning("Failed to load history for stream, starting fresh")

    record = (
        _run_store.start(agent_name=_profile.name, trigger="http", prompt=prompt, use_history=request.use_history)
        if _run_store is not None and _profile is not None
        else None
    )

    async def event_stream():
        chunks: list[str] = []
        try:
            async with _agent.run_stream(prompt, usage_limits=_limits, message_history=history) as stream:
                async for chunk in stream.stream_text(delta=True):
                    chunks.append(chunk)
                    yield f"data: {chunk}\n\n"
                if request.use_history:
                    try:
                        messages = stream.all_messages()
                        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                        HISTORY_FILE.write_bytes(ModelMessagesTypeAdapter.dump_json(messages))
                        _append_history_sidecar(record.run_id if record else None, len(messages))
                    except Exception:
                        logger.warning("Failed to save history after stream")
        except Exception as e:
            if record is not None and _run_store is not None:
                _run_store.finish(record, status="failed", error=str(e), output="".join(chunks) or None)
            raise
        if record is not None and _run_store is not None:
            try:
                usage, tool_calls = extract_run_details(stream)
            except Exception:
                usage, tool_calls = None, []
            _run_store.finish(record, status="succeeded", output="".join(chunks), usage=usage, tool_calls=tool_calls)
        yield "data: [DONE]\n\n"

    headers = {"X-Miragen-Run-Id": record.run_id} if record is not None else None
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
