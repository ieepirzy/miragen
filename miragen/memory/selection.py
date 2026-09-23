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
import importlib.util
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

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


SELECTOR_INSTRUCTIONS = """\
You select which candidate memories, if any, genuinely help the CURRENT
request. Candidates were retrieved by similarity — similarity is not
relevance, and most candidates usually do not apply.

Select a candidate only when it would change how the request is handled:
an applicable fact, a directly relevant prior episode, an applicable
procedure, an unresolved intention this request touches. Give each
selection a reason tied to THIS request, not a summary of the memory.

Do not select: near-duplicates of another selection, generically related
background, memories about different projects/branches/periods than the
request, or anything you would only include "just in case".

Selecting NOTHING is a correct and common answer. Return record_ids only —
never rewrite or summarize the memories themselves.
"""


# ── selector backends ────────────────────────────────────────────────────────
#
# `recall.model` picks the backend by its prefix:
#   "claude-code:<model>"  headless Claude Code through claude-agent-sdk (the
#                          `claude-code` extra) — subscription auth via
#                          CLAUDE_CODE_OAUTH_TOKEN / ~/.claude, or a metered
#                          ANTHROPIC_API_KEY, exactly as the executor backend.
#   anything else          a pydantic-ai model string ("deepseek:deepseek-chat",
#                          "openai:gpt-4.1-mini", ...), optionally re-pointed at
#                          a `base_url` (an OpenAI- or Anthropic-compatible
#                          endpoint: a local model server, a proxy).
# Every backend returns a validated SelectionResult or raises; the caller's
# contract is that a raising selector injects NOTHING optional.

CLAUDE_CODE_PREFIX = "claude-code:"
DEFAULT_SELECTOR_TIMEOUT_S = 6.0   # under the session plane's 8 s retrieval bound
# pydantic-ai provider prefixes that can be re-pointed at a base_url.
BASE_URL_PROVIDERS = ("openai", "openai-chat", "openai-responses", "anthropic")


class SelectorError(RuntimeError):
    """A selection that produced no valid SelectionResult."""


def selector_backend(model: str) -> str:
    """The backend label for a `recall.model` string (safe for /health)."""
    return "claude-code" if model.startswith(CLAUDE_CODE_PREFIX) else "pydantic-ai"


def validate_selector_config(model: str | None, base_url: str | None) -> None:
    """Config-load checks shared by every place a selector is configured."""
    if model is None:
        return
    if model.startswith(CLAUDE_CODE_PREFIX):
        if not model[len(CLAUDE_CODE_PREFIX):].strip():
            raise ValueError(f"recall.model {model!r} names no model (e.g. 'claude-code:haiku')")
        if base_url:
            raise ValueError(
                "recall.base_url applies to pydantic-ai models only, not "
                f"{CLAUDE_CODE_PREFIX}<model> (the Claude Code CLI owns its endpoint)"
            )
    elif base_url:
        provider = model.split(":", 1)[0] if ":" in model else ""
        if provider not in BASE_URL_PROVIDERS:
            raise ValueError(
                "recall.base_url needs an OpenAI- or Anthropic-compatible model string ("
                + ", ".join(f"{p}:<model>" for p in BASE_URL_PROVIDERS)
                + f"), got {model!r}"
            )


def build_model_selector(
    model: str,
    *,
    base_url: str | None = None,
    api_key_env: str | None = None,
    timeout_s: float | None = DEFAULT_SELECTOR_TIMEOUT_S,
    query_factory: Callable[..., Any] | None = None,
) -> SelectFn:
    """The selector for a `recall.model` string. Nothing heavy is imported
    or constructed here — the SDK / pydantic-ai load on the first call —
    so building at boot costs nothing and cannot fail on credentials."""
    validate_selector_config(model, base_url)
    if model.startswith(CLAUDE_CODE_PREFIX):
        inner = _claude_code_selector(
            model[len(CLAUDE_CODE_PREFIX):].strip(), query_factory=query_factory,
        )
    else:
        inner = _pydantic_ai_selector(model, base_url=base_url, api_key_env=api_key_env)
    select = _bounded(inner, timeout_s)
    select.backend = selector_backend(model)  # type: ignore[attr-defined]
    select.model = model  # type: ignore[attr-defined]
    select.base_url_configured = bool(base_url)  # type: ignore[attr-defined]
    return select


