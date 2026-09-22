"""Harness hook payloads → the memory lifecycle's event vocabulary.

Two serializers, one vocabulary (design doc §18.7): `context.started`,
`context.restored`, `input.received`, `tool.finished`,
`context.compacting`, `context.compacted`, `turn.finished`,
`context.child_started`, `context.child_finished`, `context.closed`. The
original event name and every harness id are preserved on the event.

Field spellings are accepted tolerantly because the two harnesses (and
successive versions of each) disagree: live-probed Claude Code 2.1.270
(2026-09-15) sends `source` on SessionStart, `prompt` + `prompt_id` on
UserPromptSubmit and `reason` on SessionEnd; the published reference also
documents `how_session_started` / `user_input` / `how_session_ended`; Codex
documents `source`, `prompt`, `turn_id` and `trigger`. Reading every
spelling costs nothing and keeps the adapter working across versions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

HARNESSES = ("claude-code", "codex")

# Tool names whose events must never re-enter memory capture (§18.7:
# avoid recursion when memory tool calls themselves trigger capture).
_MEMORY_TOOL_PREFIXES = ("memory_",)
_MEMORY_TOOL_MARKERS = ("miragen-memory", "mcp__miragen__")

# Normalized events that get durable capture. Tool successes are
# deliberately NOT captured (routine boilerplate — §17.5); failures are.
CAPTURED = frozenset({
    "input.received",
    "turn.finished",
    "context.closed",
    "context.compacting",
    "context.compacted",
    "context.child_finished",
    "tool.finished",  # only reaches capture when ok=False (see normalize)
})
CONTEXT_OPENING = frozenset({"context.started", "context.restored"})
# Events whose answer may carry context back into the harness.
CONTEXT_BEARING = CONTEXT_OPENING | {"input.received"}

_CONTENT_CAP = 20_000
_ERROR_CAP = 500
_ID_KEYS = ("prompt_id", "turn_id", "tool_use_id", "agent_id")


@dataclass
class NormalizedEvent:
    name: str
    harness: str
    original_event: str
    session_id: str | None
    ids: dict[str, str] = field(default_factory=dict)
    content: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "original_event": self.original_event,
            "ids": dict(self.ids),
            "content": self.content,
            "attributes": dict(self.attributes),
        }


def _is_memory_tool(tool_name: str) -> bool:
    return tool_name.startswith(_MEMORY_TOOL_PREFIXES) or any(
        marker in tool_name for marker in _MEMORY_TOOL_MARKERS
    )


def _first(payload: dict, *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_hook_payload(harness: str, payload: dict) -> NormalizedEvent | None:
    """One harness hook payload → the normalized vocabulary, or None for
    events the memory lifecycle has no business with."""
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness '{harness}'; expected one of {HARNESSES}")
    original = str(payload.get("hook_event_name") or "")
    session_id = payload.get("session_id")
    session_id = str(session_id) if session_id not in (None, "") else None
    ids = {key: str(payload[key]) for key in _ID_KEYS if payload.get(key)}

    def event(name: str, *, content: str | None = None, **attributes: Any) -> NormalizedEvent:
        return NormalizedEvent(
            name=name, harness=harness, original_event=original,
            session_id=session_id, ids=ids,
            content=content[:_CONTENT_CAP] if content else content,
            attributes=attributes,
        )

    if original == "SessionStart":
        source = str(_first(payload, "source", "how_session_started", "reason") or "startup")
        # resume/compact/fork all continue an existing line of work; only
        # startup and clear open a genuinely new context.
        if source in ("resume", "compact", "fork"):
            return event("context.restored", source=source)
        return event("context.started", source=source)

    if original == "UserPromptSubmit":
        content = _first(payload, "prompt", "user_input") or ""
        return event("input.received", content=str(content))

    if original in ("PostToolUse", "PostToolUseFailure"):
        tool_name = str(payload.get("tool_name") or "")
        if _is_memory_tool(tool_name):
            return None  # recursion guard
        if original == "PostToolUse":
            # Successful tool calls are routine; nothing durable to keep.
            return None
        error = str(payload.get("error") or "")[:_ERROR_CAP]
        return event("tool.finished", tool_name=tool_name, ok=False, error=error)

    if original == "PreCompact":
        trigger = str(_first(payload, "compaction_trigger", "trigger", "reason") or "auto")
        return event("context.compacting", trigger=trigger)

    if original == "PostCompact":
        trigger = str(_first(payload, "compaction_trigger", "trigger", "reason") or "auto")
        return event("context.compacted", trigger=trigger)

    if original == "Stop":
        message = payload.get("last_assistant_message")
        return event("turn.finished", content=str(message) if message else None,
                     stop_reason=payload.get("stop_reason"))

    if original == "SubagentStart":
        return event("context.child_started", agent_type=payload.get("agent_type"))

    if original == "SubagentStop":
        message = payload.get("last_assistant_message")
        return event(
            "context.child_finished",
            content=str(message) if message else None,
            agent_type=payload.get("agent_type"),
        )

    if original == "SessionEnd":
        reason = _first(payload, "reason", "how_session_ended")
        return event("context.closed", reason=reason)

    return None


def captured_content(event: NormalizedEvent) -> str:
    """What a capture stores for this event: its content, or — for events
    that carry none (a Stop without a last message) — the event name and
    attributes. One definition, used by the key AND by the write, so the
    same key always names the same content (Loimi refuses a reused key
    with different content, 409)."""
    return event.content or json.dumps(
        {"event": event.original_event, **event.attributes}, sort_keys=True, default=str,
    )


def event_idempotency_key(event: NormalizedEvent) -> str:
    """Stable per-occurrence key: a redelivered hook never writes twice.

    Harness ids name *where* an event happened, not *which* occurrence it
    is: Claude Code stamps one prompt_id on every hook fired during a
    prompt, so a Stop that fires again after a blocking Stop hook, a
    background notification or a wakeup — or a second subagent in the same
    prompt — would reuse the key with new content, and Loimi refuses that
    with 409 (395 of 405 VPS captures, 2026-09-22). So the key is the most
    specific id plus a digest of what is stored: a redelivery (same
    content) still dedupes, a new occurrence never collides."""
    # Every id goes into the digest, not just the anchor: a resumed
    # subagent keeps its agent_id across prompts, and a content-free
    # PreCompact from a later prompt must not replay-collapse into the first.
    fingerprint = captured_content(event) + json.dumps(
        event.attributes, sort_keys=True, default=str,
    ) + json.dumps(event.ids, sort_keys=True, default=str)
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
    anchor = (
        event.ids.get("tool_use_id")
        or event.ids.get("agent_id")
        or event.ids.get("prompt_id")
        or event.ids.get("turn_id")
    )
    discriminator = f"{anchor}:{digest}" if anchor else digest
    return f"hook:{event.harness}:{event.session_id}:{event.original_event}:{discriminator}"


def harness_output(harness: str, original_event: str, context: str) -> dict:
    """The harness's stdout shape for injected context. Claude Code and
    Codex share `hookSpecificOutput.additionalContext` (both verified
    against their references; Claude Code live-verified 2026-09-15)."""
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness '{harness}'")
    return {
        "hookSpecificOutput": {
            "hookEventName": original_event,
            "additionalContext": context,
        }
    }
