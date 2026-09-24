"""The zero-or-more relevance selector (§17.7 step 4).

One bounded structured-output call over at most 20 candidate cards: it
returns up to eight record ids each with an applicability reason tied to
the CURRENT request — or none, which is a correct and common answer. It
has no tools, cannot rewrite facts, change status or create memories; its
output is IDs, and every selected id is re-resolved against canonical
state before anything is rendered (the selector never authors packet
text). No rank threshold means "relevant enough" — selection is the only
gate, and on selector failure the caller injects NOTHING optional rather
than falling back to stuffing nearest neighbors.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MAX_CARDS = 20
MAX_CARD_CHARS = 300           # per-card text clamp for the selector prompt
MAX_SELECTOR_INPUT_CHARS = 24_000  # ≈6k tokens (§17.7)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    reason: str = Field(min_length=1, description="Applicability to the current request.")


class SelectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selections: list[Selection] = Field(default_factory=list)


SelectFn = Callable[[str, list[dict]], Awaitable[SelectionResult]]


# v2 (P1.1 eval, 2026-09-23): the v1 wording ("would change how the request
# is handled") let Haiku and Sonnet both inject related-just-in-case
# background — 17% / 33% false injections on 40 real prompts. This wording:
# 10% for Haiku at recall 0.83 (bars: ≤ 10%, ≥ 0.7).
SELECTOR_INSTRUCTIONS = """\
You select which candidate memories, if any, to inject for the CURRENT
request. Candidates were retrieved by similarity. Similarity is not
relevance, and usually none of them apply.

The agent already has its conversation and the task in front of it. Select
a candidate only when, without it, the agent would likely act WRONGLY or
have to REDISCOVER something for this exact request: a constraint or
decision that applies to what is being asked, a gotcha on the path the
request takes, a fact the request depends on, or an open intention this
request continues. Give the reason as that concrete consequence for THIS
request.

Do NOT select:
- background that is merely about the same system, repo or area;
- precedents, "similar past work", or anything that would matter only IF the
  request turned out to involve something it does not mention;
- near-duplicates of another selection;
- memories about different projects, branches or periods.