def _bounded(inner: SelectFn, timeout_s: float | None) -> SelectFn:
    """A hard bound on one selection: a slow model is a selector FAILURE
    (inject nothing, the lane reports degraded), never a stalled hook. The
    cancellation reaches the backend — for claude-code it tears down the
    CLI subprocess."""

    async def select(request: str, cards: list[dict]) -> SelectionResult:
        if not timeout_s:
            return await inner(request, cards)
        try:
            return await asyncio.wait_for(inner(request, cards), timeout=timeout_s)
        except asyncio.TimeoutError:
            raise SelectorError(f"selector timed out after {timeout_s:g}s") from None

    return select


# ── pydantic-ai ──────────────────────────────────────────────────────────────


def _pydantic_ai_selector(
    model: str, *, base_url: str | None, api_key_env: str | None,
) -> SelectFn:
    # Lazy construction: pydantic-ai validates provider credentials at
    # Agent construction, and the selector may be built at boot on
    # profiles (executor tier) whose model key only matters if the lane
    # ever actually runs.
    agent = None

    async def select(request: str, cards: list[dict]) -> SelectionResult:
        nonlocal agent
        if agent is None:
            from pydantic_ai import Agent

            target = (
                endpoint_model(model, base_url, resolve_api_key(api_key_env))
                if base_url else model
            )
            agent = Agent(model=target, instructions=SELECTOR_INSTRUCTIONS,
                          output_type=SelectionResult)
        result = await agent.run(render_selector_input(request, cards))
        return result.output

    return select


# Sent when a base_url endpoint needs no key (a local model server). Never
# fall back to OPENAI_API_KEY / ANTHROPIC_API_KEY: that would hand the real
# provider's credential to whatever host base_url names.
KEYLESS_PLACEHOLDER = "unused"


def resolve_api_key(api_key_env: str | None) -> str:
    """The endpoint key from the env var NAMED by recall.api_key_env (or
    its *_FILE twin); the placeholder when no name is configured."""
    if not api_key_env:
        return KEYLESS_PLACEHOLDER
    value = os.environ.get(api_key_env)
    if not value:
        file_path = os.environ.get(f"{api_key_env}_FILE")
        if file_path:
            try:
                value = Path(file_path).read_text().strip()
            except OSError as exc:
                raise SelectorError(
                    f"recall.api_key_env: cannot read {api_key_env}_FILE: {exc}"
                ) from None
    if not value:
        raise SelectorError(
            f"recall.api_key_env names {api_key_env}, but neither it nor "
            f"{api_key_env}_FILE is set"
        )
    return value


def endpoint_model(model: str, base_url: str, api_key: str) -> Any:
    """A pydantic-ai Model object for `provider:name` served at base_url."""
    provider, name = model.split(":", 1)
    if provider == "anthropic":
        try:
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider
        except ImportError as exc:
            raise SelectorError(
                "an anthropic:<model> with recall.base_url needs the anthropic "
                f"package (pip install 'pydantic-ai-slim[anthropic]'): {exc}"
            ) from None
        return AnthropicModel(name, provider=AnthropicProvider(base_url=base_url, api_key=api_key))
    from pydantic_ai.providers.openai import OpenAIProvider

    openai_provider = OpenAIProvider(base_url=base_url, api_key=api_key)
    if provider == "openai-responses":
        from pydantic_ai.models.openai import OpenAIResponsesModel

        return OpenAIResponsesModel(name, provider=openai_provider)
    from pydantic_ai.models.openai import OpenAIChatModel

    return OpenAIChatModel(name, provider=openai_provider)


# ── headless Claude Code ─────────────────────────────────────────────────────

_neutral_cwd: str | None = None


def _selector_cwd() -> str:
    """One empty directory per process, reused: the CLI must not discover a
    project (CLAUDE.md, .claude/, .mcp.json) wherever the daemon runs, and
    a fresh temp dir per prompt would leak one per prompt."""
    global _neutral_cwd
    if _neutral_cwd is None or not Path(_neutral_cwd).is_dir():
        _neutral_cwd = tempfile.mkdtemp(prefix="miragen-selector-")
    return _neutral_cwd


