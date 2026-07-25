"""Unit tests for grok-build-client (no live grok required)."""

from __future__ import annotations

from grok_build_client import build_headless_argv, normalize_acp_update, normalize_headless_event


def test_normalize_headless_text_end_error():
    assert normalize_headless_event({"type": "text", "data": "hi"})[0]["type"] == "text"
    end = normalize_headless_event({
        "type": "end",
        "sessionId": "s",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })[0]
    assert end["type"] == "end" and end["sessionId"] == "s"
    assert normalize_headless_event({"type": "error", "message": "x"})[0]["message"] == "x"


def test_normalize_acp_message_chunk():
    events = normalize_acp_update({
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "hello"},
        }
    })
    assert events == [{"type": "text", "data": "hello", "raw": events[0]["raw"]}]


def test_build_headless_argv_resume_and_flags():
    argv = build_headless_argv(
        "grok", "do it", cwd="/ws", session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        resume=False, always_approve=True, web_search=False,
    )
    assert "-s" in argv and "--always-approve" in argv and "--disable-web-search" in argv
    argv_r = build_headless_argv(
        "grok", "more", cwd="/ws", session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        resume=True, always_approve=False, web_search=True,
    )
    assert "-r" in argv_r and "--always-approve" not in argv_r
