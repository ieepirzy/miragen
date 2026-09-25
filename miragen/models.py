from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, Optional, Union

from apscheduler.triggers.cron import CronTrigger as _APCronTrigger

from miragen.profile_contract import format_api_version, parse_api_version
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


# ── Approval flow ────────────────────────────────────────────────────────────
#
# Wire models exchanged with approval handlers / webhooks. These stay lenient
# (extra keys allowed) so third-party approval services can attach metadata.

class ApprovalRequest(BaseModel):
    agent_name: str
    tool_name: str
    tool_args: dict
    request_id: str  # uuid4


class ApprovalResponse(BaseModel):
    approved: bool
    prompt: Optional[str] = None  # folded back into agent context if provided


# ── Run records ──────────────────────────────────────────────────────────────
#
# Wire models for run telemetry (RunStore, miragen/runs.py). Lenient like the
# approval models above — these are persisted/served data, not hand-authored
# config, so extra="forbid" would just make old records on disk unreadable
# after a schema change.

class ToolCallRecord(BaseModel):
    tool_name: str
    args: str  # JSON-encoded, truncated to 2_000 chars
    ok: bool  # False if the call raised / was denied


# Instance names share the agent-name grammar: they key filesystem paths
# (histories/<instance>.json) exactly like agent names key workspace dirs,
# so the same traversal-safe restriction applies (instance model ADR:
# docs/design/instance-model.md).
INSTANCE_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"
DEFAULT_INSTANCE = "default"


class RunUsage(BaseModel):
    requests: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    # Prompt-cache reads, where the adapter reports them (issue #33 Phase E).
    # None = the executor cannot report this metric; never coerced to 0.
    cached_input_tokens: Optional[int] = None


def sum_usage(a: Optional[RunUsage], b: Optional[RunUsage]) -> Optional[RunUsage]:
    """Combine usage across turns of the same run (resumed executor turns,
    or any other multi-turn accounting) — sums so each side's true total is
    preserved even when one side is None."""
    if a is None:
        return b
    if b is None:
        return a
    return RunUsage(
        requests=a.requests + b.requests,
        input_tokens=(a.input_tokens or 0) + (b.input_tokens or 0) or None,
        output_tokens=(a.output_tokens or 0) + (b.output_tokens or 0) or None,
        cached_input_tokens=(a.cached_input_tokens or 0) + (b.cached_input_tokens or 0) or None,
    )


RunStatus = Literal[
    "running", "succeeded", "failed", "interrupted",
    # Executor-tier states: suspended/failed executor runs are RESUMABLE
    # (workspace + thread survive); abandoned is the only human-terminal state.
    "suspended", "abandoned",
]

# "launch" = POST /executor-runs: an idempotent, provenance-carrying launch
# from an external control plane (issue #33 Phase B). "managed" = a fire
# from an API-owned schedule binding (Phase F), distinct from profile-driven
# "cron" so projections can tell them apart.
RunTrigger = Literal["cron", "http", "http_async", "launch", "managed"]


class RunProvenance(BaseModel):
    """Caller/product provenance persisted with a run BEFORE launch is
    acknowledged. miragen stores and returns these verbatim; it never
    interprets them — product entities stay authoritative in the control
    plane. extra="allow" so product-side fields survive the round trip."""

    model_config = ConfigDict(extra="allow")

    idempotency_key: Optional[str] = None
    environment_id: Optional[str] = None
    environment_revision: Optional[str] = None
    routine_id: Optional[str] = None
    trigger_id: Optional[str] = None
    invocation_id: Optional[str] = None
    requested_by: Optional[str] = None
    edf_api_version: Optional[str] = None


class RepositoryRevision(BaseModel):
    """A repository the run's workspace plan includes. `commit` stays None
    until workspace preparation resolves the ref (issue #33 Phase D)."""

    name: str
    ref: str
    mount_path: Optional[str] = None
    writable: bool = False
    commit: Optional[str] = None


# ── Structured interventions (issue #33 Phase G) ─────────────────────────────
#
# The executor-emitted question and the human's structured answer. Wire
# models, lenient (extra="allow"): the question file is agent-authored and
# the answer may carry product-side fields miragen stores verbatim.

