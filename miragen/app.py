from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import logging
import os
import re
import shutil
import uuid
from types import SimpleNamespace
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger as APIntervalTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.usage import UsageLimits

from miragen import events as run_events
from miragen.broker import PendingApproval, get_broker
from miragen.edf import (
    EDFValidationError,
    ResolutionContext,
    ResolvedEDF,
    build_run_snapshot,
    resolve_edf,
)
from miragen.executor import ExecutorBackend, ExecutorResult, RepositoryCheckout, build_executor
from miragen.factory import build_agent, registered_handlers, registered_tools
from miragen.harness import (
    PYDANTIC_AI, Harness, HarnessTurn, InstanceBusyError, PydanticAIHarness, build_model_harness, profile_harness,
    pydantic_ai_model,
)
from miragen.load import load_profile
from miragen.profile_contract import SUPPORTED_PROFILE_CONTRACTS
from miragen.models import (
    DEFAULT_INSTANCE,
    INSTANCE_NAME_PATTERN,
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
from miragen.publication import (
    PublicationConfigError,
    PublicationError,
    PublicationRequest,
    PublicationResponse,
    PublicationStore,
    PublicationUnavailableError,
    build_publication_backend,
    publish_reviewed_run,
)
from miragen.runs import (
    AmbiguousRunIdError,
    RunStore,
    reserved_tokens_in_flight,
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
from miragen.intervention_mcp import build_ask_human_mcp
from miragen.telemetry import MiragenTelemetry, telemetry_from_env
from miragen.memory import MemoryClient, MemoryLifecycle
from miragen.runtime_tools.scheduling import (
    SchedulingBackend,
    build_scheduling_mcp,
    build_scheduling_tools,
)
from miragen.memory.tools import build_memory_tools
from miragen.memory_mcp import build_memory_mcp
from miragen.voice import (
    SpeechAudio, VoiceBackend, build_voice_backend, load_speak_guidance,
)
from miragen.voice_mcp import build_voice_mcp

logger = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────────────────────────────────

_profile: AgentProfile | None = None
_agent: Agent | None = None
_limits: UsageLimits | None = None
# A long-lived non-PydanticAI harness (e.g. Grok Build) for base-tier
# profiles whose spec.model names one. None for pydantic-ai profiles: their
# harness wraps the _agent/_limits globals above (see _model_harness).
_harness: Harness | None = None
# memory.backend: bridge — the hosted session plane this agent reports to.
_bridge_memory = None
# The tool gateway a non-PydanticAI harness acts through (served at
# /mcp/gateway with its own per-instance credentials).
_gateway = None
# voice.instructions_file contents: renderer guidance appended to the
# system instructions (base tier, every harness).
_speak_guidance: str | None = None
_scheduler: AsyncIOScheduler = AsyncIOScheduler()
_run_store: RunStore | None = None
_executor: "ExecutorBackend | None" = None
_schedule_store: ScheduleStore | None = None
_publication_store: PublicationStore | None = None
# Test seam: inject a PublicationBackend (or factory) without hitting the network.
_publication_backend_override: object | None = None
_telemetry: MiragenTelemetry | None = None
_voice: "VoiceBackend | None" = None
_memory: "MemoryLifecycle | None" = None
_scheduling: "SchedulingBackend | None" = None

# The run/instance a model-tier tool call belongs to (speak's artifact
# storage, memory's state scope) — set around agent.run() so closures see
# their own run even with several concurrent (different-instance) turns.
_current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "miragen_current_run_id", default=None
)
_current_instance: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "miragen_current_instance", default=None
)

# Per-instance conversation state (instance model ADR: docs/design/
# instance-model.md). One history file per named instance; the pre-instance
# singleton paths remain only as the migration source — lifespan adopts them
# as the `default` instance's files on first boot.
HISTORIES_DIR = Path("/agent/histories")
LEGACY_HISTORY_FILE = Path("/agent/history.json")
LEGACY_HISTORY_SIDECAR = Path("/agent/history.runs.jsonl")

_INSTANCE_RE = re.compile(INSTANCE_NAME_PATTERN)

# Keeps references to fire-and-forget /run/async tasks so they aren't
# garbage-collected mid-run (a well-known asyncio.create_task gotcha).
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ── Admission control (instances/v1) ─────────────────────────────────────────
#
# Two bounds, both non-blocking by decision (overflow answers 429 / skips a
# scheduled fire; queueing is deliberately out of scope — see the ADR):
#   - a container-wide cap on concurrently running turns, and
#   - at most one running turn per named instance, which is what makes
#     per-instance history writes safe by construction.
# Plain counter + set, no locks: every acquire/release runs synchronously on
# the single event loop (all handlers here are `async def`, so none are moved
# to a threadpool).

DEFAULT_MAX_CONCURRENT_RUNS = 4

_active_runs = 0
_busy_instances: set[str] = set()


class RunBusy(Exception):
    """Admission refused. `retry_after_s` sizes the Retry-After header."""

    def __init__(self, detail: str, retry_after_s: int = 15) -> None:
        super().__init__(detail)
        self.retry_after_s = retry_after_s


def _max_concurrent_runs() -> int:
    env = os.environ.get("MIRAGEN_MAX_CONCURRENT")
    if env:
        return int(env)
    if _profile is not None and _profile.limits is not None and _profile.limits.max_concurrent_runs:
        return _profile.limits.max_concurrent_runs
    return DEFAULT_MAX_CONCURRENT_RUNS


def _check_instance_name(instance: str) -> str:
    if not _INSTANCE_RE.fullmatch(instance):
        raise HTTPException(
            status_code=422,
            detail=f"invalid instance name '{instance}': must match {INSTANCE_NAME_PATTERN}",
        )
    return instance


def _acquire_run_slot(instance: str | None) -> Callable[[], None]:
    """Claim one run slot (and the instance's turn, when named). Returns an
    idempotent release callable; raises RunBusy when either bound is hit.
    The busy-instance answer comes first — "this conversation is mid-turn"
    is more actionable than "the container is full" when both are true."""
    global _active_runs
    if instance is not None and instance in _busy_instances:
        raise RunBusy(
            f"instance '{instance}' already has a running turn — one turn per "
            "instance; retry after it finishes",
        )
    limit = _max_concurrent_runs()
    if _active_runs >= limit:
        raise RunBusy(
            f"container is at max_concurrent_runs ({_active_runs}/{limit}) — "
            "retry later or raise limits.max_concurrent_runs / MIRAGEN_MAX_CONCURRENT",
            retry_after_s=30,
        )
    _active_runs += 1
    if instance is not None:
        _busy_instances.add(instance)
    released = False

    def release() -> None:
        nonlocal released
        global _active_runs
        if released:
            return
        released = True
        _active_runs -= 1
        if instance is not None:
            _busy_instances.discard(instance)

    return release


def _admit_or_429(instance: str | None) -> Callable[[], None]:
    try:
        return _acquire_run_slot(instance)
    except RunBusy as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_s)},
        ) from exc


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


def _history_file(instance: str) -> Path:
    return HISTORIES_DIR / f"{instance}.json"


def _history_sidecar(instance: str) -> Path:
    return HISTORIES_DIR / f"{instance}.runs.jsonl"


def _load_history_messages(instance: str) -> list:
    """Read + validate the instance's history; missing or unparsable = empty."""
    path = _history_file(instance)
    if not path.exists():
        return []
    try:
        return ModelMessagesTypeAdapter.validate_json(path.read_bytes())
    except Exception:
        logger.warning(f"Failed to parse {path.name}")
        return []


def _save_history_messages(instance: str, messages: list, run_id: str | None) -> None:
    path = _history_file(instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ModelMessagesTypeAdapter.dump_json(messages))
    _append_history_sidecar(instance, run_id, len(messages))


def _guidance_kwargs() -> dict:
    """build_agent's system_guidance, passed only when there is some."""
    return {"system_guidance": _speak_guidance} if _speak_guidance else {}


@contextmanager
def _bind_run_context(run_id: str | None, instance: str | None):
    """Run/instance context for a runtime tool called through the gateway
    (the same contextvars run_agent sets around a PydanticAI run)."""
    run_token = _current_run_id.set(run_id)
    instance_token = _current_instance.set(instance)
    try:
        yield
    finally:
        _current_run_id.reset(run_token)
        _current_instance.reset(instance_token)


def _model_ready() -> bool:
    """A base-tier harness is available for turns."""
    return _harness is not None or _agent is not None


def _model_harness() -> Harness:
    """The harness running this profile's base-tier turns.

    A dedicated harness (Grok Build, …) is long-lived and owns its own
    state. The PydanticAI harness is a thin wrapper around the startup
    agent, rebuilt on each call so it always sees the current `_agent`."""
    if _harness is not None:
        return _harness
    assert _agent is not None, "Agent not initialized"
    return PydanticAIHarness(
        _agent,
        _limits,
        build_run_agent=lambda secret_env, extra_instructions: build_agent(
            _profile,
            telemetry=_telemetry,
            secret_env=secret_env,
            extra_tools=_runtime_extra_tools(),
            extra_instructions=extra_instructions,
            **_guidance_kwargs(),
        ),
        load_history=lambda instance: _cap_history(_load_history_messages(instance)),
        save_history=_save_history_messages,
    )


