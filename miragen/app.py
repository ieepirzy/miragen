from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger as APIntervalTrigger
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.usage import UsageLimits

from miragen.factory import build_agent, registered_handlers
from miragen.load import load_profile
from miragen.models import (
    AgentProfile,
    CronTrigger as ProfileCronTrigger,
    IntervalTrigger as ProfileIntervalTrigger,
    RunRecord,
    RunSummary,
    StartupTrigger as ProfileStartupTrigger,
)
from miragen.runs import AmbiguousRunIdError, RunStore, extract_run_details, run_retention_from_env, tokens_used_since

logger = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────────────────────────────────

_profile: AgentProfile | None = None
_agent: Agent | None = None
_limits: UsageLimits | None = None
_scheduler: AsyncIOScheduler = AsyncIOScheduler()
_run_store: RunStore | None = None

HISTORY_FILE = Path("/agent/history.json")

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


def _daily_budget_exceeded() -> bool:
    """Return True if the daily token budget is exceeded."""
    if _run_store is None or _profile is None:
        return False
    if _profile.limits is None or _profile.limits.tokens_per_day is None:
        return False
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    used = tokens_used_since(_run_store, today_start)
    limit = _profile.limits.tokens_per_day
    if used >= limit:
        logger.warning(
            f"[{_profile.name}] daily token budget exceeded: {used}/{limit} tokens used today"
        )
        return True
    return False


# ── Agent runner ──────────────────────────────────────────────────────────────────

async def run_agent(prompt: str, use_history: bool = False, record: RunRecord | None = None) -> str:
    """
    Core agent execution. Called by cron/interval/startup and HTTP triggers.

    If `record` is given (from _run_store.start()), the run's outcome — success
    with usage/tool-calls, or failure with the error — is written back to it via
    _run_store.finish() before returning (or before the exception propagates).
    """
    assert _agent is not None, "Agent not initialized"

    history = None
    if use_history:
        try:
            if HISTORY_FILE.exists():
                history = ModelMessagesTypeAdapter.validate_json(HISTORY_FILE.read_bytes())
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
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_bytes(ModelMessagesTypeAdapter.dump_json(result.all_messages()))
        except Exception:
            logger.warning("Failed to save history")

    output = str(result.output)

    if record is not None and _run_store is not None:
        usage, tool_calls = extract_run_details(result)
        _run_store.finish(record, status="succeeded", output=output, usage=usage, tool_calls=tool_calls)

    return output


async def run_agent_scheduled(prompt: str) -> None:
    """Scheduler-triggered run (cron, interval, or startup). Handles on_complete side effects."""
    assert _profile is not None
    logger.info(f"[{_profile.name}] scheduled run triggered")

    if _daily_budget_exceeded():
        logger.info(f"[{_profile.name}] scheduled run skipped: daily token budget exceeded")
        if (
            _profile.limits is not None
            and _profile.limits.on_exceeded == "notify"
            and _profile.on_complete is not None
        ):
            try:
                await _handle_on_complete("Daily token budget exceeded — run skipped.")
            except Exception as e:
                logger.error(f"[{_profile.name}] on_complete notify failed: {e}", exc_info=True)
        return

    if _profile.inject_timestamp:
        prompt = _stamp_prompt(prompt)

    record = (
        _run_store.start(agent_name=_profile.name, trigger="cron", prompt=prompt)
        if _run_store is not None
        else None
    )

    try:
        output = await run_agent(prompt, record=record)
        logger.info(f"[{_profile.name}] scheduled run complete")
    except Exception as e:
        logger.error(f"[{_profile.name}] scheduled run failed: {e}", exc_info=True)
        return

    try:
        await _handle_on_complete(output)
    except Exception as e:
        logger.error(f"[{_profile.name}] on_complete failed: {e}", exc_info=True)