class InterventionOption(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    label: Optional[str] = None
    description: Optional[str] = None


class InterventionRequest(BaseModel):
    """A structured question that suspended a run (exit_reason
    'intervention'). `intervention_id`/`requested_at` are miragen-assigned;
    everything else comes from the executor's .miragen/intervention.json."""

    model_config = ConfigDict(extra="allow")

    intervention_id: str
    question: str = Field(min_length=1)
    kind: Optional[str] = None  # e.g. 'architecture-decision', 'confirmation'
    options: list[InterventionOption] = Field(default_factory=list)
    evidence: Optional[str] = None
    affected_repositories: Optional[list[str]] = None
    requested_at: str  # ISO-8601 UTC


class InterventionAnswer(BaseModel):
    """The structured answer/resume payload, bound to the pending
    intervention by id. `approval_ref` is the server-side authorization
    hook: recorded immutably in the event stream, never inferred from
    prompt text."""

    model_config = ConfigDict(extra="allow")

    intervention_id: str = Field(min_length=1)
    decision: Optional[str] = Field(default=None, description="Chosen option id, when options were offered.")
    text: Optional[str] = Field(default=None, description="Free-text answer/guidance.")
    approval_ref: Optional[str] = None
    answered_by: Optional[str] = None

    @model_validator(mode="after")
    def validate_has_content(self) -> "InterventionAnswer":
        if self.decision is None and self.text is None:
            raise ValueError("an intervention answer requires at least one of `decision` or `text`")
        return self


class TargetOperationProvenance(BaseModel):
    """Target-operation provenance an event MAY carry under its `target` key
    (issue #33 Phases C/G). Defined now so the envelope contract is stable;
    targets themselves are not required (or provisioned) in v1 — emitters
    arrive with the target adapters."""

    model_config = ConfigDict(extra="allow")

    target_id: str = Field(min_length=1)
    target_name: Optional[str] = None
    operation_class: Literal["inspect", "read", "write", "destructive"]
    credential_grant_ref: Optional[str] = None
    approval_ref: Optional[str] = None


class RunRecord(BaseModel):
    run_id: str  # uuid4 hex
    agent_name: str
    trigger: RunTrigger
    status: RunStatus
    prompt: str  # truncated to 20_000 chars
    output: Optional[str] = None  # truncated to 100_000 chars
    error: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_s: Optional[float] = None
    usage: Optional[RunUsage] = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    use_history: bool = False
    # The named instance this run belongs to (instance model ADR). None =
    # an ephemeral run with no persistent state scope — the default for
    # scheduled fires and stateless HTTP runs, and what every pre-instance
    # record on disk reads back as.
    instance: Optional[str] = None
    # Executor-tier fields (None for model-tier runs). The thread handle lives
    # on the agent-run record, not any job record — resume re-opens the thread
    # bound to this run. exit_reason qualifies non-succeeded terminal states
    # (e.g. 'budget', 'crash', 'abandoned').
    thread_id: Optional[str] = None
    workspace: Optional[str] = None
    exit_reason: Optional[str] = None
    diff_path: Optional[str] = None  # set exactly once, on terminal success
    # None = no sink configured / not applicable; True/False = sink outcome.
    # Advisory only — the diff on disk is the source of truth either way.
    artifact_stored: Optional[bool] = None
    # Execution provenance (issue #33 Phases A/B) — None for runs launched
    # outside POST /executor-runs. snapshot_sha256 is the canonical EDF hash;
    # the full snapshot document lives beside the record (RunStore snapshots).
    executor: Optional[str] = None
    model: Optional[str] = None
    snapshot_sha256: Optional[str] = None
    provenance: Optional[RunProvenance] = None
    repositories: Optional[list[RepositoryRevision]] = None
    # Whole-run outcome classification from the harvested final diff (backend-
    # owned path analysis — never model free-text). None = unknown / no harvest;
    # [] = harvest completed with nothing affected / no categories.
    affected_repositories: Optional[list[str]] = None
    change_categories: Optional[
        list[Literal["documentation", "code", "structural"]]
    ] = None
    # Timing/telemetry intervals (issue #33 Phase E). Formulas:
    #   wall clock  = duration_s = finished_at - started_at (includes blocked)
    #   blocked_s   = Σ (resume time - previous finished_at) across reopens
    #   active_s    = duration_s - blocked_s
    #   setup_s     = Σ per-turn workspace-preparation time
    # resume_count counts reopen transitions. Tool-call summaries come from
    # normalized item events; all stay None where an executor can't report.
    resume_count: int = 0
    blocked_s: Optional[float] = None
    active_s: Optional[float] = None
    setup_s: Optional[float] = None
    tool_call_count: Optional[int] = None
    tool_call_failures: Optional[int] = None
    # The structured question this run is suspended on (exit_reason
    # 'intervention'), cleared when the run is resumed. History lives in the
    # event stream (intervention.requested/answered/superseded).
    pending_intervention: Optional[InterventionRequest] = None
    # Audio files this run's speak calls produced (docs/design/voice.md),
    # annotated post-finish from the run's audio/ directory. None = no voice
    # activity (or a provider that played the audio itself and returned none).
    audio_artifacts: Optional[list[str]] = None


class RunSummary(BaseModel):
    # Everything in RunRecord except prompt/output/tool_calls, plus previews.
    run_id: str
    agent_name: str
    trigger: RunTrigger
    status: RunStatus
    prompt_preview: str  # first 200 chars of prompt
    output_preview: Optional[str] = None  # first 200 chars of output
    error: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_s: Optional[float] = None
    usage: Optional[RunUsage] = None
    use_history: bool = False
    instance: Optional[str] = None
    snapshot_sha256: Optional[str] = None
    resume_count: int = 0
    # Control-plane correlation (managed schedule provenance, etc.). Omitted
    # fields stay None; extra keys on the stored provenance survive.
    provenance: Optional[RunProvenance] = None

    @classmethod
    def from_record(cls, record: RunRecord) -> RunSummary:
        return cls(
            run_id=record.run_id,
            agent_name=record.agent_name,
            trigger=record.trigger,
            status=record.status,
            prompt_preview=record.prompt[:200],
            output_preview=record.output[:200] if record.output is not None else None,
            error=record.error,
            started_at=record.started_at,
            finished_at=record.finished_at,
            duration_s=record.duration_s,
            usage=record.usage,
            use_history=record.use_history,
            instance=record.instance,
            snapshot_sha256=record.snapshot_sha256,
            resume_count=record.resume_count,
            provenance=record.provenance,
        )


# ── Profile models ───────────────────────────────────────────────────────────
#
# Everything below is authored by hand in agent.yaml, so unknown keys are
# almost always typos (e.g. `aproval_required`). extra="forbid" turns those
# into loud validation errors instead of silently ignored config.

class _ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Triggers ────────────────────────────────────────────────────────────────

# Shared by every self-activating trigger: scheduled fires are stateless and
# ephemeral by default; naming an instance opts the trigger into persistent
# state — on the model tier its fires then run with that instance's history
# (docs/design/instance-model.md).
_TRIGGER_INSTANCE_FIELD = Field(
    default=None,
    pattern=INSTANCE_NAME_PATTERN,
    description=(
        "Named instance this trigger's fires belong to. Omitted = each fire "
        "is an ephemeral anonymous instance (stateless, today's behaviour); "
        "named = fires serialize on, and (model tier) converse with, that "
        "instance's persistent state."
    ),
)


class CronTrigger(_ProfileModel):
    type: Literal["cron"]
    schedule: str = Field(
        description="Standard 5-field cron expression, e.g. '0 9 * * 1-5' (09:00 Mon–Fri).",
        min_length=1,
    )
    default_prompt: Optional[str] = Field(
        default=None,
        description="Prompt injected when the cron fires without an explicit prompt.",
    )
    instance: Optional[str] = _TRIGGER_INSTANCE_FIELD

    @field_validator("schedule")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        try:
            _APCronTrigger.from_crontab(v)
        except ValueError as e:
            raise ValueError(
                f"invalid cron expression '{v}': {e}. "
                "Expected 5 fields (minute hour day month day_of_week), e.g. '0 9 * * 1-5'."
            )
        return v


class HttpTrigger(_ProfileModel):
    type: Literal["http"]
    header_prompt: Optional[str] = Field(
        default=None,
        description="Text prepended to every incoming POST /run request body.",
    )


class IntervalTrigger(_ProfileModel):
    type: Literal["interval"]
    every_s: int = Field(
        ge=10,
        description="Fire every N seconds. Minimum 10s, guards against accidental hot loops.",
    )
    default_prompt: Optional[str] = Field(
        default=None,
        description="Prompt injected when the interval fires without an explicit prompt.",
    )
    instance: Optional[str] = _TRIGGER_INSTANCE_FIELD


class StartupTrigger(_ProfileModel):
    type: Literal["startup"]
    default_prompt: Optional[str] = Field(
        default=None,
        description="Prompt injected when the startup trigger fires without an explicit prompt.",
    )
    delay_s: int = Field(
        default=0,
        ge=0,
        description="Seconds to wait after container boot before firing.",
    )
    instance: Optional[str] = _TRIGGER_INSTANCE_FIELD


Trigger = Annotated[
    Union[CronTrigger, HttpTrigger, IntervalTrigger, StartupTrigger],
    Field(discriminator="type"),
]


# ── On-complete ──────────────────────────────────────────────────────────────

class OnComplete(_ProfileModel):
    log_to: Optional[str] = Field(
        default=None,
        description="Named storage target registered via @register_handler, e.g. 'miradb'.",
    )
    notify: Optional[str] = Field(
        default=None,
        description="Named notification channel registered via @register_handler, e.g. 'telegram'.",
    )
    post_to: Optional[HttpUrl] = Field(
        default=None,
        description="Webhook URL that receives the run output — escape hatch for any output routing.",
    )
    speak: bool = Field(
        default=False,
        description=(
            "Voice the run output through the profile's `voice:` provider "
            "(docs/design/voice.md). Requires a voice block — enforced at load."
        ),
    )


# ── Voice (docs/design/voice.md) ─────────────────────────────────────────────

class VoiceSpec(_ProfileModel):
    """Speech provider configuration. miragen owns the speak contract: an
    `http` provider is any endpoint implementing miragen's POST schema
    ({"text", "voice", "agent"}) — it owns synthesis AND playback, answering
    202/204 (played it) or an audio/* body (stored as a run artifact). Cloud
    providers synthesize to bytes; the artifact is the deliverable (a
    container has no speaker)."""

    provider: Literal["http", "openai"] = Field(
        default="http",
        description="'http' = self-hosted endpoint speaking miragen's schema; 'openai' = OpenAI TTS.",
    )
    url: Optional[str] = Field(
        default=None,
        description="http provider: the endpoint URL miragen POSTs the speak schema to.",
        min_length=1,
    )
    api_key_env: Optional[str] = Field(
        default=None,
        description=(
            "Env var NAME holding the credential — a bearer token for an http "
            "endpoint (optional), the API key for a cloud provider (default "
            "OPENAI_API_KEY for 'openai'). Value injected at spawn via the "
            "daemon's *_API_KEY forwarding; never in the profile."
        ),
    )
    voice: Optional[str] = Field(
        default=None,
        description="Default voice id, provider-defined (e.g. 'alloy').",
    )
    model: Optional[str] = Field(
        default=None,
        description="Cloud providers only: TTS model override (openai default: gpt-4o-mini-tts).",
    )
    instructions_file: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Renderer guidance (e.g. a TTS engine's supported tags and language "
            "handling), kept in its own file and appended to the agent's system "
            "instructions under 'Speaking aloud' (base tier, every harness). A "
            "relative path resolves against the profile file's directory."
        ),
    )

    @model_validator(mode="after")
    def validate_provider_fields(self) -> "VoiceSpec":
        # Same loud-rejection philosophy as the executor spec: dead config is
        # a false sense of a guardrail.
        if self.provider == "http":
            if not self.url:
                raise ValueError("voice provider 'http' requires `url`")
            if self.model is not None:
                raise ValueError("voice `model` only applies to cloud providers, not 'http'")
        else:
            if self.url is not None:
                raise ValueError(
                    f"voice `url` only applies to the http provider, not '{self.provider}' "
                    "(cloud providers have fixed endpoints)"
                )
        return self


