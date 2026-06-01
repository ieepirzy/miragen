from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic_ai import Agent

from miragen.factory import build_agent, registered_handlers
from miragen.load import load_profile
from miragen.models import AgentProfile, CronTrigger as ProfileCronTrigger

logger = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────────────────────────

_profile: AgentProfile | None = None
_agent: Agent | None = None
_scheduler: AsyncIOScheduler = AsyncIOScheduler()


# ── Agent runner ─────────────────────────────────────────────────────────────

async def run_agent(prompt: str) -> str:
    """Core agent execution. Called by both cron and HTTP triggers."""
    assert _agent is not None, "Agent not initialized"
    result = await _agent.run(prompt)
    return str(result.output)


async def run_agent_cron(prompt: str) -> None:
    """Cron-triggered run. Handles on_complete side effects."""
    assert _profile is not None
    logger.info(f"[{_profile.name}] cron run triggered")

    try:
        output = await run_agent(prompt)
        logger.info(f"[{_profile.name}] cron run complete")
        await _handle_on_complete(output)
    except Exception as e:
        logger.error(f"[{_profile.name}] cron run failed: {e}", exc_info=True)


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


# ── Secrets loader ───────────────────────────────────────────────────────────

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
        os.environ[target_var] = secret_path.read_text().strip()
        del os.environ[file_var]
        logger.info(f"Loaded secret {target_var} from {file_var}")


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _profile, _agent

    _load_file_secrets()

    profile_path = os.environ.get("AGENT_PROFILE", "agent.yaml")
    logger.info(f"Loading agent profile: {profile_path}")

    _profile = load_profile(profile_path)
    _agent = build_agent(_profile)

    logger.info(f"Agent '{_profile.name}' built in {_profile.mode} mode")

    # Register cron triggers
    for trigger in _profile.triggers:
        if isinstance(trigger, ProfileCronTrigger):
            prompt = trigger.default_prompt or "Run."
            _scheduler.add_job(
                run_agent_cron,
                CronTrigger.from_crontab(trigger.schedule),
                args=[prompt],
                id=f"{_profile.name}:cron",
                replace_existing=True,
            )
            logger.info(f"Registered cron trigger: {trigger.schedule}")

    _scheduler.start()
    logger.info("Scheduler started")

    yield

    _scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)


# ── HTTP trigger schemas ──────────────────────────────────────────────────────

class RunRequest(BaseModel):
    prompt: str


class RunResponse(BaseModel):
    output: str


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "agent": _profile.name if _profile else None}


@app.post("/run", response_model=RunResponse)
async def run(request: RunRequest):
    """
    HTTP trigger endpoint. Available for interactive and hybrid agents,
    and for manually triggering autonomous agents outside their cron schedule.
    """
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")

    # Prepend header_prompt if configured on the http trigger
    prompt = request.prompt
    for trigger in (_profile.triggers if _profile else []):
        if hasattr(trigger, "header_prompt") and trigger.header_prompt:
            prompt = f"{trigger.header_prompt.strip()}\n\n{prompt}"
            break

    try:
        output = await run_agent(prompt)
        return RunResponse(output=output)
    except Exception as e:
        logger.error(f"[{_profile.name if _profile else '?'}] run failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/run/stream")
async def run_stream(request: RunRequest):
    """
    Streaming variant of /run for interactive and hybrid agents.
    Returns a text/event-stream response.
    """
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not ready")

    prompt = request.prompt
    for trigger in (_profile.triggers if _profile else []):
        if hasattr(trigger, "header_prompt") and trigger.header_prompt:
            prompt = f"{trigger.header_prompt.strip()}\n\n{prompt}"
            break

    async def event_stream():
        async with _agent.run_stream(prompt) as stream:
            async for chunk in stream.stream_text(delta=True):
                yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")