# Alias — kept so existing imports of `run_agent_cron` (e.g. in tests) don't break.
run_agent_cron = run_agent_scheduled


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
            await client.post(str(oc.post_to), json={"output": output})
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
    global _profile, _agent, _limits, _run_store

    _load_file_secrets()

    profile_path = os.environ.get("AGENT_PROFILE", "agent.yaml")
    logger.info(f"Loading agent profile: {profile_path}")

    _profile = load_profile(profile_path)
    _agent, _limits = build_agent(_profile)

    _run_store = RunStore(retention=run_retention_from_env())
    interrupted = _run_store.sweep_interrupted()
    if interrupted:
        logger.warning(f"Marked {interrupted} stale 'running' record(s) as interrupted")

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


def _apply_trigger_prompt(prompt: str) -> str:
    """Stamp the timestamp and prepend a header_prompt, exactly as /run always has."""
    if _profile and _profile.inject_timestamp:
        prompt = _stamp_prompt(prompt)
    for trigger in (_profile.triggers if _profile else []):
        if hasattr(trigger, "header_prompt") and trigger.header_prompt:
            prompt = f"{trigger.header_prompt.strip()}\n\n{prompt}"
            break
    return prompt


# ── Routes ───────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    last_run = None
    if _run_store is not None:
        recent = _run_store.list(limit=1)
        last_run = recent[0] if recent else None
    return {"status": "ok", "agent": _profile.name if _profile else None, "last_run": last_run}


@app.post("/run", response_model=RunResponse)
async def run(request: RunRequest):
    """
    HTTP trigger endpoint. Available for interactive and hybrid agents,
    and for manually triggering autonomous agents outside their cron schedule.
    """
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")

    if _daily_budget_exceeded():
        limit = _profile.limits.tokens_per_day if _profile and _profile.limits else 0
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        used = tokens_used_since(_run_store, today_start) if _run_store else 0
        raise HTTPException(
            status_code=429,
            detail=f"daily token budget exceeded ({used}/{limit} tokens), resets at 00:00 UTC",
        )

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


@app.post("/run/async", status_code=202)
async def run_async(request: RunRequest):
    """
    Non-blocking variant of /run: starts the run in the background and returns
    immediately with a run_id. Poll GET /runs/{run_id} for the outcome.
    """
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    if _run_store is None or _profile is None:
        raise HTTPException(status_code=503, detail="Run store not ready")

    if _daily_budget_exceeded():
        limit = _profile.limits.tokens_per_day if _profile.limits else 0
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        used = tokens_used_since(_run_store, today_start)
        raise HTTPException(
            status_code=429,
            detail=f"daily token budget exceeded ({used}/{limit} tokens), resets at 00:00 UTC",
        )

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


@app.get("/runs", response_model=RunListResponse)
async def list_runs(limit: int = 20, status: Optional[str] = None):
    if _run_store is None:
        raise HTTPException(status_code=503, detail="Run store not ready")
    runs = _run_store.list(limit=min(limit, 100), status=status)
    return RunListResponse(count=len(runs), runs=runs)


@app.get("/runs/{run_id}", response_model=RunRecord)
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


@app.post("/run/stream")
async def run_stream(request: RunRequest):
    """
    Streaming variant of /run for interactive and hybrid agents.
    Returns a text/event-stream response.
    """
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")

    prompt = _apply_trigger_prompt(request.prompt)

    history = None
    if request.use_history:
        try:
            if HISTORY_FILE.exists():
                history = ModelMessagesTypeAdapter.validate_json(HISTORY_FILE.read_bytes())
        except Exception:
            logger.warning("Failed to load history for stream, starting fresh")

    async def event_stream():
        async with _agent.run_stream(prompt, usage_limits=_limits, message_history=history) as stream:
            async for chunk in stream.stream_text(delta=True):
                yield f"data: {chunk}\n\n"
            if request.use_history:
                try:
                    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                    HISTORY_FILE.write_bytes(ModelMessagesTypeAdapter.dump_json(stream.all_messages()))
                except Exception:
                    logger.warning("Failed to save history after stream")
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