def _sidecar_message_count(instance: str, run_id: str) -> int | None:
    """
    message_count recorded in the instance's sidecar the last time `run_id`
    saved history, or None if that run never appears there. History only ever
    grows by appending, so that count is also the prefix length of the current
    history at save time.
    """
    sidecar = _history_sidecar(instance)
    if not sidecar.exists():
        return None
    count = None
    for line in sidecar.read_text().splitlines():
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


def _migrate_legacy_history() -> None:
    """Adopt the pre-instance singleton history as the `default` instance's
    files. One-shot and best-effort: an already-migrated (or absent) legacy
    file is a no-op, and a failure only warns — the run path treats
    unreadable history as empty either way."""
    try:
        target = _history_file(DEFAULT_INSTANCE)
        if not LEGACY_HISTORY_FILE.exists() or target.exists():
            return
        HISTORIES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(LEGACY_HISTORY_FILE), str(target))
        if LEGACY_HISTORY_SIDECAR.exists():
            shutil.move(str(LEGACY_HISTORY_SIDECAR), str(_history_sidecar(DEFAULT_INSTANCE)))
        logger.info(
            f"Migrated {LEGACY_HISTORY_FILE} to {target} (the 'default' instance)"
        )
    except Exception:
        logger.warning("Failed to migrate legacy history.json", exc_info=True)


# ── Voice (docs/design/voice.md) ─────────────────────────────────────────────


def _audio_dir(run_id: str) -> Path:
    assert _run_store is not None
    return _run_store.root / run_id / "audio"


def _store_speech_audio(run_id: str | None, audio: SpeechAudio) -> str | None:
    """Write synthesized audio into the run's audio/ directory. None run (or
    no run store) = nowhere to keep it; the caller reports that honestly."""
    if run_id is None or _run_store is None:
        return None
    directory = _audio_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    ordinal = sum(1 for p in directory.iterdir() if p.is_file()) + 1
    path = directory / f"{ordinal:03d}.{audio.extension}"
    path.write_bytes(audio.data)
    return str(path)


def _resolve_and_store_audio(run_id: str | None, audio: SpeechAudio) -> str | None:
    """The /mcp/voice store callable: an explicit run_id wins; otherwise the
    single running run (the common one-agent-one-turn case, same resolution
    rule as ask_human). Ambiguous or none = not stored."""
    if _run_store is None:
        return None
    if run_id is None:
        running = _run_store.list(limit=100, status="running")
        if len(running) == 1:
            run_id = running[0].run_id
    if run_id is None:
        return None
    return _store_speech_audio(run_id, audio)


def _record_audio_artifacts(run_id: str | None) -> None:
    """Annotate a finished record with the audio files its run produced.
    Reads the record fresh from the store — never a stale in-memory copy,
    which a concurrent finish() would clobber. Best effort: the files on
    disk stay authoritative."""
    if run_id is None or _run_store is None:
        return
    try:
        directory = _audio_dir(run_id)
        if not directory.exists():
            return
        files = sorted(str(p) for p in directory.iterdir() if p.is_file())
        if not files:
            return
        record = _run_store.get(run_id)
        if record is not None:
            _run_store.annotate(record, audio_artifacts=files)
    except Exception:
        logger.warning("Failed to annotate audio artifacts", exc_info=True)


def _make_speak_tool(backend: "VoiceBackend") -> Callable:
    async def speak(text: str, voice: str | None = None) -> str:
        """Speak text aloud through this agent's configured voice provider.

        Args:
            text: What to say.
            voice: Provider-defined voice id; the profile's default when omitted.
        """
        audio = await backend.speak(text, voice=voice)
        if audio is None:
            return "Spoken."
        saved = _store_speech_audio(_current_run_id.get(), audio)
        if saved is not None:
            return f"Audio synthesized and stored at {saved}."
        return "Audio synthesized (no run to store it against; discarded)."

    return speak


def _voice_extra_tools() -> list[Callable] | None:
    """The runtime tools a `voice:` profile grants — what build_agent
    receives as extra_tools (both the startup agent and per-run rebuilds)."""
    return [_make_speak_tool(_voice)] if _voice is not None else None


def _build_memory_lifecycle(profile: AgentProfile) -> "MemoryLifecycle | None":
    """The lifespan's memory construction, including the §18.7 honesty
    gate: an unsupported REQUIRED hook mode is an explicit boot failure,
    never a silent wrapper downgrade. Model-tier boundary injection IS the
    native seam (miragen owns every turn); executor-tier native hooks land
    with the hook-bridge PR, so a profile demanding them today must refuse
    to start."""
    if profile.memory is None or profile.memory.backend == "bridge":
        return None  # the bridge backend is BridgeMemory, not a local lifecycle
    if profile.memory.hooks.mode == "native_required" and profile.is_executor:
        from miragen.memory.harness_hooks import executor_hook_support

        support = executor_hook_support(profile.executor.executor)
        if not support.get("native_hooks"):
            raise ValueError(
                f"Agent '{profile.name}' sets memory.hooks.mode: "
                f"native_required, but executor '{profile.executor.executor}' "
                f"has no verified native hook integration "
                f"({support.get('detail', 'unsupported')}) — use "
                "hooks.mode: boundary_only, or run a hook-capable executor."
            )
    selector = None
    if profile.memory.recall.enabled:
        selector_model = (
            profile.memory.recall.model
            or profile.memory.extraction.model
            # A harness model (grok-build:…) is not a PydanticAI model the
            # selector could call: such profiles name memory.recall.model.
            or pydantic_ai_model(profile)
        )
        if selector_model:
            from miragen.memory.selection import build_model_selector

            selector = build_model_selector(selector_model)
    if profile.memory.backend == "ephemeral":
        from miragen.memory.ephemeral import provision_profile, shared_service

        service = shared_service()
        token = provision_profile(service, profile.name, profile.memory.scopes)
        client = MemoryClient(
            profile.memory,
            transport=service.transport(),
            base_url="http://ephemeral.local",
            token=token,
        )
        logger.warning(
            f"Agent '{profile.name}' uses backend: ephemeral — memory is "
            "IN-PROCESS and NON-DURABLE (dies with this process); dev/demo only"
        )
    else:
        client = MemoryClient(profile.memory)
    return MemoryLifecycle(
        profile.memory,
        profile.name,
        client,
        tools_available=not profile.is_executor or bool(profile.executor.mcp_servers),
        selector=selector,
    )


def _runtime_extra_tools() -> list[Callable] | None:
    """Every runtime-granted tool for build_agent: voice's speak plus the
    memory surface (§18.8) when the profile enables them."""
    tools: list[Callable] = list(_voice_extra_tools() or [])
    if _memory is not None:
        tools.extend(
            build_memory_tools(_memory, _current_run_id.get, _current_instance.get)
        )
    if _scheduling is not None:
        tools.extend(
            build_scheduling_tools(_scheduling, _current_run_id.get, _current_instance.get)
        )
    return tools or None


# ── Agent runner ──────────────────────────────────────────────────────────────────

def _append_history_sidecar(instance: str, run_id: str | None, message_count: int) -> None:
    """Correlate a history save with the run that produced it (best effort —
    a failed sidecar write logs a warning, never fails the run)."""
    try:
        line = json.dumps({
            "run_id": run_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "message_count": message_count,
        })
        sidecar = _history_sidecar(instance)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with open(sidecar, "a") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("Failed to append history sidecar entry")