# ── Runtime tool library ─────────────────────────────────────────────────────

class RuntimeToolsSpec(_ProfileModel):
    schedule: bool = Field(
        default=True,
        description=(
            "The scheduling tools (schedule_wakeup / list_schedules / "
            "cancel_schedule). Effective on autonomous/hybrid agents only — "
            "a self-scheduled fire is self-activation, which interactive "
            "mode promises not to do."
        ),
    )


# ── Memory (docs/miragen-memory-agent-architecture-pass.md §17–§18) ─────────

class MemoryScopesSpec(_ProfileModel):
    read: list[str] = Field(
        default_factory=list,
        description="Registered scope ids this agent's retrieval may draw from.",
    )
    propose: list[str] = Field(
        default_factory=list,
        description="Scope ids this agent may propose memories into.",
    )
    default_write: str = Field(
        description="The scope new events/records/working state land in by default.",
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_default_in_propose(self) -> "MemoryScopesSpec":
        if self.default_write not in self.propose:
            raise ValueError(
                "memory.scopes.default_write must be listed in memory.scopes.propose "
                "— a default the agent cannot write to is dead config"
            )
        return self


class MemoryHooksSpec(_ProfileModel):
    mode: Literal["native_required", "boundary_only"] = Field(
        default="boundary_only",
        description=(
            "'boundary_only' injects at miragen's own run boundary (always "
            "available). 'native_required' additionally demands verified "
            "harness lifecycle hooks — an adapter without them is an "
            "explicit integration failure, never a silent wrapper downgrade "
            "(§18.7)."
        ),
    )


class MemoryExtractionSpec(_ProfileModel):
    enabled: bool = Field(
        default=False,
        description=(
            "Enable the bounded extraction worker (`miragen memory-worker`) "
            "for this profile's scopes. The worker authenticates as its own "
            "maintain-capable principal; enabling here only configures it."
        ),
    )
    model: Optional[str] = Field(
        default=None,
        description=(
            "Model for extraction/support-checking. Default: the profile's "
            "own spec.model (§17.2: use the main model initially, not an "
            "unvalidated cheaper substitute). Executor-tier profiles must "
            "set it explicitly."
        ),
    )


class MemoryRecallSpec(_ProfileModel):
    enabled: bool = Field(
        default=True,
        description=(
            "Run the optional recall lane at the boundary: bounded hybrid "
            "search + a zero-or-more relevance selection (one model call on "
            "cache misses). Requires a resolvable model; without one the "
            "lane reports itself unconfigured rather than degrading."
        ),
    )
    model: Optional[str] = Field(
        default=None,
        description="Selector model; default extraction.model, then spec.model.",
    )
    max_candidates: int = Field(default=20, ge=1, le=50)
    max_selected: int = Field(default=8, ge=1, le=20)
    max_optional_chars: int = Field(
        default=8000, ge=500,
        description="Budget for the optional section (~2000 tokens; §17.7).",
    )


class MemoryGuidanceSpec(_ProfileModel):
    required: bool = Field(
        default=True,
        description="Supply the versioned memory-use core guide in every prepared context (§18.8).",
    )


class MemorySpec(_ProfileModel):
    """Central memory service binding. Presence enables the memory
    lifecycle: working-state restore + guidance injection at the run
    boundary, durable event capture, and the agent memory tools."""

    backend: Literal["loimi", "ephemeral", "bridge"] = Field(
        default="loimi",
        description=(
            "'loimi' (or any implementation of the memory backend protocol "
            "at endpoint_env) — durable, production. 'ephemeral' — the "
            "built-in in-process backend: full lifecycle, ZERO durability "
            "(state dies with the process); dev/demo only. 'bridge' — take "
            "part in a hosted miragend session plane (endpoint_env = its URL, "
            "credential_env = its bearer) like an external harness session: "
            "the plane owns the Loimi principal, scopes and recall selector; "
            "memory tools come from its MCP (an MCP capability with "
            "bridge_session: true)."
        ),
    )
    project: Optional[str] = Field(
        default=None,
        max_length=512,
        description=(
            "bridge backend: the project this agent's sessions belong to (a "
            "repository remote such as 'github.com/ieepirzy/mira', or a name)."
        ),
    )
    endpoint_env: str = Field(
        default="LOIMI_MEMORY_URL",
        description="Env var NAME holding the memory service base URL.",
    )
    credential_env: str = Field(
        default="LOIMI_MEMORY_TOKEN",
        description="Env var NAME holding this agent's minted principal token — never the value.",
    )
    scopes: Optional[MemoryScopesSpec] = None
    hooks: MemoryHooksSpec = Field(default_factory=MemoryHooksSpec)
    guidance: MemoryGuidanceSpec = Field(default_factory=MemoryGuidanceSpec)
    extraction: MemoryExtractionSpec = Field(default_factory=MemoryExtractionSpec)
    recall: MemoryRecallSpec = Field(default_factory=MemoryRecallSpec)

    @model_validator(mode="after")
    def _backend_fields(self) -> "MemorySpec":
        if self.backend == "bridge":
            if self.scopes is not None:
                raise ValueError("memory backend 'bridge': the session plane owns scopes; "
                                 "remove `scopes`")
            if not self.project:
                raise ValueError("memory backend 'bridge' needs `project`")
        elif self.scopes is None:
            raise ValueError(f"memory backend '{self.backend}' needs `scopes`")
        elif self.project is not None:
            raise ValueError("`project` applies to the bridge backend only")
        return self


# ── PydanticAI spec (their layer) ───────────────────────────────────────────

class ModelSettings(_ProfileModel):
    max_tokens: Optional[int] = Field(default=None, ge=1)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)


