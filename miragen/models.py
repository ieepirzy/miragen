from __future__ import annotations

from typing import Annotated, Literal, Optional, Union
from pydantic import BaseModel, Field, HttpUrl, model_validator


# ── Approval flow ────────────────────────────────────────────────────────────

class ApprovalRequest(BaseModel):
    agent_name: str
    tool_name: str
    tool_args: dict
    request_id: str  # uuid4


class ApprovalResponse(BaseModel):
    approved: bool
    prompt: Optional[str] = None  # folded back into agent context if provided


# ── Triggers ────────────────────────────────────────────────────────────────

class CronTrigger(BaseModel):
    type: Literal["cron"]
    schedule: str                               # standard cron expression
    default_prompt: Optional[str] = None        # injected if no prompt provided at runtime


class HttpTrigger(BaseModel):
    type: Literal["http"]
    header_prompt: Optional[str] = None         # prepended to every incoming /run request body


Trigger = Annotated[
    Union[CronTrigger, HttpTrigger],
    Field(discriminator="type"),
]


# ── On-complete ──────────────────────────────────────────────────────────────

class OnComplete(BaseModel):
    log_to: Optional[str] = None                # named storage target e.g. "miradb"
    notify: Optional[str] = None                # named notification channel e.g. "telegram"
    post_to: Optional[HttpUrl] = None           # arbitrary webhook — escape hatch for any output routing


# ── PydanticAI spec (their layer) ───────────────────────────────────────────

class ModelSettings(BaseModel):
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


class AgentSpec(BaseModel):
    model: str                                  # any pydantic-ai model string e.g. "anthropic:claude-sonnet-4-6"
    instructions: str                           # system prompt, supports YAML block scalar (|)
    model_settings: Optional[ModelSettings] = None
    capabilities: Optional[list[str | dict]] = None   # str = no-config cap, dict = cap with config
    max_steps: Optional[int] = None             # maps to UsageLimits(request_limit=N)


# ── Top-level agent profile ──────────────────────────────────────────────────

class AgentProfile(BaseModel):
    name: str
    mode: Literal["autonomous", "interactive", "hybrid"]
    triggers: list[Trigger]
    approval_required: Optional[list[str]] = None   # glob patterns e.g. ["delete_*", "execute_*"]
    approval_webhook: Optional[HttpUrl] = None      # POST ApprovalRequest, expect ApprovalResponse
    tools: Optional[list[str]] = None               # whitelisted tool names; None = no tools injected
    on_complete: Optional[OnComplete] = None
    spec: AgentSpec

    @model_validator(mode="after")
    def validate_triggers_match_mode(self) -> AgentProfile:
        types = {t.type for t in self.triggers}

        if self.mode == "autonomous" and "http" in types and "cron" not in types:
            raise ValueError(
                "autonomous agents should have at least one cron trigger; "
                "http-only autonomous agents will never self-activate"
            )

        if self.mode == "interactive" and "cron" in types:
            raise ValueError(
                "interactive agents cannot have cron triggers; use hybrid mode instead"
            )

        return self