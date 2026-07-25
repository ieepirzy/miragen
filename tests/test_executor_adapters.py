"""Executor tier refinement — factory dispatch, wall-clock timeout, the
claude-code and spawn adapters, and the artifact sink.

Design record: docs/design/executor-tier-refinement.md. The Claude SDK is
stubbed via ClaudeCodeExecutor's query_factory seam and plain stand-in message
classes; SpawnExecutor runs real subprocesses; LoimiSink runs against an
httpx.MockTransport speaking just enough MCP streamable-HTTP.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import miragen.app  # ensure module is registered in sys.modules

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.executor import CodexExecutor, ExecutorBackend, build_executor
from miragen.executor.claude_code import ClaudeCodeExecutor
from miragen.executor.sink import LoimiSink
from miragen.executor.spawn import SpawnExecutor
from miragen.models import AgentProfile, ArtifactSinkSpec
from miragen.runs import RunStore

from tests.test_executor import StubThread, default_events


def _profile(executor_body: dict, **kw) -> AgentProfile:
    return AgentProfile.model_validate({
        "name": "adapter-worker",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "executor": {"instructions": "hi.", **executor_body},
        **kw,
    })


def _paths(profile: AgentProfile, tmp_path) -> Path:
    profile.executor.workspace_root = str(tmp_path / "workspaces")
    profile.executor.codex_home = str(tmp_path / "codex-home")
    return tmp_path / "runs"


# ── Factory + spec validation ─────────────────────────────────────────────────


def test_factory_dispatches_on_executor_kind(tmp_path):
    from miragen.executor.grok_build import GrokBuildExecutor
    from miragen.executor.kimi_code import KimiCodeExecutor

    cases = {
        "codex": ({"executor": "codex"}, CodexExecutor),
        "claude-code": ({"executor": "claude-code"}, ClaudeCodeExecutor),
        "kimi-code": ({"executor": "kimi-code"}, KimiCodeExecutor),
        "grok-build": ({"executor": "grok-build"}, GrokBuildExecutor),
        "spawn": ({"executor": "spawn", "command": ["/bin/true"]}, SpawnExecutor),
    }
    for kind, (body, cls) in cases.items():
        backend = build_executor(_profile(body), runs_root=tmp_path / "runs")
        assert type(backend) is cls, kind
        assert isinstance(backend, ExecutorBackend)


def test_kimi_home_defaults_and_rejects_on_other_backends():
    p = _profile({"executor": "kimi-code"})
    assert p.executor.kimi_home == "/agent/kimi-home"
    with pytest.raises(ValidationError, match="kimi_home"):
        _profile({"executor": "codex", "kimi_home": "/tmp/kimi"})


def test_grok_home_defaults_and_rejects_on_other_backends():
    p = _profile({"executor": "grok-build"})
    assert p.executor.grok_home == "/agent/grok-home"
    with pytest.raises(ValidationError, match="grok_home"):
        _profile({"executor": "codex", "grok_home": "/tmp/grok"})


def test_spawn_requires_command():
    with pytest.raises(ValidationError, match="requires `command`"):
        _profile({"executor": "spawn"})


def test_command_rejected_on_non_spawn_backends():
    with pytest.raises(ValidationError, match="`command` only applies"):
        _profile({"executor": "codex", "command": ["/bin/true"]})


def test_spawn_rejects_mcp_servers():
    with pytest.raises(ValidationError, match="cannot inject"):
        _profile({
            "executor": "spawn",
            "command": ["/bin/true"],
            "mcp_servers": [{"name": "loimi", "url": "https://loimi.mesh/mcp/"}],
        })


def test_turn_timeout_default_is_finite():
    assert _profile({"executor": "codex"}).executor.turn_timeout_s == 1800


# ── App wiring: wall-clock timeout ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._executor = None


def hanging_session(handle="thr_hang"):
    """A session_factory that emits its resume handle, then wedges — the case
    turn_timeout_s exists for. The handle is persisted (and thus recoverable)
    before the wedge."""
    def _factory(prompt, *, thread_id=None, first_turn=True, options=None):
        async def gen():
            yield {"type": "thread.started", "thread_id": handle}
            await asyncio.sleep(30)
        return gen()
    return _factory


async def test_turn_timeout_suspends_run_resumably(tmp_path):
    profile = _profile({"executor": "codex", "turn_timeout_s": 1})
    runs_root = _paths(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = CodexExecutor(
        profile, runs_root=runs_root, session_factory=hanging_session("thr_hang")
    )
    app_module._run_store = RunStore(root=runs_root, retention=50)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "wedge"})
        assert resp.status_code == 200, resp.text
        assert "[suspended: timeout]" in resp.json()["output"]
        run_id = resp.json()["run_id"]

        record = app_module._run_store.get(run_id)
        assert record.status == "suspended"
        assert record.exit_reason == "timeout"
        # resume handle recovered from the persisted event stream — the turn
        # was cancelled before run_job could return a result carrying it
        assert record.thread_id == "thr_hang"
        assert record.diff_path is None  # no harvest on timeout

        # suspended + thread handle = resumable; give it a working thread
        app_module._executor._session_factory = StubThread(default_events())
        resp = await client.post(f"/runs/{run_id}/resume", json={"prompt": "unwedge"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "succeeded"


async def test_no_timeout_when_disabled(tmp_path):
    profile = _profile({"executor": "codex", "turn_timeout_s": None})
    runs_root = _paths(profile, tmp_path)
    executor = CodexExecutor(
        profile, runs_root=runs_root, session_factory=StubThread(default_events())
    )
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = RunStore(root=runs_root, retention=50)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "fine"})
        assert resp.status_code == 200
        assert app_module._run_store.get(resp.json()["run_id"]).status == "succeeded"


async def test_timeout_after_thread_started_still_recovers_thread_id(tmp_path):
    """The resume handle is emitted before the turn streams, so a turn that
    wedges immediately after still leaves a recoverable thread_id in the
    persisted event stream."""
    profile = _profile({"executor": "codex"})
    runs_root = _paths(profile, tmp_path)
    executor = CodexExecutor(
        profile, runs_root=runs_root, session_factory=hanging_session("thr_early")
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(executor.run_job("wedge", "run-early"), timeout=0.3)
    assert executor.latest_thread_id("run-early") == "thr_early"


def test_latest_thread_id_survives_long_event_streams(tmp_path):
    """Regression for Codex review: thread.started is typically the FIRST
    event; a tail-window scan loses it once a long turn pushes it out."""
    profile = _profile({"executor": "codex"})
    runs_root = _paths(profile, tmp_path)
    executor = CodexExecutor(profile, runs_root=runs_root, session_factory=None)

    events_path = executor._events_path("run-long")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("w") as f:
        f.write(json.dumps({"type": "thread.started", "thread_id": "thr_first"}) + "\n")
        for i in range(1500):
            f.write(json.dumps({"type": "item.completed", "item": {"type": "stdout", "text": str(i)}}) + "\n")
    assert executor.latest_thread_id("run-long") == "thr_first"


# ── Codex App Server SDK notification mapping (issue #38) ─────────────────────
#
# The stub session_factory yields already-normalized dicts (which pass
# through), so these exercise `_normalize_notification` against stand-in
# classes matched by name — the real SDK-type → base-payload mapping.


class _StubItem:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class ItemCompletedNotification:
    def __init__(self, item):
        self.item = item


class TurnCompletedNotification:
    def __init__(self, turn):
        self.turn = turn


class ThreadTokenUsageUpdatedNotification:
    def __init__(self, token_usage=None):
        self.token_usage = token_usage


def test_normalize_agent_message_item():
    from miragen.executor.codex import _normalize_notification

    note = ItemCompletedNotification(_StubItem(type="agentMessage", text="all done", phase=None))
    assert _normalize_notification(note) == [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "all done"}}
    ]


def test_normalize_command_item_preserves_command_and_exit():
    from miragen.executor.codex import _normalize_notification

    note = ItemCompletedNotification(
        _StubItem(type="commandExecution", command="rm -rf build", status="failed", exit_code=1)
    )
    (payload,) = _normalize_notification(note)
    assert payload["item"] == {
        "type": "command_execution", "command": "rm -rf build", "status": "failed", "exit_code": 1,
    }


def test_normalize_turn_completed_maps_usage_from_last_breakdown():
    from miragen.executor.codex import _normalize_notification

    breakdown = _StubItem(input_tokens=100, output_tokens=50, cached_input_tokens=80)
    turn = _StubItem(status="completed", usage=_StubItem(last=breakdown), error=None)
    (payload,) = _normalize_notification(TurnCompletedNotification(turn))
    assert payload["type"] == "turn.completed"
    assert payload["usage"] == {"input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 80}


def test_normalize_turn_failed():
    from miragen.executor.codex import _normalize_notification

    turn = _StubItem(status="failed", error=_StubItem(message="boom"), usage=None)
    assert _normalize_notification(TurnCompletedNotification(turn)) == [
        {"type": "turn.failed", "error": {"message": "boom"}}
    ]


def test_normalize_interim_usage_update_is_dropped():
    from miragen.executor.codex import _normalize_notification

    assert _normalize_notification(ThreadTokenUsageUpdatedNotification()) == []


def test_normalize_passes_plain_dicts_through():
    from miragen.executor.codex import _normalize_notification

    d = {"type": "thread.started", "thread_id": "t"}
    assert _normalize_notification(d) == [d]


# ── ClaudeCodeExecutor ────────────────────────────────────────────────────────
#
# Stand-in classes: the adapter matches SDK messages by class NAME, so these
# plain fakes exercise the real normalization path.


class SystemMessage:
    def __init__(self, subtype, data):
        self.subtype, self.data = subtype, data


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, name):
        self.name = name


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, *, session_id=None, is_error=False, usage=None, result=None):
        self.session_id = session_id
        self.is_error = is_error
        self.usage = usage
        self.result = result


def _claude_messages(session_id="sess_abc"):
    return [
        SystemMessage("init", {"session_id": session_id}),
        AssistantMessage([ToolUseBlock("Bash"), TextBlock("patched the bug")]),
        ResultMessage(session_id=session_id, usage={"input_tokens": 70, "output_tokens": 30}),
    ]


def _claude_executor(tmp_path, messages=None, executor_body=None, captured=None):
    profile = _profile({"executor": "claude-code", **(executor_body or {})})
    runs_root = _paths(profile, tmp_path)

    def factory(prompt, options):
        if captured is not None:
            captured.append((prompt, options))

        async def gen():
            for m in messages if messages is not None else _claude_messages():
                yield m

        return gen()

    return profile, ClaudeCodeExecutor(profile, runs_root=runs_root, query_factory=factory)


async def test_claude_code_success_harvests_diff(tmp_path):
    captured = []
    profile, executor = _claude_executor(tmp_path, captured=captured)

    run_id = "cc-run1"
    ws = Path(profile.executor.workspace_root) / run_id
    executor._prepare_workspace(ws)
    (ws / "fix.py").write_text("print('fixed')\n")

    result = await executor.run_job("fix the bug", run_id)
    assert result.status == "succeeded"
    assert result.thread_id == "sess_abc"
    assert result.output == "patched the bug"
    assert result.usage.input_tokens == 70 and result.usage.output_tokens == 30
    assert "+print('fixed')" in Path(result.diff_path).read_text()

    prompt, options = captured[0]
    assert prompt.startswith("hi.")  # instructions prepended on first turn
    assert options["cwd"] == str(ws)
    assert options["permission_mode"] == "bypassPermissions"  # never -> unattended-safe
    assert options["resume"] is None


async def test_claude_code_resume_passes_session_id(tmp_path):
    captured = []
    _, executor = _claude_executor(tmp_path, captured=captured)
    await executor.run_job("continue", "cc-run2", thread_id="sess_abc", first_turn=False)
    prompt, options = captured[0]
    assert prompt == "continue"
    assert options["resume"] == "sess_abc"


async def test_claude_code_error_result_is_resumable_crash(tmp_path):
    messages = [
        SystemMessage("init", {"session_id": "sess_err"}),
        ResultMessage(session_id="sess_err", is_error=True, result="credit exhausted"),
    ]
    _, executor = _claude_executor(tmp_path, messages=messages)
    result = await executor.run_job("try", "cc-run3")
    assert result.status == "failed"
    assert result.exit_reason == "crash"
    assert "credit exhausted" in result.error
    assert result.thread_id == "sess_err"  # resume handle survives the failure
    assert result.diff_path is None


async def test_claude_code_injects_mcp_servers_with_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("LOIMI_TOKEN", "tok-123")
    captured = []
    _, executor = _claude_executor(
        tmp_path,
        executor_body={
            "mcp_servers": [
                {"name": "loimi", "url": "https://loimi.mesh/mcp/", "bearer_token_env": "LOIMI_TOKEN"}
            ]
        },
        captured=captured,
    )
    await executor.run_job("go", "cc-run4")
    servers = captured[0][1]["mcp_servers"]
    assert servers["loimi"]["type"] == "http"
    assert servers["loimi"]["url"] == "https://loimi.mesh/mcp/"
    assert servers["loimi"]["headers"]["Authorization"] == "Bearer tok-123"


# ── KimiCodeExecutor ──────────────────────────────────────────────────────────
#
# Stand-in Wire types: adapter matches by class NAME (and ApprovalRequest by
# name in the production path). session_factory yields messages + synthetic
# thread.started from the factory itself for resume-handle tests.


class TextPart:
    def __init__(self, text):
        self.text = text


class _Fn:
    def __init__(self, name):
        self.name = name


class ToolCall:
    def __init__(self, name):
        self.function = _Fn(name)


class _TokenUsage:
    def __init__(self, input_other, output, input_cache_read=0):
        self.input_other = input_other
        self.output = output
        self.input_cache_read = input_cache_read


class StatusUpdate:
    def __init__(self, token_usage=None):
        self.token_usage = token_usage


class TurnEnd:
    pass


def _kimi_messages(session_id="kimi_sess"):
    # thread.started is emitted by the factory (mirrors production), not by Wire.
    return [
        {"type": "thread.started", "thread_id": session_id},
        ToolCall("Bash"),
        TextPart("patched the bug"),
        StatusUpdate(_TokenUsage(90, 40, input_cache_read=10)),
        TurnEnd(),
        {"type": "turn.completed", "usage": {"input_tokens": 90, "output_tokens": 40, "cached_input_tokens": 10}},
    ]


def _kimi_executor(tmp_path, messages=None, executor_body=None, captured=None):
    from miragen.executor.kimi_code import KimiCodeExecutor

    profile = _profile({"executor": "kimi-code", **(executor_body or {})})
    profile.executor.kimi_home = str(tmp_path / "kimi-home")
    runs_root = _paths(profile, tmp_path)

    def factory(prompt, *, thread_id=None, first_turn=True, options=None):
        if captured is not None:
            captured.append({
                "prompt": prompt,
                "thread_id": thread_id,
                "first_turn": first_turn,
                "options": options,
            })

        async def gen():
            for m in messages if messages is not None else _kimi_messages():
                yield m

        return gen()

    return profile, KimiCodeExecutor(profile, runs_root=runs_root, session_factory=factory)


def test_normalize_kimi_text_and_tool_and_usage():
    from miragen.executor.kimi_code import _normalize

    assert _normalize(TextPart("hi")) == [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}
    ]
    assert _normalize(ToolCall("Shell")) == [
        {"type": "item.completed", "item": {"type": "tool_use", "name": "Shell"}}
    ]
    assert _normalize(StatusUpdate(_TokenUsage(1, 2, 3))) == [
        {"type": "turn.completed", "usage": {
            "input_tokens": 1, "output_tokens": 2, "cached_input_tokens": 3,
        }}
    ]
    assert _normalize(TurnEnd()) == []
    assert _normalize({"type": "thread.started", "thread_id": "x"}) == [
        {"type": "thread.started", "thread_id": "x"}
    ]


async def test_kimi_code_success_harvests_diff(tmp_path):
    captured = []
    profile, executor = _kimi_executor(tmp_path, captured=captured)

    run_id = "kc-run1"
    ws = Path(profile.executor.workspace_root) / run_id
    executor._prepare_workspace(ws)
    (ws / "fix.py").write_text("print('fixed')\n")

    result = await executor.run_job("fix the bug", run_id)
    assert result.status == "succeeded"
    assert result.thread_id == "kimi_sess"
    assert result.output == "patched the bug"
    assert result.usage.input_tokens == 90 and result.usage.output_tokens == 40
    assert "+print('fixed')" in Path(result.diff_path).read_text()

    call = captured[0]
    assert call["prompt"].startswith("hi.")
    assert call["options"]["work_dir"] == str(ws)
    assert call["options"]["yolo"] is True  # approval_policy never, no leash
    assert call["thread_id"] is None


async def test_kimi_code_resume_passes_session_id(tmp_path):
    captured = []
    _, executor = _kimi_executor(tmp_path, captured=captured)
    await executor.run_job("continue", "kc-run2", thread_id="kimi_sess", first_turn=False)
    call = captured[0]
    assert call["prompt"] == "continue"
    assert call["thread_id"] == "kimi_sess"
    assert call["first_turn"] is False


async def test_kimi_code_leash_disables_yolo(tmp_path):
    captured = []
    _, executor = _kimi_executor(
        tmp_path,
        executor_body={"leash": {"gate": ["command"]}},
        captured=captured,
    )
    await executor.run_job("go", "kc-leash")
    assert captured[0]["options"]["yolo"] is False


async def test_kimi_code_injects_mcp_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("LOIMI_TOKEN", "tok-kimi")
    captured = []
    _, executor = _kimi_executor(
        tmp_path,
        executor_body={
            "mcp_servers": [
                {"name": "loimi", "url": "https://loimi.mesh/mcp/", "bearer_token_env": "LOIMI_TOKEN"}
            ]
        },
        captured=captured,
    )
    await executor.run_job("go", "kc-mcp")
    configs = captured[0]["options"]["mcp_configs"]
    assert configs[0]["name"] == "loimi"
    assert configs[0]["url"] == "https://loimi.mesh/mcp/"
    assert configs[0]["headers"]["Authorization"] == "Bearer tok-kimi"


async def test_kimi_code_prepare_sets_kimi_code_home(tmp_path, monkeypatch):
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    profile, executor = _kimi_executor(tmp_path)
    executor.prepare()
    assert os.environ["KIMI_CODE_HOME"] == profile.executor.kimi_home
    assert Path(profile.executor.kimi_home).is_dir()


async def test_kimi_code_prepare_overrides_inherited_kimi_code_home(tmp_path, monkeypatch):
    """Profile kimi_home must win over a pre-set env (not setdefault)."""
    monkeypatch.setenv("KIMI_CODE_HOME", "/wrong/inherited/home")
    profile, executor = _kimi_executor(tmp_path)
    executor.prepare()
    assert os.environ["KIMI_CODE_HOME"] == profile.executor.kimi_home
    assert os.environ["KIMI_CODE_HOME"] != "/wrong/inherited/home"


async def test_kimi_code_product_cancel_is_resumable_failure(tmp_path):
    """Product-side RunCancelled must not become CancelledError (leaves run
    stuck at running). Stand-in: a factory that yields thread.started then a
    synthetic cancel as turn.failed — production maps RunCancelled the same way."""
    messages = [
        {"type": "thread.started", "thread_id": "kimi_cancel"},
        {"type": "turn.failed", "error": {"message": "kimi run cancelled: product"}},
    ]
    _, executor = _kimi_executor(tmp_path, messages=messages)
    result = await executor.run_job("go", "kc-cancel")
    assert result.status == "failed"
    assert result.exit_reason == "crash"
    assert result.thread_id == "kimi_cancel"
    assert "cancelled" in (result.error or "").lower()


# ── GrokBuildExecutor (Phase A headless) ──────────────────────────────────────


def _grok_stream_events(session_id="11111111-1111-1111-1111-111111111111"):
    """CLI streaming-json event sequence (not yet base-normalized)."""
    return [
        {"type": "text", "data": "patched "},
        {"type": "text", "data": "the bug"},
        {"type": "thought", "data": "done"},
        {
            "type": "end",
            "stopReason": "EndTurn",
            "sessionId": session_id,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_read_input_tokens": 20,
            },
        },
    ]


def _grok_executor(tmp_path, events=None, executor_body=None, captured=None):
    from miragen.executor.grok_build import GrokBuildExecutor

    profile = _profile({"executor": "grok-build", **(executor_body or {})})
    profile.executor.grok_home = str(tmp_path / "grok-home")
    runs_root = _paths(profile, tmp_path)

    def factory(prompt, *, thread_id=None, first_turn=True, options=None):
        if captured is not None:
            captured.append({
                "prompt": prompt,
                "thread_id": thread_id,
                "first_turn": first_turn,
                "options": options,
            })

        async def gen():
            for e in events if events is not None else _grok_stream_events():
                yield e

        return gen()

    return profile, GrokBuildExecutor(profile, runs_root=runs_root, session_factory=factory)


def test_normalize_grok_streaming_events():
    from miragen.executor.grok_build import _normalize_event

    assert _normalize_event({"type": "text", "data": "hi"}) == [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}
    ]
    assert _normalize_event({"type": "thought", "data": "hmm"}) == [
        {"type": "item.completed", "item": {"type": "reasoning", "text": "hmm"}}
    ]
    end = _normalize_event({
        "type": "end",
        "sessionId": "s1",
        "usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3},
    })
    assert end[0] == {"type": "thread.started", "thread_id": "s1"}
    assert end[1]["type"] == "turn.completed"
    assert end[1]["usage"] == {
        "input_tokens": 1, "output_tokens": 2, "cached_input_tokens": 3,
    }
    assert _normalize_event({"type": "error", "message": "boom"}) == [
        {"type": "turn.failed", "error": {"message": "boom"}}
    ]


def test_build_argv_new_vs_resume():
    from miragen.executor.grok_build import _build_argv

    new_opts = {
        "cwd": "/ws", "session_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "resume": False, "always_approve": True, "model": None,
        "reasoning_effort": None, "web_search": False,
    }
    argv = _build_argv("grok", "do it", new_opts)
    assert "-s" in argv and "-r" not in argv
    assert "--always-approve" in argv
    assert "--disable-web-search" in argv
    assert "--output-format" in argv and "streaming-json" in argv

    resume_opts = {**new_opts, "resume": True, "always_approve": False, "web_search": True}
    argv_r = _build_argv("grok", "more", resume_opts)
    assert "-r" in argv_r and "-s" not in argv_r
    assert "--always-approve" not in argv_r
    assert "--disable-web-search" not in argv_r


async def test_grok_build_success_harvests_diff(tmp_path):
    captured = []
    profile, executor = _grok_executor(tmp_path, captured=captured)

    run_id = "gb-run1"
    ws = Path(profile.executor.workspace_root) / run_id
    executor._prepare_workspace(ws)
    (ws / "fix.py").write_text("print('fixed')\n")

    result = await executor.run_job("fix the bug", run_id)
    assert result.status == "succeeded"
    assert result.thread_id is not None  # minted UUID
    assert result.output == "patched the bug"
    assert result.usage.input_tokens == 100 and result.usage.output_tokens == 40
    assert result.usage.cached_input_tokens == 20
    assert "+print('fixed')" in Path(result.diff_path).read_text()

    call = captured[0]
    assert call["prompt"].startswith("hi.")
    assert call["options"]["cwd"] == str(ws)
    assert call["options"]["always_approve"] is True
    assert call["options"]["resume"] is False
    assert call["first_turn"] is True


async def test_grok_build_resume_uses_session_id(tmp_path):
    captured = []
    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _, executor = _grok_executor(tmp_path, captured=captured)
    await executor.run_job("continue", "gb-run2", thread_id=sid, first_turn=False)
    call = captured[0]
    assert call["prompt"] == "continue"
    assert call["options"]["session_id"] == sid
    assert call["options"]["resume"] is True
    assert call["first_turn"] is False


async def test_grok_build_error_is_resumable_crash(tmp_path):
    events = [{"type": "error", "message": "auth failed"}]
    profile, executor = _grok_executor(tmp_path, events=events)
    result = await executor.run_job("go", "gb-err")
    assert result.status == "failed"
    assert result.exit_reason == "crash"
    assert "auth failed" in result.error
    assert result.thread_id is not None  # still minted before stream
    assert result.diff_path is None


def test_grok_build_leash_requires_acp_transport():
    with pytest.raises(ValidationError, match="grok_transport: acp"):
        _profile({"executor": "grok-build", "leash": {"gate": ["command"]}})


async def test_grok_build_acp_transport_options(tmp_path):
    captured = []
    _, executor = _grok_executor(
        tmp_path,
        executor_body={"grok_transport": "acp"},
        captured=captured,
        events=[
            {"type": "session", "sessionId": "acp-sess-1"},
            {"type": "text", "data": "hi"},
            {"type": "end", "sessionId": "acp-sess-1", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ],
    )
    result = await executor.run_job("go", "gb-acp")
    assert result.status == "succeeded"
    assert captured[0]["options"]["transport"] == "acp"
    assert result.thread_id == "acp-sess-1"
    assert result.output == "hi"


async def test_grok_build_no_terminal_event_fails(tmp_path):
    """Empty / non-JSON stream must not harvest as success."""
    _, executor = _grok_executor(tmp_path, events=[])
    result = await executor.run_job("go", "gb-empty")
    assert result.status == "failed"
    assert "terminal" in (result.error or "").lower()
    assert result.diff_path is None


def test_codex_prepare_overrides_inherited_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/wrong/inherited")
    profile = _profile({"executor": "codex"})
    profile.executor.codex_home = str(tmp_path / "codex-home")
    profile.executor.workspace_root = str(tmp_path / "workspaces")
    from miragen.executor.codex import CodexExecutor

    CodexExecutor(profile, runs_root=tmp_path / "runs", session_factory=None).prepare()
    assert os.environ["CODEX_HOME"] == profile.executor.codex_home



async def test_grok_build_prepare_overrides_inherited_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", "/wrong/inherited")
    profile, executor = _grok_executor(tmp_path)
    executor.prepare()
    assert os.environ["GROK_HOME"] == profile.executor.grok_home
    assert (Path(profile.executor.grok_home) / "config.toml").exists()


# ── SpawnExecutor ─────────────────────────────────────────────────────────────


def _spawn_executor(tmp_path, command):
    profile = _profile({"executor": "spawn", "command": command})
    runs_root = _paths(profile, tmp_path)
    return profile, SpawnExecutor(profile, runs_root=runs_root)


async def test_spawn_success_harvests_diff_and_keeps_stdout(tmp_path):
    _, executor = _spawn_executor(
        tmp_path, ["/bin/sh", "-c", "echo did-it && echo content > produced.txt"]
    )
    result = await executor.run_job("make a file", "sp-run1")
    assert result.status == "succeeded"
    assert result.thread_id is None  # no resume handle, by design
    assert result.usage is None  # bare CLIs report no usage
    assert "did-it" in result.output
    assert "produced.txt" in Path(result.diff_path).read_text()

    kinds = [e["type"] for e in executor.read_events("sp-run1")]
    assert "item.completed" in kinds and "turn.completed" in kinds
    assert kinds[-1] == "lifecycle.harvest.completed"


async def test_spawn_nonzero_exit_is_resumable_crash(tmp_path):
    # Consume stdin before failing so a fast-exit shell does not race the
    # prompt write (BrokenPipe / Connection lost on drain).
    _, executor = _spawn_executor(
        tmp_path, ["/bin/sh", "-c", "cat >/dev/null; echo boom >&2; exit 3"]
    )
    result = await executor.run_job("doomed", "sp-run2")
    assert result.status == "failed"
    assert result.exit_reason == "crash"
    assert "exited with code 3" in result.error
    assert "boom" in result.error  # stderr tail captured (merged into stdout)
    assert result.diff_path is None


async def test_spawn_prompt_via_stdin_when_no_placeholder(tmp_path):
    profile, executor = _spawn_executor(tmp_path, ["/bin/sh", "-c", "cat > from-stdin.txt"])
    result = await executor.run_job("the actual task", "sp-run3")
    assert result.status == "succeeded"
    diff = Path(result.diff_path).read_text()
    assert "from-stdin.txt" in diff and "the actual task" in diff


async def test_spawn_prompt_placeholder_substitution(tmp_path):
    _, executor = _spawn_executor(tmp_path, ["/bin/echo", "{prompt}"])
    result = await executor.run_job("just this", "sp-run4", first_turn=False)
    assert result.status == "succeeded"
    assert result.output.strip() == "just this"


async def test_spawn_cancellation_kills_the_whole_process_tree(tmp_path):
    """Regression for Codex review: killing only the wrapper leaves grand-
    children alive to keep mutating a workspace that resume/abandon assumes
    is quiescent. The backgrounded subshell here would touch the file ~1s
    after cancellation if it survived the group kill."""
    profile, executor = _spawn_executor(
        tmp_path, ["/bin/sh", "-c", "(sleep 1; touch child-escaped.txt) & sleep 30"]
    )
    run_id = "sp-cancel"
    task = asyncio.create_task(executor.run_job("wedge", run_id))
    await asyncio.sleep(0.3)  # let the shell start and background its child
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(1.2)  # past the child's sleep — it must be dead by now
    ws = Path(profile.executor.workspace_root) / run_id
    assert not (ws / "child-escaped.txt").exists()


# ── Artifact sink ─────────────────────────────────────────────────────────────


def _mcp_transport(calls, *, store_response=None, sse=False):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"headers": dict(request.headers), "body": body})
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "loimi"}}},
                headers={"mcp-session-id": "sess-42"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            payload = store_response or {
                "jsonrpc": "2.0", "id": 2,
                "result": {"content": [{"type": "text", "text": "stored doc_1"}]},
            }
            if sse:
                return httpx.Response(
                    200,
                    text=f"event: message\ndata: {json.dumps(payload)}\n\n",
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, json=payload)
        raise AssertionError(f"unexpected method {method}")

    return httpx.MockTransport(handler)


def _sink_spec(**kw) -> ArtifactSinkSpec:
    return ArtifactSinkSpec.model_validate({"url": "https://loimi.mesh/mcp/", **kw})


async def test_loimi_sink_calls_store_document():
    calls = []
    sink = LoimiSink(_sink_spec(), bearer_token="tok-9", transport=_mcp_transport(calls))
    await sink.store(diff="diff --git a/x b/x", metadata={"run_id": "r1", "agent": "a"})

    methods = [c["body"].get("method") for c in calls]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    # session id from initialize carried on subsequent calls; bearer on all
    assert calls[2]["headers"]["mcp-session-id"] == "sess-42"
    assert calls[2]["headers"]["authorization"] == "Bearer tok-9"

    args = calls[2]["body"]["params"]
    assert args["name"] == "store_document"
    assert args["arguments"]["kind"] == "executor_diff"
    assert args["arguments"]["content"] == "diff --git a/x b/x"
    assert args["arguments"]["metadata"]["run_id"] == "r1"


async def test_loimi_sink_parses_sse_responses():
    calls = []
    sink = LoimiSink(_sink_spec(), transport=_mcp_transport(calls, sse=True))
    await sink.store(diff="d", metadata={})  # no exception = parsed the SSE frame


async def test_loimi_sink_raises_on_tool_error():
    calls = []
    error_response = {
        "jsonrpc": "2.0", "id": 2,
        "result": {"isError": True, "content": [{"type": "text", "text": "schema mismatch"}]},
    }
    sink = LoimiSink(_sink_spec(), transport=_mcp_transport(calls, store_response=error_response))
    with pytest.raises(RuntimeError, match="store_document returned an error"):
        await sink.store(diff="d", metadata={})


class _StubSink:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.stored = []

    async def store(self, *, diff, metadata):
        if self.fail:
            raise RuntimeError("sink unreachable")
        self.stored.append((diff, metadata))


async def test_success_does_not_auto_publish_even_with_sink_configured(tmp_path, monkeypatch):
    """Reviewed publication only: executor success must not call the backend."""
    profile = _profile({
        "executor": "codex",
        "artifact_sink": {"url": "https://loimi.mesh/mcp/", "bearer_token_env": "LOIMI_TOKEN"},
    })
    runs_root = _paths(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = CodexExecutor(
        profile, runs_root=runs_root, session_factory=StubThread(default_events())
    )
    app_module._run_store = RunStore(root=runs_root, retention=50)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "fix it"})
    assert resp.status_code == 200, resp.text
    record = app_module._run_store.get(resp.json()["run_id"])
    assert record.status == "succeeded"
    assert record.artifact_stored is None  # no automatic store_document
    assert record.diff_path  # local harvest remains the source of truth

async def test_no_sink_configured_leaves_field_none(tmp_path):
    profile = _profile({"executor": "codex"})
    runs_root = _paths(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = CodexExecutor(
        profile, runs_root=runs_root, session_factory=StubThread(default_events())
    )
    app_module._run_store = RunStore(root=runs_root, retention=50)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "fix it"})
    assert app_module._run_store.get(resp.json()["run_id"]).artifact_stored is None