async def run_agent(
    prompt: str,
    use_history: bool = False,
    record: RunRecord | None = None,
    repositories: list[RepositoryCheckout] | None = None,
    mcp_secret_env: dict[str, str] | None = None,
    instance: str | None = None,
) -> str:
    """
    Core agent execution. Called by cron/interval/startup and HTTP triggers.

    If `record` is given (from _run_store.start()), the run's outcome — success
    with usage/tool-calls, or failure with the error — is written back to it via
    _run_store.finish() before returning (or before the exception propagates).

    `instance` names the state scope a history-using run converses with
    (instances/v1); callers that set use_history without an instance get
    `default`. Admission (the per-instance mutex, the container cap) is the
    CALLER's job — this function assumes its slot is already held.

    Executor-tier profiles dispatch to the executor backend instead of the
    pydantic-ai agent; `use_history` does not apply there (the executor thread
    is the history). `repositories` (executor tier only) is the launch-time
    checkout plan carrying ephemeral bindings. `mcp_secret_env` (executor tier
    only) is this launch's ephemeral MCP bearer-token values, keyed by the
    declared secret's environment_variable name — never persisted.
    """
    if _executor is not None:
        if use_history:
            # Loud rejection, not a silent no-op: the executor thread is the
            # history, and a caller who set the flag believed otherwise.
            raise ValueError(
                "executor-tier runs do not support use_history — the executor "
                "thread is the conversation state; resume the run instead"
            )
        return await _run_executor_turn(
            prompt, record, repositories=repositories, mcp_secret_env=mcp_secret_env
        )

    assert _model_ready(), "Agent not initialized"

    # Same rule as use_history above, in the other direction. Workspace
    # checkout is executor-tier machinery that the model tier does not have
    # yet, so accepting a plan nothing acts on would produce a run that looks
    # successful while the agent never saw the code.
    if repositories:
        raise ValueError(
            "model-tier runs do not support repositories yet — miragen owns "
            "the loop and prepares no workspace checkout; use an "
            "executor-tier profile for runs that need a repository tree"
        )

    # Per-run MCP credentials, on the other hand, ARE carried: capabilities
    # are constructed in build_agent, so a launch supplying ephemeral token
    # values gets its own agent rather than the one built at startup with the
    # deployment-level environment baked in. The startup agent is reused
    # whenever a run supplies none, which is the common case.
    # Memory boundary (§17.3): restore working state + guidance and carry
    # it as per-run extra_instructions — transient by construction (it is
    # regenerated each run, never saved into history). Memory failures
    # degrade explicitly; they never fail the run.
    memory_packet = None
    if _memory is not None:
        memory_packet = await _memory.prepare_context(
            instance=instance,
            run_id=record.run_id if record is not None else None,
            trigger=record.trigger if record is not None else "direct",
            prompt_hint=prompt,
        )
    elif _bridge_memory is not None:
        memory_packet = SimpleNamespace(text=await _bridge_memory.prepare(
            instance=instance, run_id=record.run_id if record is not None else None,
            prompt=prompt))

    history_instance = instance or DEFAULT_INSTANCE
    turn = HarnessTurn(
        prompt=prompt,
        instance=history_instance,
        use_history=use_history,
        run_id=record.run_id if record is not None else None,
        secret_env=mcp_secret_env or None,
        extra_instructions=memory_packet.text if memory_packet is not None else None,
    )
    harness = _model_harness()

    run_ctx = (
        _telemetry.run_span(
            "agent run",
            run_id=record.run_id if record is not None else uuid.uuid4().hex,
            trigger=record.trigger if record is not None else None,
            tier="model",
        )
        if _telemetry is not None
        else nullcontext()
    )
    run_id_token = _current_run_id.set(record.run_id if record is not None else None)
    instance_token = _current_instance.set(instance)
    try:
        with run_ctx as run_span:
            result = await harness.run(turn)
            if run_span is not None and result.usage is not None:
                if result.usage.input_tokens:
                    run_span.set_attribute("gen_ai.usage.input_tokens", result.usage.input_tokens)
                if result.usage.output_tokens:
                    run_span.set_attribute("gen_ai.usage.output_tokens", result.usage.output_tokens)
    except Exception as e:
        if record is not None and _run_store is not None:
            _run_store.finish(record, status="failed", error=str(e))
            _write_model_run_events(record.run_id, error=str(e))
            _record_audio_artifacts(record.run_id)
            if _memory is not None:
                await _memory.finish_turn(
                    instance=instance, run_id=record.run_id, trigger=record.trigger,
                    status="failed", error=str(e),
                )
        if _bridge_memory is not None:
            await _bridge_memory.finish(instance=instance, run_id=record.run_id if record else None,
                                        output=None, status="failed")
        raise
    finally:
        _current_run_id.reset(run_id_token)
        _current_instance.reset(instance_token)

    output = result.output

    if record is not None and _run_store is not None:
        usage, tool_calls = result.usage, result.tool_calls
        _run_store.finish(
            record,
            status="succeeded",
            output=output,
            usage=usage,
            tool_calls=tool_calls,
            # Aggregates derived from the same trace, so both tiers report
            # them — a consumer of tool_call_count no longer needs to know
            # which backend ran the agent.
            tool_call_count=len(tool_calls),
            tool_call_failures=sum(1 for c in tool_calls if not c.ok),
        )
        _write_model_run_events(record.run_id, tool_calls=tool_calls, usage=usage, output=output)
        _record_audio_artifacts(record.run_id)
        if _memory is not None:
            await _memory.finish_turn(
                instance=instance, run_id=record.run_id, trigger=record.trigger,
                status="succeeded", summary=output,
            )
    if _bridge_memory is not None:
        await _bridge_memory.finish(instance=instance, run_id=record.run_id if record else None,
                                    output=output, status="succeeded")

    return output


def _write_model_run_events(run_id: str, **kw) -> None:
    """Model-tier half of the unified event log: one post-hoc turn write into
    the same stream the executor tier appends live. Best effort — the run
    record stays authoritative; a failed event write never fails the run."""
    if _run_store is None:
        return
    try:
        run_events.write_model_turn_events(
            run_events.events_path(_run_store.root, run_id), **kw
        )
    except Exception:
        logger.warning("Failed to write model-tier run events", exc_info=True)


async def _run_executor_turn(
    prompt: str,
    record: RunRecord | None,
    *,
    resume: bool = False,
    repositories: list[RepositoryCheckout] | None = None,
    mcp_secret_env: dict[str, str] | None = None,
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

    # Memory boundary (§17.3), executor tier: boundary injection — the
    # packet rides the turn prompt (the executor thread persists it; that
    # is the capability honestly reported as boundary_injection, not
    # transient context replacement).
    memory_packet = None
    if _memory is not None:
        memory_packet = await _memory.prepare_context(
            instance=record.instance if record is not None else None,
            run_id=run_id,
            trigger=record.trigger if record is not None else "direct",
            prompt_hint=prompt,
        )
        prompt = f"{memory_packet.text}\n\n{prompt}"

    # Cursor + wall clock captured before the turn so its slice of the event
    # stream (and its true span interval) can be exported afterwards.
    turn_started = datetime.now(timezone.utc)
    events_before = _executor.last_seq(run_id) if _telemetry is not None else 0

    turn = _executor.run_job(
        prompt,
        run_id,
        thread_id=record.thread_id if record is not None else None,
        workspace=record.workspace if record is not None else None,
        first_turn=not resume,
        prior_usage=record.usage if record is not None else None,
        repositories=repositories,
        mcp_secret_env=mcp_secret_env,
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
            affected_repositories=result.affected_repositories,
            change_categories=result.change_categories,
            setup_s=_accumulate(record.setup_s, result.setup_s),
            tool_call_count=_accumulate(record.tool_call_count, result.tool_call_count),
            tool_call_failures=_accumulate(record.tool_call_failures, result.tool_call_failures),
            pending_intervention=InterventionRequest.model_validate(result.intervention)
            if result.intervention is not None else None,
        )
        if prepared_revisions:
            _record_snapshot_commits(record.run_id, prepared_revisions)
        # Speak calls made through /mcp/voice during this turn land in the
        # run's audio/ directory; fold them onto the finished record.
        _record_audio_artifacts(record.run_id)
        if _memory is not None:
            await _memory.finish_turn(
                instance=record.instance, run_id=record.run_id,
                trigger=record.trigger, status=result.status,
                summary=result.output, error=result.error,
                turn=record.resume_count,
            )

    if _telemetry is not None:
        # Post-hoc, from the turn's slice of the durable event stream — the
        # self-harnessed loop can't be instrumented from here, but its events
        # can be translated span-for-span with their own timestamps.
        # Paginated on the cursor watermark (§18.2): no silent 10k
        # truncation. The page cap is a guardrail against a pathological
        # stream; hitting it reports exported vs remaining explicitly.
        turn_events: list = []
        cursor = events_before
        for _page_index in range(20):  # ≤200k events per turn export
            page = _executor.read_events_page(run_id, after=cursor, limit=10_000)
            turn_events.extend(page.events)
            cursor = page.next_after
            if not page.has_more:
                break
        else:
            logger.warning(
                f"[{_profile.name if _profile else '?'}] run {run_id}: turn "
                f"exceeded the 200k-event export guardrail; exported "
                f"{len(turn_events)} events, remainder stays in the durable "
                "stream (not silently dropped — re-exportable by cursor)"
            )
        _telemetry.emit_executor_turn(
            turn_events,
            run_id=run_id,
            trigger=record.trigger if record is not None else None,
            executor=_executor.spec.executor,
            status=result.status,
            exit_reason=result.exit_reason,
            usage=result.usage,
            started_at=turn_started,
            finished_at=datetime.now(timezone.utc),
            resume_count=record.resume_count if record is not None else 0,
        )

    # Reviewed publication only: do NOT auto-push to the publication backend
    # on success. Orchestrators call POST /runs/{id}/publications after human
    # review (capability reviewed-publication/v1). The harvested diff on disk
    # remains the authoritative local artifact until then.

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


def _daily_budget_status() -> tuple[int, int] | None:
    """(used, limit) today (UTC) if profile.limits.tokens_per_day is configured, else None.

    `used` folds in a reservation for runs still `running`, not just usage
    already recorded by `finish()` — otherwise concurrent run requests issued
    before any of them complete each see the same total and all pass. See
    reserved_tokens_in_flight and miragen#58."""
    if _profile is None or _profile.limits is None or _profile.limits.tokens_per_day is None:
        return None
    if _run_store is None:
        return None
    midnight_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    limit = _profile.limits.tokens_per_day
    used = tokens_used_since(_run_store, midnight_utc)
    still_remaining = limit - used
    used += reserved_tokens_in_flight(_run_store, midnight_utc, _profile.limits.tokens_per_run, still_remaining)
    return used, limit


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
    instance: str | None = None,
) -> None:
    """Scheduler-triggered run (cron, interval, startup, or a managed
    binding). Handles on_complete side effects — for managed fires too, by
    decision: uniform with profile fires. The daily budget applies to every
    trigger source; `stamp=False` (managed fires) keeps completed prompts
    verbatim.

    `instance` (a trigger's / binding's opt-in) makes the fire a turn of that
    named instance — serialized on it, and conversing with its history on the
    model tier. Admission overflow skips the fire with a warning, exactly like
    a budget-exceeded skip: the next fire will try again."""
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

    try:
        release = _acquire_run_slot(instance)
    except RunBusy as e:
        logger.warning(f"[{_profile.name}] scheduled run skipped: {e}")
        return

    if stamp and _profile.inject_timestamp:
        prompt = _stamp_prompt(prompt)

    # A named instance's fires are a conversation on the model tier; the
    # executor tier has no history mechanism (the thread is the state), so
    # there the name only scopes serialization and run records.
    use_history = instance is not None and _executor is None

    record = (
        _run_store.start(
            agent_name=_profile.name,
            trigger=trigger,
            prompt=prompt,
            use_history=use_history,
            instance=instance,
            provenance=provenance,
        )
        if _run_store is not None
        else None
    )

    try:
        output = await run_agent(prompt, use_history=use_history, record=record, instance=instance)
        logger.info(f"[{_profile.name}] scheduled run complete")
    except Exception as e:
        logger.error(f"[{_profile.name}] scheduled run failed: {e}", exc_info=True)
        return
    finally:
        release()

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
        await _handle_on_complete(output, run_id=record.run_id if record else None)
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
    if spec.at is not None:
        # One-shot: a past `at` fires promptly rather than being silently
        # dropped by a misfire window.
        run_date = spec.at if spec.at.tzinfo else spec.at.replace(tzinfo=timezone.utc)
        floor = datetime.now(timezone.utc) + timedelta(seconds=2)
        return DateTrigger(run_date=max(run_date, floor))
    return APIntervalTrigger(seconds=spec.every_s)


