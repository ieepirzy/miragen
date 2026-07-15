from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Optional, Union

from apscheduler.triggers.cron import CronTrigger as _APCronTrigger
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


class RunUsage(BaseModel):
    requests: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


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
    )


RunStatus = Literal[
    "running", "succeeded", "failed", "interrupted",
    # Executor-tier states: suspended/failed executor runs are RESUMABLE
    # (workspace + thread survive); abandoned is the only human-terminal state.
    "suspended", "abandoned",
]


class RunRecord(BaseModel):
    run_id: str  # uuid4 hex
    agent_name: str
    trigger: Literal["cron", "http", "http_async"]
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
    # Executor-tier fields (None for model-tier runs). The thread handle lives
    # on the agent-run record, not any job record — resume re-opens the thread
    # bound to this run. exit_reason qualifies non-succeeded terminal states
    # (e.g. 'budget', 'crash', 'abandoned').
    thread_id: Optional[str] = None
    workspace: Optional[str] = None
    exit_reason: Optional[str] = None
    diff_path: Optional[str] = None  # set exactly once, on terminal success


class RunSummary(BaseModel):
    # Everything in RunRecord except prompt/output/tool_calls, plus previews.
    run_id: str
    agent_name: str
    trigger: Literal["cron", "http", "http_async"]
    status: RunStatus
    prompt_preview: str  # first 200 chars of prompt
    output_preview: Optional[str] = None  # first 200 chars of output
    error: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_s: Optional[float] = None
    usage: Optional[RunUsage] = None
    use_history: bool = False

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
        )


# ── Profile models ───────────────────────────────────────────────────────────
#
# Everything below is authored by hand in agent.yaml, so unknown keys are
# almost always typos (e.g. `aproval_required`). extra="forbid" turns those
# into loud validation errors instead of silently ignored config.

class _ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Triggers ────────────────────────────────────────────────────────────────

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


# ── PydanticAI spec (their layer) ───────────────────────────────────────────

class ModelSettings(_ProfileModel):
    max_tokens: Optional[int] = Field(default=None, ge=1)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)


class AgentSpec(_ProfileModel):
    model: str = Field(
        description="Any pydantic-ai model string, e.g. 'anthropic:claude-sonnet-4-6'.",
        min_length=1,
    )
    instructions: str = Field(
        description="System prompt; supports YAML block scalar (|).",
        min_length=1,
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

    @model_validator(mode="after")
    def validate_at_least_one_cap(self) -> Limits:
        if self.tokens_per_run is None and self.tokens_per_day is None:
            raise ValueError(
                "limits block requires at least one of tokens_per_run or tokens_per_day "
                "(an empty limits: {} block is dead config)"
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


class ExecutorSpec(_ProfileModel):
    executor: Literal["codex"] = Field(
        description="Executor backend. Codex is the first target; the contract is executor-agnostic.",
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
            "CODEX_HOME for the executor. auth.json must be volume-mounted here "
            "(ephemeral containers fail auth on spawn otherwise); miragen writes "
            "config.toml (MCP servers, trust settings) into it at startup."
        ),
    )
    mcp_servers: Optional[list[ExecutorMCPServer]] = Field(
        default=None,
        description="MCP servers injected into the executor's config at startup (e.g. Loimi via Origo).",
    )


# ── Top-level agent profile ──────────────────────────────────────────────────

class AgentProfile(_ProfileModel):
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
        description="fnmatch glob patterns for human-in-the-loop gating, e.g. ['delete_*', 'execute_*'].",
    )
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
    def validate_approval_mode_needs_approval_required(self) -> AgentProfile:
        non_default = self.approval_mode != "open" or self.approval_timeout_s != 300
        if non_default and not self.approval_required:
            raise ValueError(
                "approval_mode/approval_timeout_s are set but approval_required is empty — "
                "these only take effect once at least one glob is in approval_required "
                "(dead config, likely a typo)"
            )
        return self