def claude_code_selector_options(model: str) -> dict[str, Any]:
    """ClaudeAgentOptions kwargs for one bounded selection: no tools, one
    turn, no settings/hooks/plugins/MCP from any filesystem source, no
    session transcript, structured output against SelectionResult."""
    return {
        "model": model,
        "system_prompt": SELECTOR_INSTRUCTIONS,
        "tools": [],                 # --tools "": no built-in tools at all
        "max_turns": 1,
        "setting_sources": [],       # no user/project/local settings: no hooks, plugins, CLAUDE.md
        "strict_mcp_config": True,   # and no MCP servers from anywhere
        "cwd": _selector_cwd(),
        "thinking": {"type": "disabled"},
        "output_format": {"type": "json_schema", "schema": SelectionResult.model_json_schema()},
        # Selector runs are not sessions anyone resumes: a transcript per
        # prompt would pile up under ~/.claude/projects.
        "extra_args": {"no-session-persistence": None},
        # Skips the CLI's nonessential startup traffic (~0.5 s per spawn, measured).
        "env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
    }


def _sdk_query(prompt: str, options: dict[str, Any]) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions, query

    return query(prompt=prompt, options=ClaudeAgentOptions(**options))


def _claude_code_selector(
    model: str, *, query_factory: Callable[..., Any] | None,
) -> SelectFn:
    """`query_factory(prompt, options: dict)` returns an async iterator of
    SDK messages; the default imports claude_agent_sdk on first use.
    Messages are matched by class name (the SDK is an optional extra and
    tests drive this with stand-in classes), as in the executor backend."""
    if query_factory is None:
        warn_if_claude_code_unavailable(model)
    factory = query_factory or _sdk_query

    async def select(request: str, cards: list[dict]) -> SelectionResult:
        options = claude_code_selector_options(model)
        result = None
        async for message in factory(render_selector_input(request, cards), options):
            if type(message).__name__ == "ResultMessage":
                result = message
        return parse_claude_code_result(result)

    return select


def parse_claude_code_result(message: Any) -> SelectionResult:
    """ResultMessage → SelectionResult, or raise. Prefers the SDK's
    `structured_output`; a strict JSON parse of the result text is the
    fallback only when no structured output was attached."""
    if message is None:
        raise SelectorError("claude-code selector: no result message")
    if getattr(message, "is_error", False) or getattr(message, "subtype", None) != "success":
        detail = getattr(message, "result", None) or getattr(message, "subtype", None)
        raise SelectorError(f"claude-code selector failed: {str(detail)[:200]}")
    structured = getattr(message, "structured_output", None)
    try:
        if structured is not None:
            return SelectionResult.model_validate(structured)
        text = getattr(message, "result", None)
        if not isinstance(text, str) or not text.strip():
            raise SelectorError("claude-code selector: empty result")
        return SelectionResult.model_validate_json(text.strip())
    except ValidationError as exc:
        raise SelectorError(
            f"claude-code selector: invalid selection: {exc.errors()[:2]}"
        ) from None


def warn_if_claude_code_unavailable(model: str) -> None:
    """Startup honesty for the claude-code backend: the SDK must be
    installed and SOME credential visible (mirrors ClaudeCodeExecutor.
    prepare()). A warning, not a refusal — the lane fails closed anyway."""
    if importlib.util.find_spec("claude_agent_sdk") is None:
        logger.warning(
            f"recall.model {CLAUDE_CODE_PREFIX}{model}: claude-agent-sdk is not installed "
            "(pip install 'miragen[claude-code]') — every selection will fail and "
            "optional recall will inject nothing"
        )
    if (
        not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        and not os.environ.get("ANTHROPIC_API_KEY")
        and not (Path.home() / ".claude").exists()
    ):
        logger.warning(
            f"recall.model {CLAUDE_CODE_PREFIX}{model}: no CLAUDE_CODE_OAUTH_TOKEN, no "
            "ANTHROPIC_API_KEY and no ~/.claude credentials — selections will fail auth. "
            "Mint a subscription token with `claude setup-token`, set a metered key, or "
            "mount Claude Code credentials."
        )


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