async def _run_managed_schedule(name: str) -> None:
    """One fire of a managed binding. Reads the binding fresh so a
    just-disabled or just-deleted binding never fires stale."""
    if _schedule_store is None:
        return
    binding = _schedule_store.get(name)
    if binding is None or not binding.fires_here:
        # Includes a binding handed to the control plane since this job was
        # scheduled: firing it here too would launch the same fire twice.
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
        instance=binding.instance,
    )
    if binding.schedule.one_shot:
        # Fired once: the binding removes itself, mirroring the tool
        # contract ("fires once, then deletes"). Best-effort — a crash
        # between fire and delete re-fires promptly on restart, which the
        # run-side idempotency (instance serialization, memory keys)
        # tolerates better than a silently-lost wakeup.
        try:
            _schedule_store.delete(name)
        except KeyError:
            pass
        _drop_managed_job(name)


def _reconcile_managed_job(binding: ScheduleBinding) -> None:
    """Make the scheduler match one binding: enabled → (re)registered job,
    disabled or externally fired → no job. Raises on scheduler failure
    (callers roll back)."""
    if binding.fires_here:
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
    if _profile is not None and _profile.mode == "interactive":
        # The live PUT /schedules path rejects interactive agents, but a stale
        # schedules volume could still carry bindings from a prior deployment.
        # Honor the mode contract on startup too: an interactive agent must not
        # self-activate. Bindings are left on disk (not deleted), just not run.
        pending = [b.name for b in _schedule_store.list() if b.fires_here]
        if pending:
            logger.warning(
                f"[{_profile.name}] mode is interactive — not registering "
                f"{len(pending)} managed schedule binding(s): {pending}. "
                "Redeploy as hybrid to run them."
            )
        return 0
    count = 0
    for binding in _schedule_store.list():
        try:
            _reconcile_managed_job(binding)
            count += binding.fires_here
        except Exception as e:
            logger.error(f"failed to register managed schedule '{binding.name}': {e}", exc_info=True)
    return count


def _next_fire_at(name: str) -> str | None:
    job = _scheduler.get_job(_managed_job_id(name))
    next_run = getattr(job, "next_run_time", None) if job is not None else None
    return next_run.isoformat() if next_run else None


def _assert_on_complete_handlers_registered(profile) -> None:
    """Refuse to start when on_complete names a handler nobody registered.

    Dispatch below is `if oc.notify and oc.notify in handlers` — a name that
    was never registered is skipped in silence. miragen ships no handlers of
    its own, so `notify: telegram` in a profile whose tools.py never calls
    `@register_handler("telegram")` produces no notification, no warning and no
    error, while the run itself succeeds. "Delivered" and "silently dropped"
    then look identical from every angle an operator can see, and an autonomous
    agent's entire output path can be dead for weeks.

    A named handler that does not exist is a configuration error, so it fails
    here the way `_inject_tools` already fails on unknown tool names.

    This lives in the lifespan rather than in `load_profile` on purpose: the
    daemon's `validate_profile_text` validates YAML without ever importing an
    agent's tools.py, so a check there would reject every profile carrying an
    on_complete block. By the time this runs, `_import_tools` has executed
    (cli.run imports tools before starting uvicorn) and the registry is
    populated.
    """
    if not profile.on_complete:
        return

    handlers = registered_handlers()
    missing = [
        (field, name)
        for field, name in (
            ("log_to", profile.on_complete.log_to),
            ("notify", profile.on_complete.notify),
        )
        if name and name not in handlers
    ]
    if missing:
        described = ", ".join(f"{field}: '{name}'" for field, name in missing)
        raise ValueError(
            f"Agent '{profile.name}' on_complete references unregistered "
            f"handler(s) — {described}. Registered: {sorted(handlers) or 'none'}. "
            "Register it in the agent's tools.py with @register_handler, or "
            "remove it from the profile; leaving it configured would silently "
            "deliver nothing."
        )


