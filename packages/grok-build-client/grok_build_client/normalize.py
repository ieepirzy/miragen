"""Map CLI/ACP payloads onto a small stable event union."""

from __future__ import annotations

from typing import Any


def normalize_headless_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize one Grok headless streaming-json / json object."""
    etype = event.get("type")

    if etype == "text":
        data = event.get("data") or ""
        return [{"type": "text", "data": data, "raw": event}] if data else []

    if etype == "thought":
        data = event.get("data") or ""
        return [{"type": "thought", "data": data, "raw": event}] if data else []

    if etype == "end":
        out: dict[str, Any] = {
            "type": "end",
            "sessionId": event.get("sessionId"),
            "stopReason": event.get("stopReason"),
            "usage": event.get("usage") if isinstance(event.get("usage"), dict) else {},
            "raw": event,
        }
        return [out]

    if etype == "error":
        return [{
            "type": "error",
            "message": event.get("message") or "grok reported an error",
            "raw": event,
        }]

    # Non-streaming json result (no type field)
    if etype is None and ("text" in event or "sessionId" in event):
        events: list[dict[str, Any]] = []
        if event.get("text"):
            events.append({"type": "text", "data": event["text"], "raw": event})
        events.append({
            "type": "end",
            "sessionId": event.get("sessionId"),
            "stopReason": event.get("stopReason"),
            "usage": event.get("usage") if isinstance(event.get("usage"), dict) else {},
            "raw": event,
        })
        return events

    return []


def normalize_acp_update(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize one ACP `session/update` notification params object."""
    update = params.get("update") if isinstance(params.get("update"), dict) else params
    kind = update.get("sessionUpdate") or update.get("session_update")
    content = update.get("content") if isinstance(update.get("content"), dict) else {}

    if kind == "agent_message_chunk":
        text = content.get("text") or update.get("text") or ""
        return [{"type": "text", "data": text, "raw": params}] if text else []

    if kind == "agent_thought_chunk":
        text = content.get("text") or update.get("text") or ""
        return [{"type": "thought", "data": text, "raw": params}] if text else []

    if kind == "tool_call":
        return [{
            "type": "tool_call",
            "name": update.get("title") or update.get("kind") or update.get("toolName"),
            "status": update.get("status"),
            "raw": params,
        }]

    if kind == "tool_call_update":
        return [{
            "type": "tool_call",
            "name": update.get("title") or update.get("toolName"),
            "status": update.get("status"),
            "raw": params,
        }]

    return []