class AgentSpec(_ProfileModel):
    model: str = Field(
        description="Any pydantic-ai model string, e.g. 'anthropic:claude-sonnet-4-6'.",
        min_length=1,
    )
    instructions: Optional[str] = Field(
        default=None,
        description="System prompt; supports YAML block scalar (|). Or use instructions_file.",
        min_length=1,
    )
    instructions_file: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "System prompt read from a file (e.g. an identity markdown file kept "
            "under version control). Resolved by the profile loader, relative to "
            "the profile file; exclusive with `instructions`."
        ),
    )
    model_settings: Optional[ModelSettings] = None
    capabilities: Optional[list[str | dict]] = Field(
        default=None,
        description=(
            "Capability list. Strings for no-config capabilities ('WebSearch'), "
            "single-key dicts for configured ones ({'Thinking': {'effort': 'low'}})."
        ),
    )
    max_steps: Optional[int] = Field(
        default=None,
        ge=1,
        description="Maps to UsageLimits(request_limit=N) — caps model round-trips per run.",
    )

    @model_validator(mode="after")
    def one_instructions_source(self) -> "AgentSpec":
        if self.instructions is None and self.instructions_file is None:
            raise ValueError("spec needs `instructions` or `instructions_file`")
        if self.instructions is not None and self.instructions_file is not None:
            raise ValueError("set `instructions` or `instructions_file`, not both")
        return self


# ── Budgets ──────────────────────────────────────────────────────────────────