async def _handle_on_complete(output: str, run_id: str | None = None) -> None:
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

    if oc.speak and _voice is not None:
        audio = await _voice.speak(output)
        if audio is not None:
            saved = _store_speech_audio(run_id, audio)
            _record_audio_artifacts(run_id)
            logger.info(
                f"[{_profile.name}] output synthesized to "
                f"{saved or 'nowhere (no run to store against)'}"
            )
        else:
            logger.info(f"[{_profile.name}] output spoken")


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
    global _profile, _agent, _limits, _harness, _gateway, _bridge_memory, _speak_guidance, _run_store, _executor, _schedule_store, \
        _publication_store, _telemetry, _voice, _memory, _scheduling

    _load_file_secrets()

    profile_path = os.environ.get("AGENT_PROFILE", "agent.yaml")
    logger.info(f"Loading agent profile: {profile_path}")

    _profile = load_profile(profile_path)

    _telemetry = telemetry_from_env(_profile)
    if _telemetry is not None:
        logger.info("OTLP telemetry enabled")

    _migrate_legacy_history()

    _run_store = RunStore(retention=run_retention_from_env())
    _publication_store = PublicationStore(_run_store.root / "publications")
    interrupted = _run_store.sweep_interrupted()
    if interrupted:
        logger.warning(f"Marked {interrupted} stale 'running' record(s) as interrupted")

    # The schedule store exists before the agent is built: the scheduling
    # tools close over it (and managed-schedule registration reuses it
    # further down, unchanged).
    _schedule_store = ScheduleStore()
    if _profile.mode != "interactive" and _profile.runtime_tools.schedule:
        _scheduling = SchedulingBackend(
            _schedule_store, _reconcile_managed_job, _drop_managed_job, _next_fire_at
        )
        logger.info("Runtime tools enabled: scheduling")
    else:
        _scheduling = None

    # Built before the agent: the memory tools close over the lifecycle.
    _memory = _build_memory_lifecycle(_profile)
    if _profile.memory is not None and _profile.memory.backend == "bridge":
        from miragen.memory.bridge import BridgeMemory

        _bridge_memory = BridgeMemory.from_env(_profile.memory, _profile.name, dict(os.environ))
        logger.info(f"Memory through the session plane at {_bridge_memory.base_url} "
                    f"(project {_profile.memory.project})")
    if _memory is not None:
        logger.info(f"Memory enabled (backend: {_profile.memory.backend})")

    # Built before the agent: the model tier's speak tool closes over it.
    _voice = (
        build_voice_backend(_profile.voice, _profile.name)
        if _profile.voice is not None
        else None
    )
    _speak_guidance = (
        load_speak_guidance(_profile.voice, profile_path) if _profile.voice is not None else None
    )
    if _voice is not None:
        logger.info(f"Voice enabled (provider: {_profile.voice.provider})")

    if _profile.is_executor:
        _executor = build_executor(_profile, runs_root=_run_store.root)
        # Before prepare(): config installation (e.g. Codex hooks.json)
        # must see whether memory is wired (§18.7).
        _executor.set_memory(_memory)
        _executor.prepare()
        logger.info(f"Agent '{_profile.name}' built in {_profile.mode} mode (executor tier: {_profile.executor.executor})")
    elif profile_harness(_profile) != PYDANTIC_AI:
        _harness, _gateway = build_model_harness(
            _profile,
            runs_root=_run_store.root,
            runtime_tools=_runtime_extra_tools(),
            registered_tools=registered_tools(),
            bind_context=_bind_run_context,
            system_guidance=_speak_guidance,
            session_key_for=_bridge_memory.session_key if _bridge_memory is not None else None,
        )
        _gateway_mount.inner = _gateway.asgi
        if _bridge_memory is not None and hasattr(_harness, "session_info"):
            # Rotated sessions are separate plane sessions; compactions and
            # rotations reach the plane as compacting / closed.
            harness = _harness
            _bridge_memory.session_seq = lambda name: (harness.session_info(name) or {}).get("seq", 1)
            harness.on_lifecycle = _bridge_memory.lifecycle
        logger.info(
            f"Agent '{_profile.name}' built in {_profile.mode} mode (harness: {_harness.name})"
        )
    else:
        _agent, _limits = build_agent(
            _profile, telemetry=_telemetry, extra_tools=_runtime_extra_tools(),
            **_guidance_kwargs(),
        )
        logger.info(f"Agent '{_profile.name}' built in {_profile.mode} mode")

    _assert_on_complete_handlers_registered(_profile)

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
                kwargs={"instance": trigger.instance},
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
                kwargs={"instance": trigger.instance},
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
                kwargs={"instance": trigger.instance},
                id=f"{_profile.name}:startup:{startup_i}",
                replace_existing=True,
            )
            logger.info(f"Registered startup trigger: delay {trigger.delay_s}s")
            startup_i += 1

    managed = _register_managed_schedules()
    if managed:
        logger.info(f"Registered {managed} managed schedule binding(s)")

    # The mounted ask_human MCP sub-app doesn't get its own lifespan from
    # FastAPI; its session manager must be running for the endpoint to serve —
    # and it's once-per-instance, so each lifespan builds a fresh server.
    # Entered BEFORE the scheduler starts: its startup awaits must not open a
    # window in which an immediate startup trigger fires ahead of `yield`.
    ask_human_mcp = build_ask_human_mcp(lambda: (_run_store, _executor))
    _ask_human_guard.inner = ask_human_mcp.streamable_http_app()
    voice_mcp = build_voice_mcp(lambda: (_voice, _resolve_and_store_audio))
    _voice_mcp_guard.inner = voice_mcp.streamable_http_app()
    memory_mcp = build_memory_mcp(lambda: (_memory, _run_store))
    _memory_mcp_guard.inner = memory_mcp.streamable_http_app()
    schedule_mcp = build_scheduling_mcp(lambda: (_scheduling, _run_store))
    _schedule_mcp_guard.inner = schedule_mcp.streamable_http_app()
    try:
        async with (
            ask_human_mcp.session_manager.run(),
            voice_mcp.session_manager.run(),
            memory_mcp.session_manager.run(),
            schedule_mcp.session_manager.run(),
            _gateway.running() if _gateway is not None else nullcontext(),
        ):
            _scheduler.start()
            logger.info("Scheduler started")
            yield
    finally:
        _ask_human_guard.inner = _mcp_not_ready
        _voice_mcp_guard.inner = _mcp_not_ready
        _memory_mcp_guard.inner = _mcp_not_ready
        _schedule_mcp_guard.inner = _mcp_not_ready

    _scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
    if _harness is not None:
        # Long-lived harnesses hold processes (e.g. one grok agent per
        # instance); stop them with the container.
        await _harness.aclose()
        _harness = None
    _gateway_mount.inner = _mcp_not_ready
    _gateway = None

    if _telemetry is not None:
        _telemetry.shutdown()
        _telemetry = None
        logger.info("Telemetry flushed")


# ── App ────────────────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)


# ── ask_human MCP mount ──────────────────────────────────────────────────────
#
# The MCP-tool variant of structured interventions (design doc §5): writes the
# same workspace sentinel the file mechanism uses. Executor profiles opt in by
# pointing an executor.mcp_servers entry at /mcp/ask-human on this app.


class _TokenGuardASGI:
    """Route-level `_internal_auth` doesn't reach a mounted sub-app, so the
    same MIRAGEN_INTERNAL_TOKEN contract is enforced here: unset means no
    enforcement (single-container zero-config), set means the header (or a
    bearer token, which is how ExecutorMCPServer.bearer_token_env arrives)
    must match."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        expected = os.environ.get("MIRAGEN_INTERNAL_TOKEN")
        if scope["type"] == "http" and expected:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers") or []
            }
            supplied = headers.get("x-miragen-token") or ""
            auth = headers.get("authorization", "")
            if not supplied and auth.startswith("Bearer "):
                supplied = auth[len("Bearer "):].strip()
            if not hmac.compare_digest(supplied, expected):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b'{"error": "unauthorized"}',
                })
                return
        await self.inner(scope, receive, send)


async def _mcp_not_ready(scope, receive, send):
    if scope["type"] != "http":  # pragma: no cover - lifespan probes
        return
    await send({
        "type": "http.response.start",
        "status": 503,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"error": "ask_human MCP not started (app lifespan not running)"}',
    })


# The guard's inner app is swapped in by the lifespan: the MCP session manager
# is once-per-instance, so each lifespan run (restarts, tests) builds a fresh
# FastMCP rather than re-running a module-level one.
_ask_human_guard = _TokenGuardASGI(_mcp_not_ready)
app.mount("/mcp/ask-human", _ask_human_guard)

# Voice front door (docs/design/voice.md) — same guard/lifespan pattern.
_voice_mcp_guard = _TokenGuardASGI(_mcp_not_ready)
app.mount("/mcp/voice", _voice_mcp_guard)

# Memory tools for the executor tier (§18.8) — same guard/lifespan pattern.
_memory_mcp_guard = _TokenGuardASGI(_mcp_not_ready)
app.mount("/mcp/memory", _memory_mcp_guard)

# Runtime tool library: scheduling, executor tier — same pattern.
_schedule_mcp_guard = _TokenGuardASGI(_mcp_not_ready)
app.mount("/mcp/schedule", _schedule_mcp_guard)


class _SwappableASGI:
    """A mount whose app is set by the lifespan. The tool gateway does its
    own auth (per-instance credentials), so no internal-token guard here."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        await self.inner(scope, receive, send)


_gateway_mount = _SwappableASGI(_mcp_not_ready)
app.mount("/mcp/gateway", _gateway_mount)


# ── HTTP trigger schemas ────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    prompt: str
    use_history: bool = False
    instance: Optional[str] = Field(
        default=None,
        pattern=INSTANCE_NAME_PATTERN,
        description=(
            "Named instance this run belongs to (instances/v1). Omitted: "
            "history-using runs converse with 'default'; stateless runs stay "
            "ephemeral (unserialized), exactly as before instances existed."
        ),
    )

    def effective_instance(self) -> Optional[str]:
        return self.instance or (DEFAULT_INSTANCE if self.use_history else None)


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
#
# reviewed-publication/v1 means the HTTP endpoint exists in this build.
# A successful publish still requires executor.artifact_sink on the profile
# (otherwise POST returns 400). See health["publication"] for config readiness.
CONTRACT_CAPABILITIES = [
    "edf-resolve/mirarun.io-v1alpha1",   # POST /profiles/resolve
    "executor-launch/v1",                # POST /executor-runs (idempotency + provenance)
    "run-snapshot/v1",                   # GET /runs/{id}/snapshot
    "events-cursor/v1",                  # GET /runs/{id}/events?after=
    "run-events-unified/v1",             # /runs/{id}/events serves BOTH tiers
    "managed-schedules/v1",              # GET/PUT/DELETE /schedules (CAS reconciliation)
    "managed-schedules-external-fire/v1",  # bindings with externally_fired: recorded, never fired here
    "interventions/v1",                  # structured question suspension + answered resume
    "ask-human-mcp/v1",                  # /mcp/ask-human MCP tool (writes the sentinel)
    "reviewed-publication/v1",           # POST /runs/{id}/publications (endpoint; backend config required)
    "model-tier-launch/v1",              # /executor-runs accepts model-tier EDFs (spec.executor.kind: model)
    "instances/v1",                      # named instances: per-instance history + admission control
    "voice/v1",                          # speak tool + /mcp/voice mount + on_complete.speak
    "memory/v1",                         # memory lifecycle: boundary injection + tools + /mcp/memory
    "runtime-tools/v1",                  # default tool library: scheduling (+ /mcp/schedule)
]


