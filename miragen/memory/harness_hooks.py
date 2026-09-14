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

import logging
from typing import Any

# The serializers, idempotency keys and installers live in the stdlib-only
# adapter package (miragen_hook) so external hook invocations never import
# the agent runtime; they are re-exported here for the in-process paths.
from miragen_hook.install import install_codex_hooks  # noqa: F401
from miragen_hook.normalize import (  # noqa: F401
    CAPTURED as _CAPTURED,
    CONTEXT_OPENING as _CONTEXT_OPENING,
    HARNESSES,
    NormalizedEvent,
    event_idempotency_key,
    normalize_hook_payload,
)

logger = logging.getLogger("miragen.memory.hooks")


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