class Limits(_ProfileModel):
    tokens_per_run: Optional[int] = Field(
        default=None,
        ge=1,
        description="Per-run token cap, enforced by PydanticAI (UsageLimits.total_tokens_limit).",
    )
    tokens_per_day: Optional[int] = Field(
        default=None,
        ge=1,
        description="Rolling UTC-day token cap across this agent's run records, enforced by miragen.",
    )
    on_exceeded: Literal["skip", "notify"] = Field(
        default="skip",
        description="What a blocked cron/interval/startup run does when tokens_per_day is exceeded.",
    )
    max_concurrent_runs: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Container-wide cap on concurrently running turns across all "
            "instances (instance model ADR). Overflow answers 429; scheduled "
            "fires skip. Default 4; the MIRAGEN_MAX_CONCURRENT env var "
            "overrides per deployment."
        ),
    )

    @model_validator(mode="after")
    def validate_at_least_one_cap(self) -> Limits:
        if (
            self.tokens_per_run is None
            and self.tokens_per_day is None
            and self.max_concurrent_runs is None
        ):
            raise ValueError(
                "limits block requires at least one of tokens_per_run, tokens_per_day "
                "or max_concurrent_runs (an empty limits: {} block is dead config)"
            )
        return self


# ── Executor spec (second backend tier) ─────────────────────────────────────
#
# Self-harnessed executors (Codex first) own their own agent loop; the
# contract inverts from messages-in/completions-out to workspace-in /
# diff-and-events-out. This is a SECOND TIER next to AgentSpec, not another
# model backend inside it: profiles declare exactly one of `spec` / `executor`.

class ExecutorMCPServer(_ProfileModel):
    name: str = Field(
        description="Server name as it appears in the executor's MCP config, e.g. 'loimi'.",
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    url: str = Field(
        description="Streamable-HTTP MCP endpoint URL.",
        min_length=1,
    )
    bearer_token_env: Optional[str] = Field(
        default=None,
        description=(
            "Name of the env var holding this agent's bearer token for the server. "
            "Per-agent Origo confidential-client credentials are supplied to the "
            "container at spawn (env or mounted secret) and referenced here by NAME — "
            "the token value never enters the profile."
        ),
    )


class ArtifactSinkSpec(_ProfileModel):
    """Publication backend configuration (reviewed graduation only).

    Names the external document/provenance store used by
    ``POST /runs/{id}/publications`` after human review. It is **not**
    auto-invoked on executor success — miragen keeps the harvested diff as
    the local source of truth until an orchestrator explicitly publishes.

    Backend-agnostic: ``kind`` selects the implementation (first: Loimi's
    plain REST v0 API — this is a backend process publishing on an
    orchestrator's behalf, not an LLM agent making a tool call, so MCP does
    not apply here). miragen does not hard-code product flows beyond the
    registered backends; new kinds plug in without changing the HTTP
    contract.
    """

    kind: Literal["loimi"] = Field(
        default="loimi",
        description=(
            "Publication backend implementation. 'loimi' speaks "
            "POST /v0/runs, POST /v0/artifacts, PATCH /v0/runs/{id} against "
            "Loimi's plain REST v0 API. Additional kinds may be added "
            "without changing the /publications contract."
        ),
    )
    url: str = Field(
        description="Base URL of the publication backend's REST API (e.g. Loimi's LOIMI_API_URL).",
        min_length=1,
    )
    bearer_token_env: Optional[str] = Field(
        default=None,
        description="Env var NAME holding the bearer token; value injected at spawn, never in the profile.",
    )
    document_kind: str = Field(
        default="executor_diff",
        description="`kind` stamped on stored artifacts (backend-specific; Loimi store_put_artifact).",
    )


# ── Leash (host-imposed action gate, issue #38) ──────────────────────────────

# Operation classes a leash may gate — the signals reliably recoverable from a
# backend's approval request: a file write, a shell command, or network
# egress. Open-ended shell danger (rm -rf, curl | sh) is caught by
# deny_commands, not a class.
GateClass = Literal["write", "command", "network"]


class LeashSpec(_ProfileModel):
    """Opt-in host-imposed action gate. When present, miragen answers the
    executor's per-action approval requests itself: safe actions are accepted
    instantly (no stall), gated ones are blocked (they never run) and escalate
    into a Phase-G intervention for human review. Absent = today's behaviour
    (no gating). The gate lives entirely in miragen — it costs the agent no
    context. Deterministic and ~free; an optional classifier (later) handles
    only the ambiguous shell residue."""

    gate: Optional[list[GateClass]] = Field(
        default=None,
        description=(
            "Operation classes to gate. None = default by agent mode: autonomous "
            "gets a long leash (['network'] only — plus deny_commands); hybrid a "
            "short one (['write','command','network'] — every escaping action)."
        ),
    )
    deny_commands: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns matched against a command string; a match is gated "
            "regardless of class — the free-form-shell backstop (e.g. 'rm\\\\s+-rf', "
            "'curl.*\\\\|\\\\s*sh')."
        ),
    )

    @field_validator("deny_commands")
    @classmethod
    def validate_patterns(cls, v: list[str]) -> list[str]:
        import re
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"invalid deny_commands regex {pattern!r}: {e}")
        return v

    def gated_classes(self, mode: str) -> set[str]:
        if self.gate is not None:
            return set(self.gate)
        return {"network"} if mode == "autonomous" else {"write", "command", "network"}


# grok-build fields that only the headless transport wires (argv flags or
# the hermetic GROK_HOME it launches against). "Set" = not the default.
_GROK_HEADLESS_ONLY_FIELDS = (
    "grok_hermetic",
    "grok_tools",
    "grok_disallowed_tools",
    "grok_permission_mode",
    "grok_allow",
    "grok_deny",
    "grok_max_turns",
)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _grok_field_set(spec: "ExecutorSpec", name: str) -> bool:
    value = getattr(spec, name)
    return bool(value) if isinstance(value, (bool, list)) else value is not None