def _installed_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("miragen")
    except Exception:
        return None


def _publication_health() -> dict:
    """Honest readiness for reviewed publication (capability ≠ configured)."""
    configured = bool(
        _executor is not None
        and _executor.spec is not None
        and _executor.spec.artifact_sink is not None
    )
    return {
        # Endpoint is always present when this capability string is advertised.
        "endpoint_supported": True,
        # Backend profile config required for a non-4xx publish.
        "backend_configured": configured,
        "backend_kind": (
            _executor.spec.artifact_sink.kind
            if configured and _executor is not None and _executor.spec.artifact_sink is not None
            else None
        ),
    }


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
        # Profile contract levels this runtime executes (#75) — the same
        # declaration the image label and `miragen contract` carry.
        "profile_contracts": list(SUPPORTED_PROFILE_CONTRACTS),
        "publication": _publication_health(),
        # Honest readiness, same pattern as publication: configured ≠ healthy,
        # but a False here explains an empty backend before anyone debugs it.
        "telemetry": {
            "otlp_configured": _telemetry is not None,
            # §18.2: export failure/drop visibility — "configured" never
            # implied "delivered", and now the gap is measurable.
            "export": {
                "attempted_batches": _telemetry.export_stats.attempted_batches,
                "failed_batches": _telemetry.export_stats.failed_batches,
                "failed_spans": _telemetry.export_stats.failed_spans,
            } if _telemetry is not None else None,
        },
        # Capability ≠ configured, same pattern as publication: voice/v1 is
        # always advertised; this says whether THIS profile can actually speak.
        "voice": {
            "configured": _voice is not None,
            "provider": _profile.voice.provider
            if _profile is not None and _profile.voice is not None
            else None,
        },
        # Memory lifecycle readiness (capability != configured, as with
        # voice) plus the honest degradation counters (§18.8): a nonzero
        # degraded_count explains missing memories before anyone debugs.
        "memory": {
            "configured": _memory is not None,
            "backend": _profile.memory.backend
            if _profile is not None and _profile.memory is not None
            else None,
            "boundary_injection": _memory is not None,
            # Model tier: miragen owns every turn, so the boundary IS the
            # native seam. Executor tier: the adapter's honest report.
            "native_hooks": (
                None if _memory is None
                else _executor.memory_hook_capabilities() if _executor is not None
                else {"native_hooks": True, "mechanism": "model_tier_boundary"}
            ),
            "degraded_count": _memory.degraded_count if _memory else 0,
            "last_degraded": _memory.last_degraded if _memory else None,
        },
        # Live admission state (instances/v1): what's running now against the
        # effective cap, so "why am I getting 429" is answerable from /health.
        "concurrency": {
            "active_runs": _active_runs,
            "max_concurrent_runs": _max_concurrent_runs(),
            "busy_instances": sorted(_busy_instances),
        },
    }


def _reject_executor_use_history(request: "RunRequest") -> None:
    """use_history on an executor-backed agent was silently ignored once —
    a caller who sets it believes a history mechanism exists. 400, loudly."""
    if _executor is not None and request.use_history:
        raise HTTPException(
            status_code=400,
            detail=(
                "use_history does not apply to executor-backed agents — the "
                "executor thread is the conversation state; resume the run "
                "(POST /runs/{run_id}/resume) to continue it"
            ),
        )


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
    if not _model_ready() and _executor is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    _reject_executor_use_history(request)
    _raise_if_daily_budget_exceeded()

    instance = request.effective_instance()
    release = _admit_or_429(instance)
    try:
        prompt = _apply_trigger_prompt(request.prompt)

        record = (
            _run_store.start(
                agent_name=_profile.name,
                trigger="http",
                prompt=prompt,
                use_history=request.use_history,
                instance=instance,
            )
            if _run_store is not None and _profile is not None
            else None
        )

        try:
            output = await run_agent(
                prompt, use_history=request.use_history, record=record, instance=instance
            )
            return RunResponse(output=output, run_id=record.run_id if record else None)
        except Exception as e:
            logger.error(f"[{_profile.name if _profile else '?'}] run failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
    finally:
        release()


@app.post("/run/async", status_code=202, dependencies=[_internal_auth])
async def run_async(request: RunRequest):
    """
    Non-blocking variant of /run: starts the run in the background and returns
    immediately with a run_id. Poll GET /runs/{run_id} for the outcome.
    """
    if not _model_ready() and _executor is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    if _run_store is None or _profile is None:
        raise HTTPException(status_code=503, detail="Run store not ready")
    _reject_executor_use_history(request)
    _raise_if_daily_budget_exceeded()

    # The slot is claimed HERE, not in the background task: 429 must be the
    # synchronous answer to an over-capacity request, never a task that was
    # accepted with a run_id and then silently refused.
    instance = request.effective_instance()
    release = _admit_or_429(instance)

    try:
        prompt = _apply_trigger_prompt(request.prompt)
        record = _run_store.start(
            agent_name=_profile.name,
            trigger="http_async",
            prompt=prompt,
            use_history=request.use_history,
            instance=instance,
        )
    except Exception:
        release()
        raise

    async def _background_run() -> None:
        try:
            await run_agent(
                prompt, use_history=request.use_history, record=record, instance=instance
            )
        except Exception as e:
            # run_agent already wrote the failure to the record; this is just
            # so an async-run exception never propagates into a bare task error.
            logger.error(f"[{_profile.name}] async run failed: {e}", exc_info=True)
        finally:
            release()

    _spawn_background(_background_run())

    return {"run_id": record.run_id, "status": "running"}


@app.get("/runs", response_model=RunListResponse, dependencies=[_internal_auth])
async def list_runs(
    limit: int = 20,
    status: Optional[str] = None,
    trigger: Optional[str] = None,
):
    if _run_store is None:
        raise HTTPException(status_code=503, detail="Run store not ready")
    runs = _run_store.list(
        limit=min(limit, 100),
        status=status,
        trigger=trigger,
    )
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

    # A resumed turn is a turn: it takes a slot, and serializes on the run's
    # instance when it has one. Claimed before reopen() so an over-capacity
    # resume answers 429 without ever flipping the record back to running.
    release = _admit_or_429(record.instance)
    try:
        record = _run_store.reopen(record)
        try:
            await _run_executor_turn(prompt, record, resume=True)
        except RuntimeError:
            pass  # outcome (failed:crash) is already on the record; return it
        return _run_store.get(record.run_id)
    finally:
        release()


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


@app.post(
    "/runs/{run_id}/publications",
    response_model=PublicationResponse,
    dependencies=[_internal_auth],
)
async def publish_run(run_id: str, request: PublicationRequest):
    """Reviewed whole-run publication (capability reviewed-publication/v1).

    After human review, an orchestrator asks miragen to graduate one
    **succeeded** executor run into the configured publication backend
    (profile ``executor.artifact_sink``). miragen reads its authoritative
    run/diff, performs the backend writes, and returns **opaque** references
    only. Orchestrators must not write artifacts/provenance/history to the
    backend themselves.

    Idempotent on ``idempotency_key``. Backend/network failures are retryable
    (503) and never change the execution run's status. Validation failures are
    deterministic 4xx.
    """
    if _run_store is None:
        raise HTTPException(status_code=503, detail="Run store not ready")
    if _executor is None or _executor.spec.artifact_sink is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "publication backend not configured — set executor.artifact_sink "
                "on the agent profile (kind + url); publication is never auto-fired"
            ),
        )
    pub_store = _publication_store
    if pub_store is None:
        # Tests / partial wiring: create beside the run store.
        pub_store = PublicationStore(_run_store.root / "publications")

    try:
        record = _get_executor_record(run_id)
    except HTTPException:
        raise
    except AmbiguousRunIdError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "ambiguous run_id prefix", "candidates": e.candidates},
        )

    if _publication_backend_override is not None:
        backend = _publication_backend_override
    else:
        sink_spec = _executor.spec.artifact_sink
        token = (
            os.environ.get(sink_spec.bearer_token_env)
            if sink_spec.bearer_token_env
            else None
        )
        try:
            backend = build_publication_backend(sink_spec, bearer_token=token)
        except PublicationConfigError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    def _annotate(run: RunRecord, **kw):
        assert _run_store is not None
        return _run_store.annotate(run, **kw)

    try:
        return await publish_reviewed_run(
            run=record,
            request=request,
            store=pub_store,
            backend=backend,
            annotate_run=_annotate,
        )
    except PublicationConfigError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=str(e)) from e
    except PublicationUnavailableError as e:
        raise HTTPException(
            status_code=503,
            detail={"error": str(e), "retryable": True},
        ) from e
    except PublicationError as e:
        status = e.status_code or (503 if e.retryable else 502)
        raise HTTPException(
            status_code=status,
            detail={"error": str(e), "retryable": e.retryable},
        ) from e


