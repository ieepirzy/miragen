"""Native harness hooks → memory lifecycle (§18.7).

One bounded bridge, two serializers: Claude Code and Codex publish
near-isomorphic hook contracts (stdin JSON in, `hookSpecificOutput` out),
verified against the official references on 2026-09-13:
  Claude Code: https://code.claude.com/docs/en/hooks
  Codex:       https://developers.openai.com/codex/hooks

Harness events are normalized to the design doc's vocabulary —
`context.started`, `context.restored`, `input.received`, `tool.finished`,
`context.compacting`, `turn.finished`, `context.closed`,
`context.child_started`, `context.child_finished` — preserving the
original event name and ids. Capture is durable and idempotent (keyed by
harness/session/event/discriminator); context-opening events answer with
the trusted guidance + working-state packet as `additionalContext`.

Trust boundary (§18.7): the bridge takes credentials and identity from the
host environment ONLY — nothing in a hook payload can choose a store
destination or a principal. Capture is fail-open for the harness (a broken
memory service must not block the agent's work) but never silent: failures
land in the lifecycle's degradation counters.

Two transports for the same logic:
- in-process SDK callbacks for the claude-code executor (miragen drives
  the claude-agent-sdk, so Python callables ARE the native seam there);
- the `miragen memory-hook <harness>` CLI for harnesses that run shell
  hooks (Codex via hooks.json, external Claude Code sessions).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("miragen.memory.hooks")

HARNESSES = ("claude-code", "codex")

# Tool names whose events must never re-enter memory capture (§18.7:
# avoid recursion when memory tool calls themselves trigger capture).
_MEMORY_TOOL_PREFIXES = ("memory_",)
_MEMORY_TOOL_MARKERS = ("miragen-memory", "mcp__miragen__")

# Normalized events that get durable capture. Tool successes are
# deliberately NOT captured (routine boilerplate — §17.5); failures are.
_CAPTURED = {
    "input.received",
    "turn.finished",
    "context.closed",
    "context.compacting",
    "context.child_finished",
    "tool.finished",  # only reaches capture when ok=False (see normalize)
}
_CONTEXT_OPENING = {"context.started", "context.restored"}

_CONTENT_CAP = 20_000
_ERROR_CAP = 500


@dataclass
class NormalizedEvent:
    name: str
    harness: str
    original_event: str
    session_id: str | None
    ids: dict[str, str] = field(default_factory=dict)
    content: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


def _is_memory_tool(tool_name: str) -> bool:
    return tool_name.startswith(_MEMORY_TOOL_PREFIXES) or any(
        marker in tool_name for marker in _MEMORY_TOOL_MARKERS
    )


def normalize_hook_payload(harness: str, payload: dict) -> NormalizedEvent | None:
    """One harness hook payload → the normalized vocabulary, or None for
    events the memory lifecycle has no business with."""
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness '{harness}'; expected one of {HARNESSES}")
    original = payload.get("hook_event_name", "")
    session_id = payload.get("session_id")
    ids = {
        key: str(payload[key])
        for key in ("prompt_id", "turn_id", "tool_use_id", "agent_id")
        if payload.get(key)
    }

    def event(name: str, *, content: str | None = None, **attributes: Any) -> NormalizedEvent:
        return NormalizedEvent(
            name=name, harness=harness, original_event=original,
            session_id=session_id, ids=ids,
            content=content[:_CONTENT_CAP] if content else content,
            attributes=attributes,
        )

    if original == "SessionStart":
        # Claude Code calls it `reason`; Codex calls it `source`.
        source = payload.get("reason") or payload.get("source") or "startup"
        if source in ("resume", "compact"):
            return event("context.restored", source=source)
        return event("context.started", source=source)

    if original == "UserPromptSubmit":
        content = payload.get("user_input") or payload.get("prompt") or ""
        return event("input.received", content=content)

    if original in ("PostToolUse", "PostToolUseFailure"):
        tool_name = payload.get("tool_name", "")
        if _is_memory_tool(tool_name):
            return None  # recursion guard
        ok = original == "PostToolUse"
        error = None if ok else str(payload.get("error", ""))[:_ERROR_CAP]
        if ok:
            # Successful tool calls are routine; nothing durable to keep.
            return None
        return event("tool.finished", tool_name=tool_name, ok=False, error=error)

    if original == "PreCompact":
        trigger = payload.get("reason") or payload.get("trigger") or "auto"
        return event("context.compacting", trigger=trigger)

    if original == "Stop":
        return event("turn.finished", content=payload.get("last_assistant_message"))

    if original == "SubagentStart":
        return event("context.child_started", agent_type=payload.get("agent_type"))

    if original == "SubagentStop":
        return event(
            "context.child_finished",
            content=payload.get("last_assistant_message"),
            agent_type=payload.get("agent_type"),
        )

    if original == "SessionEnd":
        return event("context.closed", reason=payload.get("reason"))

    return None


def event_idempotency_key(event: NormalizedEvent) -> str:
    """Stable per-occurrence key: a redelivered hook never writes twice.
    The discriminator prefers harness-supplied ids; content hash is the
    fallback for events that carry neither."""
    discriminator = (
        event.ids.get("tool_use_id")
        or event.ids.get("prompt_id")
        or event.ids.get("turn_id")
        or event.ids.get("agent_id")
        or hashlib.sha256((event.content or "").encode()).hexdigest()[:16]
    )
    return f"hook:{event.harness}:{event.session_id}:{event.original_event}:{discriminator}"


async def handle_hook_event(lifecycle, event: NormalizedEvent, *, instance: str | None) -> dict | None:
    """Run one normalized event through the lifecycle. Returns the hook's
    stdout JSON (context-opening events answer the packet), or None."""
    if event.name in _CONTEXT_OPENING:
        packet = await lifecycle.prepare_context(
            instance=instance,
            run_id=event.session_id,
            trigger=f"hook:{event.original_event.lower()}",
        )
        # additionalContext is the one privileged insertion path a shell
        # hook has; what rides it is OUR fixed trusted wrapper around
        # strictly serialized, source-labeled data — never recalled prose
        # promoted to instructions (§18.7).
        return {
            "hookSpecificOutput": {
                "hookEventName": event.original_event,
                "additionalContext": packet.text,
            }
        }

    if event.name in _CAPTURED:
        await lifecycle.capture_harness_event(instance=instance, event=event)
    return None


# ── Static support detection (the §18.7 honesty gate's input) ────────────────


def sdk_hooks_available() -> bool:
    """Whether the installed claude-agent-sdk exposes the callback-hook
    surface — detected from the actual install, never inferred from docs."""
    try:
        from claude_agent_sdk import HookMatcher  # noqa: F401

        return True
    except ImportError:
        return False


def executor_hook_support(executor_kind: str) -> dict:
    """What native-hook integration each executor kind has TODAY. The
    native_required gate consults this; /health reports it verbatim."""
    if executor_kind == "claude-code":
        if sdk_hooks_available():
            return {"native_hooks": True, "mechanism": "sdk_callbacks"}
        return {
            "native_hooks": False,
            "mechanism": "sdk_callbacks",
            "detail": "claude-agent-sdk not installed or lacks hook support",
        }
    if executor_kind == "codex":
        return {"native_hooks": True, "mechanism": "hooks_json_bridge"}
    return {
        "native_hooks": False,
        "detail": f"no verified hook contract for '{executor_kind}' yet (§18.7: "
        "kimi/grok are next capability-tested targets; status unverified, "
        "not unsupported)",
    }


# ── Claude Code in-process SDK callbacks ─────────────────────────────────────

# Events the in-process path subscribes to. Context injection is NOT here:
# the run_job boundary already carries the packet, so SDK hooks are
# capture-only — subscribing SessionStart too would double-inject.
SDK_CAPTURE_EVENTS = (
    "UserPromptSubmit",
    "PostToolUseFailure",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionEnd",
)


def build_sdk_hook_callables(lifecycle, instance: str | None) -> dict[str, Any]:
    """{event_name: async callable} in claude-agent-sdk's callback shape —
    the adapter wraps each in a HookMatcher when the SDK is present."""

    def make(event_name: str):
        async def on_hook(input_data: dict, tool_use_id=None, context=None) -> dict:
            try:
                payload = dict(input_data or {})
                payload.setdefault("hook_event_name", event_name)
                event = normalize_hook_payload("claude-code", payload)
                if event is not None:
                    await handle_hook_event(lifecycle, event, instance=instance)
            except Exception:
                # Fail-open for the harness; the lifecycle already counted
                # real degradations. A hook must never sink the turn.
                logger.warning("memory hook capture failed", exc_info=True)
            return {}

        return on_hook

    return {name: make(name) for name in SDK_CAPTURE_EVENTS}


# ── Codex hooks.json installation ────────────────────────────────────────────

_OWNED_MARKER = "miragen memory-hook"

# (event, timeout_s) — SessionEnd's max is 3 per the Codex reference.
_CODEX_HOOK_EVENTS = (
    ("SessionStart", 10),
    ("UserPromptSubmit", 10),
    ("PreCompact", 10),
    ("Stop", 10),
    ("SubagentStop", 10),
    ("SessionEnd", 3),
)


def install_codex_hooks(hooks_path: Path) -> None:
    """Install/refresh miragen's hook entries in a Codex hooks.json,
    merging: only entries carrying our command marker are owned (removed
    and re-added); every user entry is preserved verbatim (§18.7)."""
    try:
        existing = json.loads(hooks_path.read_text())
        if not isinstance(existing, dict):
            existing = {}
    except FileNotFoundError:
        existing = {}
    except ValueError:
        # A hooks.json we cannot parse is the user's; refusing to touch it
        # beats silently rewriting their configuration.
        raise RuntimeError(
            f"{hooks_path} exists but is not valid JSON — fix or remove it "
            "before miragen can install its memory hooks"
        ) from None

    hooks = existing.setdefault("hooks", {})
    for event_name, timeout in _CODEX_HOOK_EVENTS:
        groups = [
            group for group in hooks.get(event_name, [])
            if not _group_is_owned(group)
        ]
        groups.append({
            "hooks": [{
                "type": "command",
                "command": f"{_OWNED_MARKER} codex",
                "timeout": timeout,
                "statusMessage": "miragen memory",
            }]
        })
        hooks[event_name] = groups

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(existing, indent=2) + "\n")


def _group_is_owned(group: dict) -> bool:
    return any(
        _OWNED_MARKER in str(hook.get("command", ""))
        for hook in group.get("hooks", [])
        if isinstance(hook, dict)
    )