class ExecutorSpec(_ProfileModel):
    executor: Literal["codex", "claude-code", "spawn", "kimi-code", "grok-build"] = Field(
        description=(
            "Executor backend. 'codex' (openai-codex), 'claude-code' "
            "(claude-agent-sdk), 'kimi-code' (kimi-agent-sdk), 'grok-build' "
            "(Grok Build CLI headless), or 'spawn' (argv-template fallback). "
            "The contract is executor-agnostic."
        ),
    )
    instructions: str = Field(
        description="Task framing prepended to the first prompt of every job.",
        min_length=1,
    )
    model: Optional[str] = Field(
        default=None,
        description="Executor-side model override (e.g. a codex model name); executor default when None.",
    )
    sandbox_mode: Literal["read-only", "workspace-write", "danger-full-access"] = Field(
        default="workspace-write",
        description="Codex sandbox mode, passed per turn. The container itself is the outer sandbox.",
    )
    approval_policy: Literal["untrusted", "on-failure", "on-request", "never"] = Field(
        default="never",
        description=(
            "MUST be effective for unattended runs — an executor waiting on interactive "
            "approval stalls the job. Default 'never' (the container + sandbox_mode are the guard)."
        ),
    )
    network_access: bool = Field(
        default=False,
        description="Allow network access inside the executor sandbox (workspace-write mode).",
    )
    web_search: bool = Field(default=False)
    reasoning_effort: Optional[Literal["minimal", "low", "medium", "high"]] = None
    workspace_root: str = Field(
        default="/agent/workspaces",
        description=(
            "Parent directory for per-run workspaces. Workspace lifetime != container "
            "lifetime: mount a persistent volume here so suspended/failed runs stay resumable."
        ),
    )
    codex_home: str = Field(
        default="/agent/codex-home",
        description=(
            "CODEX_HOME for the codex executor (maps to the CODEX_HOME env var "
            "the App Server / openai-codex SDK inherit — not a CodexConfig field). "
            "auth.json must be volume-mounted here (ephemeral containers fail auth "
            "on spawn otherwise); miragen writes config.toml (MCP servers) into it "
            "at startup. See docs/design/codex-auth.md."
        ),
    )
    kimi_home: Optional[str] = Field(
        default=None,
        description=(
            "kimi-code only: KIMI_CODE_HOME for OAuth/config/sessions (default "
            "/agent/kimi-home when executor is kimi-code). Mount a shared volume "
            "populated once via `miragen kimi-login`. See docs/design/subscription-homes.md."
        ),
    )
    grok_home: Optional[str] = Field(
        default=None,
        description=(
            "grok-build only: GROK_HOME for OAuth/config/sessions (default "
            "/agent/grok-home when executor is grok-build). Mount a shared volume "
            "populated once via `miragen grok-login`. See "
            "docs/design/subscription-homes.md."
        ),
    )
    grok_transport: Optional[Literal["headless", "acp"]] = Field(
        default=None,
        description=(
            "grok-build only: 'headless' (default, streaming-json one-shot) or "
            "'acp' (grok agent stdio — required for host leash and preferred for "
            "MCP injection). See packages/grok-build-client."
        ),
    )
    mcp_servers: Optional[list[ExecutorMCPServer]] = Field(
        default=None,
        description="MCP servers injected into the executor's config at startup (e.g. Loimi via Origo).",
    )
    grok_auth: Optional[Literal["auto", "subscription"]] = Field(
        default=None,
        description=(
            "grok-build only (default 'auto' on grok-build). 'auto' keeps "
            "grok's own precedence: the GROK_HOME subscription session wins, "
            "XAI_API_KEY is the silent metered fallback when no session is "
            "active. 'subscription' removes XAI_API_KEY / GROK_CODE_XAI_API_KEY "
            "from the grok process env on every transport, so an expired or "
            "missing login fails the turn instead of billing the API key."
        ),
    )
    grok_hermetic: bool = Field(
        default=False,
        description=(
            "grok-build headless only. miragen owns GROK_HOME's config.toml "
            "and requirements.toml and rewrites both atomically at every "
            "start: Claude/Cursor/Codex compat discovery, subagents, memory, "
            "managed MCPs, remote managed config, the shared leader and trace "
            "upload are off; the ONLY MCP servers are this spec's "
            "`mcp_servers` (headers reference `${bearer_token_env}`, never "
            "the value), pinned by a requirements.toml URL allowlist that "
            "also blocks project `.grok/config.toml` / `.mcp.json` servers; "
            "only managed hooks run. The grok process gets an empty HOME "
            "(so ~/.claude plugins, ~/.agents skills and ~/.claude.json are "
            "invisible) and no GROK_* env except GROK_HOME. Auth files in "
            "GROK_HOME are never touched. The home must be dedicated to this "
            "agent: prepare() refuses a home another agent owns. See "
            "docs/design/kimi-and-grok-executors.md."
        ),
    )
    grok_tools: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        description=(
            "grok-build headless only: built-in tool allowlist (`--tools`), "
            "e.g. ['search_tool', 'use_tool'] for MCP-only. None = grok's "
            "default toolset."
        ),
    )
    grok_disallowed_tools: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        description=(
            "grok-build headless only: tools removed (`--disallowed-tools`); "
            "'Agent' blocks every subagent. Wins over grok_tools."
        ),
    )
    grok_permission_mode: Optional[
        Literal["default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"]
    ] = Field(
        default=None,
        description=(
            "grok-build headless only: `--permission-mode`. When set it "
            "replaces the implicit `--always-approve` derived from "
            "approval_policy='never'. 'dontAsk' denies everything not "
            "pre-approved by grok_allow — but grok still auto-approves its "
            "read-only tools (read_file, grep, list_dir, web_search, skills) "
            "in every mode, so pair it with grok_tools and grok_deny."
        ),
    )
    grok_allow: list[str] = Field(
        default_factory=list,
        description=(
            "grok-build headless only: repeated `--allow` rules, e.g. "
            "'MCPTool(loimi__store_search)'."
        ),
    )
    grok_deny: list[str] = Field(
        default_factory=list,
        description=(
            "grok-build headless only: repeated `--deny` rules, e.g. 'Bash', "
            "'Read', 'MCPTool(loimi__*_delete)'. Deny wins over allow."
        ),
    )
    grok_max_turns: Optional[int] = Field(
        default=None,
        gt=0,
        description="grok-build headless only: `--max-turns` cap on agentic turns per job turn.",
    )
    turn_timeout_s: Optional[int] = Field(
        default=1800,
        gt=0,
        description=(
            "Wall-clock cap on a single executor turn, enforced by miragen "
            "independent of the backend (asyncio timeout around the whole turn). "
            "On expiry the run suspends (exit_reason='timeout') — resumable, no "
            "harvest. None disables the cap. The token budget is a spend limit, "
            "not a clock; this is the clock."
        ),
    )
    command: Optional[list[str]] = Field(
        default=None,
        description=(
            "spawn backend only: argv template run inside the workspace. "
            "'{workspace}' and '{prompt}' are substituted per element; if no "
            "element contains '{prompt}', the prompt is written to stdin."
        ),
    )
    artifact_sink: Optional[ArtifactSinkSpec] = Field(
        default=None,
        description=(
            "Publication backend for POST /runs/{id}/publications (reviewed "
            "graduation). Not auto-called on success — the harvested diff stays "
            "local until an orchestrator explicitly publishes. Required for the "
            "reviewed-publication/v1 capability to be usable."
        ),
    )
    leash: Optional[LeashSpec] = Field(
        default=None,
        description=(
            "Opt-in host-imposed action gate (issue #38). Absent = no gating "
            "(today's behaviour). Present = miragen answers the executor's "
            "per-action approval requests, blocking gated operations and "
            "escalating them into a human intervention."
        ),
    )

    @model_validator(mode="after")
    def validate_backend_fields(self) -> "ExecutorSpec":
        # Same loud-rejection philosophy as the model-tier/executor-tier field
        # split: dead config is a false sense of a guardrail.
        if self.executor == "spawn":
            if not self.command:
                raise ValueError("spawn executor requires `command` (argv template)")
            if self.mcp_servers:
                raise ValueError(
                    "spawn executor cannot inject `mcp_servers` — a spawned CLI "
                    "reads its own config; configure its MCP access there"
                )
        elif self.command is not None:
            raise ValueError(f"`command` only applies to the spawn executor, not '{self.executor}'")

        if self.executor == "kimi-code":
            if self.kimi_home is None:
                self.kimi_home = "/agent/kimi-home"
        elif self.kimi_home is not None:
            raise ValueError(f"`kimi_home` only applies to the kimi-code executor, not '{self.executor}'")

        if self.executor == "grok-build":
            if self.grok_home is None:
                self.grok_home = "/agent/grok-home"
            if self.grok_transport is None:
                self.grok_transport = "headless"
            if self.leash is not None and self.grok_transport == "headless":
                raise ValueError(
                    "grok-build host leash requires grok_transport: acp "
                    "(headless has no pre-tool approval seam)"
                )
            if self.grok_auth is None:
                self.grok_auth = "auto"
            if self.mcp_servers and self.grok_transport == "headless" and not self.grok_hermetic:
                # Same dead-config rule as leash+headless: plain headless has
                # no injection seam, so declared servers would silently never
                # reach the agent. grok_hermetic IS the seam (it writes them
                # into GROK_HOME/config.toml).
                raise ValueError(
                    "grok-build `mcp_servers` with grok_transport: headless requires "
                    "grok_hermetic: true (miragen then owns GROK_HOME/config.toml); "
                    "otherwise use grok_transport: acp"
                )
            if self.grok_transport != "headless":
                for field_name in _GROK_HEADLESS_ONLY_FIELDS:
                    if _grok_field_set(self, field_name):
                        raise ValueError(
                            f"`{field_name}` requires grok_transport: headless "
                            f"(not wired for '{self.grok_transport}')"
                        )
            if self.grok_hermetic:
                for server in self.mcp_servers or []:
                    env = server.bearer_token_env
                    if env is not None and not _ENV_NAME_RE.fullmatch(env):
                        raise ValueError(
                            f"grok_hermetic: mcp_servers[{server.name}].bearer_token_env "
                            f"{env!r} is not a plain env var name (it is written as "
                            "a ${...} reference into config.toml)"
                        )
                    if "${" in server.url:
                        raise ValueError(
                            f"grok_hermetic: mcp_servers[{server.name}].url must be literal "
                            "(grok would expand ${...} in it; the URL allowlist needs "
                            "the exact value)"
                        )
            if self.web_search and self.grok_tools is not None and not (
                {"web_search", "web_fetch"} & set(self.grok_tools)
            ):
                raise ValueError(
                    "grok-build web_search: true is dead config with a grok_tools "
                    "allowlist that lists neither web_search nor web_fetch"
                )
        else:
            if self.grok_home is not None:
                raise ValueError(f"`grok_home` only applies to the grok-build executor, not '{self.executor}'")
            if self.grok_transport is not None:
                raise ValueError(
                    f"`grok_transport` only applies to the grok-build executor, not '{self.executor}'"
                )
            for field_name in ("grok_auth", *_GROK_HEADLESS_ONLY_FIELDS):
                if _grok_field_set(self, field_name):
                    raise ValueError(
                        f"`{field_name}` only applies to the grok-build executor, "
                        f"not '{self.executor}'"
                    )

        return self