@app.get("/runs/{run_id}/events", dependencies=[_internal_auth])
async def get_run_events(run_id: str, limit: int = 200, after: Optional[int] = None):
    """The run's durable event stream — BOTH tiers (capability
    run-events-unified/v1). Executor runs stream turn/item/lifecycle events
    live; model-tier runs write their turn's events (tool calls, usage,
    outcome) after the fact from the same run details that fill the record.
    One envelope, one sequence, one cursor contract — a projector never
    branches on tier.

    Two read modes over the same durable sequenced stream:
    - without `after`: tail read (newest `limit` events), original contract;
    - with `after`: cursor replay — events with seq > after, oldest first,
      plus `next_after`/`has_more` for paging. (run_id, seq) is the
      deduplication key; replaying any cursor is idempotent, so a projector
      can rebuild its projection from after=0 at any time.
    """
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

    # The executor knows its own runs_root (they can differ in tests/embeds);
    # model-tier streams live beside the run store.
    path = (
        _executor._events_path(record.run_id)
        if _executor is not None
        else run_events.events_path(_run_store.root, record.run_id)
    )
    limit = min(limit, 1000)
    if after is not None:
        page = run_events.read_events_page(path, after=after, limit=limit)
        return {
            "run_id": record.run_id,
            "count": len(page.events),
            "events": page.events,
            "next_after": page.next_after,
            "has_more": page.has_more,
        }
    events = run_events.read_events(path, limit=limit)
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
    # Base-tier launches only: the named instance the run converses with,
    # and whether it continues that instance's conversation (instances/v1).
    # A durable, idempotent launch *into a conversation* — what a control
    # plane driving a persistent agent needs (Mira).
    instance: Optional[str] = Field(default=None, pattern=INSTANCE_NAME_PATTERN)
    use_history: bool = False
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
    # resolved_profile is AgentProfile.model_dump(), which carries BOTH tier
    # keys with the unused one set to None — so presence of the key proves
    # nothing and the value is what distinguishes the tiers.
    wants_model_tier = resolved.resolved_profile.get("spec") is not None
    if wants_model_tier or _profile.executor is None:
        # Model tier on at least one side. Both must be model tier, and the
        # model must match for the same reason it must on the executor tier:
        # a launch executes with the CONFIGURED agent, so an EDF resolving to
        # a different one belongs to a different deployment.
        if not wants_model_tier:
            issues.append(
                "configured agent is model-tier but the EDF resolves to executor "
                f"'{resolved.resolved_profile['executor']['executor']}'"
            )
        elif _profile.spec is None:
            issues.append(
                "EDF resolves to the model tier but this agent is executor-tier "
                f"('{_profile.executor.executor}')"
            )
        else:
            want_model = resolved.resolved_profile["spec"]["model"]
            if want_model != _profile.spec.model:
                issues.append(
                    f"EDF resolves to model '{want_model}' but this agent is "
                    f"configured for '{_profile.spec.model}'"
                )
        return {"compatible": not issues, "issues": issues}
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


class TurnRequest(BaseModel):
    """One turn in a conversation instance (base tier)."""

    prompt: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    provenance: RunProvenance | None = None


@app.post("/instances/{name}/turns", status_code=202, dependencies=[_internal_auth])
async def start_turn(name: str, request: TurnRequest, response: Response):
    """Start a turn in a conversation instance, with its history.

    The base tier's name for what /executor-runs does for it: the same
    durable, idempotent acceptance (a retried idempotency_key returns the
    original turn with 200 and duplicate: true), the same admission, and the
    turn's record is readable at GET /instances/{name}/turns/{turn_id} (or
    /runs/{turn_id}). A turn is asynchronous: it can take minutes (tools,
    approvals), so this answers at once with the turn's id."""
    _check_instance_name(name)
    if _executor is not None:
        raise HTTPException(status_code=409, detail="an executor-tier agent has no "
                            "conversation instances; use /executor-runs")
    result = await launch_executor_run(
        ExecutorLaunchRequest(prompt=request.prompt, idempotency_key=request.idempotency_key,
                              provenance=request.provenance, instance=name, use_history=True),
        response)
    return {"turn_id": result["run_id"], "instance": name,
            **{k: v for k, v in result.items() if k in ("status", "duplicate")}}