Short or ambiguous requests ("ok", "continue", "is it fixed?") almost never
warrant a memory unless one names exactly what is being continued. Selecting
NOTHING is correct and common. Return record_ids only; never rewrite the
memories.
"""


# pydantic-ai providers a `base_url` can re-point (OpenAI- or Anthropic-
# compatible endpoints: a local model server, a proxy).
BASE_URL_PROVIDERS = ("openai", "openai-chat", "openai-responses", "anthropic")
# Sent to a base_url endpoint that needs no key. Never OPENAI_API_KEY /
# ANTHROPIC_API_KEY: that would hand the real provider's credential to
# whatever host base_url names.
KEYLESS_PLACEHOLDER = "unused"


class SelectorError(RuntimeError):
    """A selector that cannot be built or run as configured."""


def selector_backend(model: str) -> str:
    """The backend label for a model string (safe for /health)."""
    from miragen.memory.claude_code import is_claude_code_model

    return "claude-code" if is_claude_code_model(model) else "pydantic-ai"


def validate_selector_config(
    model: str | None, base_url: str | None, api_key_env: str | None = None,
) -> None:
    """Config-load checks for a selector model + optional endpoint."""
    if api_key_env and not base_url:
        raise ValueError("api_key_env applies only with base_url")
    if model is None:
        return
    if selector_backend(model) == "claude-code":
        from miragen.memory.claude_code import model_name

        model_name(model)  # "claude-code:" alone names no model: ValueError
    if not base_url:
        return
    if selector_backend(model) == "claude-code":
        raise ValueError(
            "base_url applies to pydantic-ai models only, not claude-code:<model> "
            "(the Claude Code CLI owns its endpoint)"
        )
    provider = model.split(":", 1)[0] if ":" in model else ""
    if provider not in BASE_URL_PROVIDERS:
        raise ValueError(
            "base_url needs an OpenAI- or Anthropic-compatible model string ("
            + ", ".join(f"{p}:<model>" for p in BASE_URL_PROVIDERS) + f"), got {model!r}"
        )


def resolve_api_key(api_key_env: str | None) -> str:
    """The endpoint key from the env var NAMED by api_key_env (or its
    <NAME>_FILE twin); the keyless placeholder when no name is configured."""
    if not api_key_env:
        return KEYLESS_PLACEHOLDER
    value = os.environ.get(api_key_env)
    if not value and os.environ.get(f"{api_key_env}_FILE"):
        try:
            value = Path(os.environ[f"{api_key_env}_FILE"]).read_text().strip()
        except OSError as exc:
            raise SelectorError(f"cannot read {api_key_env}_FILE: {exc}") from None
    if not value:
        raise SelectorError(f"api_key_env names {api_key_env}, but neither it nor "
                            f"{api_key_env}_FILE is set")
    return value


def endpoint_model(model: str, base_url: str, api_key: str) -> Any:
    """A pydantic-ai Model for `provider:name` served at base_url."""
    provider, name = model.split(":", 1)
    if provider == "anthropic":
        try:
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider
        except ImportError as exc:
            raise SelectorError(
                "anthropic:<model> with base_url needs the anthropic package "
                f"(pip install 'pydantic-ai-slim[anthropic]'): {exc}"
            ) from None
        return AnthropicModel(name, provider=AnthropicProvider(base_url=base_url, api_key=api_key))
    from pydantic_ai.providers.openai import OpenAIProvider

    openai_provider = OpenAIProvider(base_url=base_url, api_key=api_key)
    if provider == "openai-responses":
        from pydantic_ai.models.openai import OpenAIResponsesModel

        return OpenAIResponsesModel(name, provider=openai_provider)
    from pydantic_ai.models.openai import OpenAIChatModel

    return OpenAIChatModel(name, provider=openai_provider)


def build_model_selector(
    model: str, *, base_url: str | None = None, api_key_env: str | None = None,
    timeout_s: float | None = None,
) -> SelectFn:
    """The selector for a model string: `claude-code:<model>` runs headless
    Claude Code on the subscription (miragen.memory.claude_code); anything
    else is a pydantic-ai model string, optionally re-pointed at base_url.
    `timeout_s` bounds one selection (None = the runner's own default for
    claude-code, unbounded here for pydantic-ai; the caller's recall bound
    applies either way). The returned function carries `backend`, `model`
    and `base_url_configured` for /health — never the URL itself."""
    from miragen.memory.claude_code import DEFAULT_TIMEOUT_S, ClaudeCodeRunner

    validate_selector_config(model, base_url, api_key_env)
    if selector_backend(model) == "claude-code":
        runner = ClaudeCodeRunner(model, timeout=timeout_s or DEFAULT_TIMEOUT_S)

        async def select(request: str, cards: list[dict]) -> SelectionResult:
            return await runner.run(
                SELECTOR_INSTRUCTIONS, render_selector_input(request, cards), SelectionResult,
            )
    else:
        # Lazy construction: pydantic-ai validates provider credentials at
        # Agent construction, and the selector may be built at boot on
        # profiles (executor tier) whose model key only matters if the lane
        # ever actually runs.
        agent = None

        async def select(request: str, cards: list[dict]) -> SelectionResult:
            nonlocal agent
            if agent is None:
                from pydantic_ai import Agent

                target = (endpoint_model(model, base_url, resolve_api_key(api_key_env))
                          if base_url else model)
                agent = Agent(model=target, instructions=SELECTOR_INSTRUCTIONS,
                              output_type=SelectionResult)
            run = agent.run(render_selector_input(request, cards))
            result = await (asyncio.wait_for(run, timeout_s) if timeout_s else run)
            return result.output

    select.backend = selector_backend(model)  # type: ignore[attr-defined]
    select.model = model  # type: ignore[attr-defined]
    select.base_url_configured = bool(base_url)  # type: ignore[attr-defined]
    return select


def render_selector_input(request: str, cards: list[dict]) -> str:
    lines = [f"Current request:\n{request[:2000]}", "", "Candidate memories:"]
    for card in cards[:MAX_CARDS]:
        payload = card.get("payload") or {}
        text = str(payload.get("text") or payload.get("value") or payload)[:MAX_CARD_CHARS]
        slot = card.get("slot") or {}
        descriptor = f" [{slot.get('subject')} {slot.get('predicate')}]" if slot else ""
        lines.append(
            f"- record_id={card['record_id']} type={card['type']}{descriptor}: {text}"
        )
    rendered = "\n".join(lines)
    return rendered[:MAX_SELECTOR_INPUT_CHARS]


def clamp_selections(
    result: SelectionResult, candidates: list[dict], max_selected: int
) -> list[Selection]:
    """Deterministic post-filter: only ids that were actually candidates
    (the selector cannot introduce records), deduplicated, capped."""
    candidate_ids = {card["record_id"] for card in candidates}
    seen: set[str] = set()
    kept: list[Selection] = []
    for selection in result.selections:
        if selection.record_id not in candidate_ids or selection.record_id in seen:
            continue
        seen.add(selection.record_id)
        kept.append(selection)
        if len(kept) >= max_selected:
            break
    return kept


_OPTIONAL_HEADER = "[recalled memories — attributed reference data, selected for this request]"


def _optional_line(entry: dict) -> str:
    return (
        f"- ({entry['type']}, {entry['record_id'][:8]}) {entry['text']}"
        f" | why: {entry['reason']}"
    )


def fit_optional_entries(entries: list[dict], budget_chars: int) -> list[dict]:
    """The prefix of `entries` the optional section actually emits: items
    that do not fit are omitted whole (§17.7 step 6), and so is everything
    after the first that does not fit."""
    used = len(_OPTIONAL_HEADER)
    fitted = []
    for entry in entries:
        line = _optional_line(entry)
        if used + len(line) > budget_chars:
            break
        fitted.append(entry)
        used += len(line)
    return fitted


def render_optional_section(entries: list[dict], budget_chars: int) -> str:
    """The packet's optional section: canonical payload text (never
    selector prose), source-labeled, budget-clamped — items that do not
    fit are omitted whole (§17.7 step 6)."""
    fitted = fit_optional_entries(entries, budget_chars)
    return "\n".join([_OPTIONAL_HEADER, *map(_optional_line, fitted)]) if fitted else ""