# ── Top-level agent profile ──────────────────────────────────────────────────

class AgentProfile(_ProfileModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: Optional[str] = Field(
        default=None,
        alias="apiVersion",
        description=(
            "Profile contract this profile is written against, e.g. "
            "'miragen/v2'. Optional: the effective requirement is the "
            "greater of this declaration and what the profile's features "
            "imply (an executor block implies v2), so a profile can pin "
            "forward but never understate what it uses. miragend refuses "
            "to spawn a profile onto a runtime image that does not "
            "declare support for the required contract (#75)."
        ),
    )
    name: str = Field(
        description=(
            "Unique agent ID, also used as the Docker container name. Lowercase "
            "letters, digits, hyphens and underscores; max 63 chars."
        ),
        pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$",
    )
    mode: Literal["autonomous", "interactive", "hybrid"]
    triggers: list[Trigger] = Field(min_length=1)
    approval_required: Optional[list[str]] = Field(
        default=None,
        description=(
            "Human-in-the-loop gating rules: fnmatch tool globs ('delete_*'), optionally "
            "with one argument condition — 'tool:arg=g1|g2' (gated when the argument "
            "matches) or 'tool:arg!=g1|g2' (gated unless it matches; fail closed, e.g. "
            "'crm_execute_tool:toolName!=find_*|get_*')."
        ),
    )

    @field_validator("approval_required")
    @classmethod
    def _approval_rules_parse(cls, rules: Optional[list[str]]) -> Optional[list[str]]:
        from miragen.approval import parse_approval_rule

        for rule in rules or []:
            parse_approval_rule(rule)
        return rules
    approval_webhook: Optional[HttpUrl] = Field(
        default=None,
        description="URL that receives ApprovalRequest POSTs and returns an ApprovalResponse.",
    )
    approval_mode: Literal["open", "strict", "queue"] = Field(
        default="open",
        description=(
            "What a gated tool call does when no handler/webhook is configured: "
            "'open' auto-approves with a warning (default, today's behaviour), "
            "'strict' denies, 'queue' parks the request for HTTP resolution via /approvals."
        ),
    )
    approval_timeout_s: int = Field(
        default=300,
        ge=1,
        description="queue mode only — how long a request may wait before it's denied.",
    )
    tools: Optional[list[str]] = Field(
        default=None,
        description="Whitelisted @register tool names; None/omitted = no local tools injected.",
    )
    voice: Optional[VoiceSpec] = Field(
        default=None,
        description=(
            "Speech provider (docs/design/voice.md). Presence grants the "
            "agent a `speak` tool (model tier) and the /mcp/voice mount "
            "(executor tier), and enables on_complete.speak."
        ),
    )
    runtime_tools: RuntimeToolsSpec = Field(
        default_factory=RuntimeToolsSpec,
        description="The default tool library miragen ships across harnesses.",
    )
    memory: Optional[MemorySpec] = Field(
        default=None,
        description=(
            "Central memory service binding (memory architecture pass). "
            "Presence enables working-state restore + guidance at the run "
            "boundary, durable event capture, and the memory tools."
        ),
    )
    on_complete: Optional[OnComplete] = None
    inject_timestamp: bool = Field(
        default=True,
        description="Prepend the current UTC timestamp to every incoming prompt.",
    )
    limits: Optional[Limits] = None
    history_max_messages: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Cap on /agent/history.json length: when set, only the newest N messages "
            "are kept after loading (oldest dropped first). None = unbounded (today's behaviour)."
        ),
    )
    spec: Optional[AgentSpec] = Field(
        default=None,
        description="Model-tier backend (miragen owns the loop). Exactly one of spec/executor.",
    )
    executor: Optional[ExecutorSpec] = Field(
        default=None,
        description="Executor-tier backend (self-harnessed loop). Exactly one of spec/executor.",
    )

    @property
    def is_executor(self) -> bool:
        return self.executor is not None

    # -- profile contract (#75) --------------------------------------------

    def inferred_contract(self) -> int:
        """The contract level this profile's FEATURES require, regardless
        of what it declares. The executor tier is the v2 surface; everything
        the model tier ever supported is v1."""
        return 2 if self.executor is not None else 1

    def required_contract(self) -> int:
        """The effective requirement miragend checks the runtime against:
        max of declaration and inference — declaring forward is allowed,
        understating is rejected by the validator below."""
        declared = (
            parse_api_version(self.api_version) if self.api_version else 1
        )
        return max(declared, self.inferred_contract())

    @field_validator("api_version")
    @classmethod
    def validate_api_version(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            parse_api_version(v)  # raises ValueError with the pattern
        return v

    @model_validator(mode="after")
    def validate_contract_not_understated(self) -> "AgentProfile":
        if self.api_version is None:
            return self
        declared = parse_api_version(self.api_version)
        inferred = self.inferred_contract()
        if declared < inferred:
            raise ValueError(
                f"profile declares apiVersion {self.api_version} but uses "
                f"features requiring {format_api_version(inferred)} "
                "(the executor tier is a v2 surface) — a profile must not "
                "understate its runtime requirement"
            )
        return self

    @model_validator(mode="after")
    def validate_exactly_one_backend(self) -> AgentProfile:
        if (self.spec is None) == (self.executor is None):
            raise ValueError(
                "an agent profile declares exactly one backend: `spec` (model tier) "
                "or `executor` (executor tier)"
            )
        return self

    @model_validator(mode="after")
    def validate_executor_profile_fields(self) -> AgentProfile:
        if self.executor is None:
            return self
        # Model-tier-only knobs on an executor profile are dead config at best
        # and a false sense of a guardrail at worst — reject loudly.
        offending = [
            name for name, value in (
                ("tools", self.tools),
                ("approval_required", self.approval_required),
                ("approval_webhook", self.approval_webhook),
                ("history_max_messages", self.history_max_messages),
            ) if value
        ]
        if offending:
            raise ValueError(
                f"executor-tier profiles do not support model-tier fields: {offending}. "
                "Tool access is the executor's own; approvals are `executor.approval_policy`; "
                "history is the executor thread itself."
            )
        return self

    @model_validator(mode="after")
    def validate_triggers_match_mode(self) -> AgentProfile:
        types = {t.type for t in self.triggers}
        self_activating = {"cron", "interval"}

        if self.mode == "autonomous" and "http" in types and not (types & self_activating):
            raise ValueError(
                "autonomous agents should have at least one cron trigger or interval trigger; "
                "http-only autonomous agents will never self-activate"
            )

        if self.mode == "interactive" and (types & self_activating):
            raise ValueError(
                "interactive agents cannot have cron triggers or interval triggers; "
                "use hybrid mode instead"
            )

        return self

    @model_validator(mode="after")
    def validate_speak_needs_voice(self) -> AgentProfile:
        if self.on_complete is not None and self.on_complete.speak and self.voice is None:
            raise ValueError(
                "on_complete.speak requires a `voice:` block — without a "
                "provider there is nothing to speak through (dead config)"
            )
        return self

    @model_validator(mode="after")
    def validate_approval_mode_needs_approval_required(self) -> AgentProfile:
        non_default = self.approval_mode != "open" or self.approval_timeout_s != 300
        if non_default and not self.approval_required:
            raise ValueError(
                "approval_mode/approval_timeout_s are set but approval_required is empty — "
                "these only take effect once at least one glob is in approval_required "
                "(dead config, likely a typo)"
            )
        return self