@app.get("/instances/{name}/turns/{turn_id}", response_model=RunRecord, dependencies=[_internal_auth])
async def get_turn(name: str, turn_id: str):
    """A turn's record (status, output, tool calls, usage), if it belongs to
    this instance."""
    _check_instance_name(name)
    record = await get_run(turn_id)
    instance = record.get("instance") if isinstance(record, dict) else getattr(record, "instance", None)
    if instance != name:
        raise HTTPException(status_code=404, detail=f"no turn {turn_id} in instance '{name}'")
    return record


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
    # Both tiers launch here (capability model-tier-launch/v1). The endpoint
    # name is executor-era and kept for compatibility; what it actually means
    # is "durable, idempotent, provenance-carrying launch", which is not
    # tier-specific. A profile with neither backend cannot run at all.
    if _executor is None and not _model_ready():
        raise HTTPException(
            status_code=400,
            detail="this agent has no backend configured; /executor-runs requires "
            "an executor-tier or model-tier profile",
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

    # Launches carry no named instance but still occupy a run slot, claimed
    # synchronously so an over-capacity launch answers 429 BEFORE the durable
    # acceptance point — a 429 must never leave a record behind. Claimed after
    # validation on purpose: an invalid EDF deserves its 4xx even at capacity.
    if (request.instance is not None or request.use_history) and _executor is not None:
        raise HTTPException(
            status_code=400,
            detail="instance/use_history apply to base-tier launches; an executor run's "
            "thread is its conversation state",
        )
    release = _admit_or_429(request.instance)

    # Durable acceptance point — no awaits between the idempotency lookup
    # above and this write, so a same-key race cannot slip between them
    # (the admission claim is synchronous too).
    try:
        record = _run_store.start(
            agent_name=_profile.name,
            trigger="launch",
            prompt=request.prompt,
            # Guarded: model-tier launches (model-tier-launch/v1) reach here
            # with no executor block.
            executor=_profile.executor.executor if _profile.executor else None,
            model=_profile.executor.model if _profile.executor else None,
            snapshot_sha256=resolved.sha256 if resolved is not None else None,
            provenance=provenance,
            use_history=request.use_history,
            instance=request.instance,
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
    except Exception:
        release()
        raise

    # Ephemeral per-launch secret values (e.g. a MiraRun-minted MCP bearer
    # token) map onto the declared secret_bindings' environment_variable
    # names, exactly like repository bindings map onto connectionRefs. A
    # secret with no supplied value here is left to whatever the adapter's
    # existing static os.environ fallback provides — this channel is
    # additive, not a new required-binding gate.
    mcp_secret_env: dict[str, str] = {}
    if resolved is not None and request.context is not None:
        for binding in resolved.secret_bindings:
            if not binding.environment_variable:
                continue
            value = request.context.secrets.get(binding.name)
            if value:
                mcp_secret_env[binding.environment_variable] = value

    async def _background_run() -> None:
        try:
            await run_agent(
                request.prompt,
                record=record,
                repositories=checkouts,
                mcp_secret_env=mcp_secret_env or None,
                use_history=request.use_history,
                instance=request.instance,
            )
        except Exception as e:
            # run_agent already wrote the failure to the record; this is just
            # so a launch exception never propagates into a bare task error.
            logger.error(f"[{_profile.name}] launched run failed: {e}", exc_info=True)
        finally:
            release()

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
    instance: Optional[str] = Field(
        default=None,
        pattern=INSTANCE_NAME_PATTERN,
        description="Named instance the binding's fires belong to; None = ephemeral fires.",
    )
    provenance: Optional[RunProvenance] = None
    metadata: dict[str, str] = Field(default_factory=dict)
    externally_fired: bool = Field(
        default=False,
        description="The control plane fires this binding; MiraGen records it "
        "but registers no job (managed-schedules-external-fire/v1).",
    )
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
            instance=request.instance,
            provenance=request.provenance,
            metadata=request.metadata,
            externally_fired=request.externally_fired,
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
async def get_history(
    limit: int = 20, run_id: Optional[str] = None, instance: str = DEFAULT_INSTANCE
):
    """
    Read-only view of one instance's persisted conversation history
    (default: the `default` instance — the pre-instance contract).

    Without run_id: newest `limit` messages (default 20, max 200).
    With run_id: the message slice the history held right after that run saved
    it, recovered via the instance's sidecar. 404 if that run never saved
    history there.
    """
    _check_instance_name(instance)
    messages = _load_history_messages(instance)

    if run_id is not None:
        message_count = _sidecar_message_count(instance, run_id)
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


# ── Instances (instances/v1) ─────────────────────────────────────────────────


@app.get("/instances", dependencies=[_internal_auth])
async def list_instances():
    """Known instances: the union of persisted histories, instances named on
    retained run records, and instances currently holding a running turn.
    `history_message_count` is None for a history file that exists but does
    not parse — distinct from 0 (no messages)."""
    infos: dict[str, dict] = {}

    def info(name: str) -> dict:
        return infos.setdefault(name, {
            "name": name,
            "history_message_count": 0,
            "running": name in _busy_instances,
            "last_run": None,
        })

    if HISTORIES_DIR.exists():
        for path in sorted(HISTORIES_DIR.glob("*.json")):
            name = path.stem
            if not _INSTANCE_RE.fullmatch(name):
                continue
            entry = info(name)
            try:
                entry["history_message_count"] = len(
                    ModelMessagesTypeAdapter.validate_json(path.read_bytes())
                )
            except Exception:
                entry["history_message_count"] = None

    if _run_store is not None:
        # Newest-first, so the first record seen per instance is its latest.
        for summary in _run_store.list(limit=200):
            if summary.instance is None:
                continue
            entry = info(summary.instance)
            if entry["last_run"] is None:
                entry["last_run"] = {
                    "run_id": summary.run_id,
                    "status": summary.status,
                    "started_at": summary.started_at.isoformat(),
                }

    for name in sorted(_busy_instances):
        info(name)

    session_info = getattr(_harness, "session_info", None)
    if session_info is not None:
        for entry in infos.values():
            entry["session"] = session_info(entry["name"])

    instances = sorted(infos.values(), key=lambda entry: entry["name"])
    return {"count": len(instances), "instances": instances}


@app.get("/instances/{name}/session", dependencies=[_internal_auth])
async def instance_session(name: str):
    """The harness's session state for an instance (Grok lifecycle): its
    sequence, context size, and whether the next turn opens a rotated
    session (`fresh`), so a client can add its own recent transcript."""
    _check_instance_name(name)
    session_info = getattr(_harness, "session_info", None)
    info = session_info(name) if session_info is not None else None
    if info is None:
        raise HTTPException(status_code=404, detail=f"instance '{name}' has no harness session")
    return {"instance": name, **info}


@app.post("/instances/{name}/rotate", dependencies=[_internal_auth])
async def rotate_instance(name: str):
    """Start the instance's next session now (e.g. the user asked for a fresh
    start): memory save + handoff note, then a new session. The instance
    name, and so the client's conversation, stays the same."""
    _check_instance_name(name)
    rotate = getattr(_harness, "rotate", None)
    if rotate is None:
        raise HTTPException(status_code=409, detail="this agent's harness has no sessions to rotate")
    if name in _busy_instances:
        raise HTTPException(status_code=409, detail=f"instance '{name}' has a running turn")
    try:
        info = await rotate(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"instance '{name}' has no session") from None
    return {"instance": name, **info}


@app.delete("/instances/{name}", dependencies=[_internal_auth])
async def delete_instance(name: str):
    """Discard an instance's conversation state (history + sidecar). Run
    records are untouched — they are run telemetry, not instance state."""
    _check_instance_name(name)
    if name in _busy_instances:
        raise HTTPException(
            status_code=409,
            detail=f"instance '{name}' has a running turn; retry after it finishes",
        )
    deleted = []
    # A harness that owns its conversation natively (Grok Build) holds the
    # real state: its process, session files and working directory.
    forget = getattr(_harness, "forget", None)
    if forget is not None:
        try:
            deleted += await forget(name)
        except InstanceBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    for path in (_history_file(name), _history_sidecar(name)):
        if path.exists():
            path.unlink()
            deleted.append(path.name)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"instance '{name}' has no persisted state"
        )
    return {"instance": name, "deleted": deleted}


class ApprovalListResponse(BaseModel):
    count: int
    approvals: list[PendingApproval]
    version: int = 0   # changes whenever the pending set does (long-poll `since`)


class ResolveApprovalResponse(BaseModel):
    resolved: bool


@app.get("/approvals", response_model=ApprovalListResponse, dependencies=[_internal_auth])
async def list_approvals(
    since: Optional[int] = Query(default=None, description=(
        "Long-poll: the `version` from your last answer. With `wait`, the call "
        "returns as soon as the pending set changes, or after `wait` seconds.")),
    wait: float = Query(default=0, ge=0, le=60),
):
    broker = get_broker()
    if since is not None and wait > 0:
        await broker.wait_for_change(since, wait)
    pending = broker.pending()
    return ApprovalListResponse(count=len(pending), approvals=pending, version=broker.version)


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
    if not _model_ready():
        raise HTTPException(status_code=503, detail="Agent not ready")

    instance = request.effective_instance()
    # Held for the life of the stream: the generator below releases it, and
    # its finally runs on client disconnect too (GeneratorExit).
    release = _admit_or_429(instance)

    try:
        prompt = _apply_trigger_prompt(request.prompt)
        history_instance = instance or DEFAULT_INSTANCE

        record = (
            _run_store.start(
                agent_name=_profile.name,
                trigger="http",
                prompt=prompt,
                use_history=request.use_history,
                instance=instance,
            )
            if _run_store is not None and _profile is not None
            else None
        )

        memory_packet = None
        if _memory is not None:
            memory_packet = await _memory.prepare_context(
                instance=instance,
                run_id=record.run_id if record is not None else None,
                trigger="http",
                prompt_hint=prompt,
            )
        elif _bridge_memory is not None:
            memory_packet = SimpleNamespace(text=await _bridge_memory.prepare(
                instance=instance, run_id=record.run_id if record is not None else None,
                prompt=prompt))
        turn = HarnessTurn(
            prompt=prompt,
            instance=history_instance,
            use_history=request.use_history,
            run_id=record.run_id if record is not None else None,
            extra_instructions=memory_packet.text if memory_packet is not None else None,
        )
        harness = _model_harness()
    except Exception:
        release()
        raise

    async def event_stream():
        chunks: list[str] = []
        # §18.2: streaming runs get the same run-span/context wrapper as
        # ordinary runs — identity stamped on every child span, cleanup on
        # cancellation/failure via the try/finally around the generator.
        run_ctx = (
            _telemetry.run_span(
                "agent run",
                run_id=record.run_id if record is not None else uuid.uuid4().hex,
                trigger="http",
                tier="model",
            )
            if _telemetry is not None
            else nullcontext()
        )
        try:
          with run_ctx as run_span:
            try:
                async with harness.stream(turn) as stream:
                    async for chunk in stream:
                        chunks.append(chunk)
                        yield f"data: {chunk}\n\n"
                    stream_result = stream.result
            except Exception as e:
                if record is not None and _run_store is not None:
                    _run_store.finish(record, status="failed", error=str(e), output="".join(chunks) or None)
                    _write_model_run_events(record.run_id, error=str(e))
                    if _memory is not None:
                        await _memory.finish_turn(
                            instance=instance, run_id=record.run_id,
                            trigger="http", status="failed", error=str(e),
                        )
                raise
            if record is not None and _run_store is not None:
                usage, tool_calls = stream_result.usage, stream_result.tool_calls
                _run_store.finish(
                    record,
                    status="succeeded",
                    output="".join(chunks),
                    usage=usage,
                    tool_calls=tool_calls,
                    tool_call_count=len(tool_calls),
                    tool_call_failures=sum(1 for c in tool_calls if not c.ok),
                )
                _write_model_run_events(
                    record.run_id, tool_calls=tool_calls, usage=usage, output="".join(chunks)
                )
                if _memory is not None:
                    await _memory.finish_turn(
                        instance=instance, run_id=record.run_id,
                        trigger="http", status="succeeded", summary="".join(chunks),
                    )
                if _bridge_memory is not None:
                    await _bridge_memory.finish(instance=instance, run_id=record.run_id,
                                                output="".join(chunks), status="succeeded")
                if run_span is not None and usage is not None:
                    if usage.input_tokens:
                        run_span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
                    if usage.output_tokens:
                        run_span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
            yield "data: [DONE]\n\n"
        finally:
            release()

    headers = {"X-Miragen-Run-Id": record.run_id} if record is not None else None
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
