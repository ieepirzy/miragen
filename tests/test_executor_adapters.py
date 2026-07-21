"""Executor tier refinement — factory dispatch, wall-clock timeout, the
claude-code and spawn adapters, and the artifact sink.

Design record: docs/design/executor-tier-refinement.md. The Claude SDK is
stubbed via ClaudeCodeExecutor's query_factory seam and plain stand-in message
classes; SpawnExecutor runs real subprocesses; LoimiSink runs against an
httpx.MockTransport speaking just enough MCP streamable-HTTP.
"""

import asyncio
import json
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
    cases = {
        "codex": ({"executor": "codex"}, CodexExecutor),
        "claude-code": ({"executor": "claude-code"}, ClaudeCodeExecutor),
        "spawn": ({"executor": "spawn", "command": ["/bin/true"]}, SpawnExecutor),
    }
    for kind, (body, cls) in cases.items():
        backend = build_executor(_profile(body), runs_root=tmp_path / "runs")
        assert type(backend) is cls, kind
        assert isinstance(backend, ExecutorBackend)


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


class HangingThread:
    """Emits its resume handle, then wedges — the case turn_timeout_s exists for."""

    id = "thr_hang"

    async def run_streamed(self, prompt):
        async def gen():
            yield {"type": "thread.started", "thread_id": "thr_hang"}
            await asyncio.sleep(30)

        class _S:
            events = gen()

        return _S()


async def test_turn_timeout_suspends_run_resumably(tmp_path):
    profile = _profile({"executor": "codex", "turn_timeout_s": 1})
    runs_root = _paths(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = CodexExecutor(
        profile, runs_root=runs_root, thread_factory=lambda **kw: HangingThread()
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
        app_module._executor._thread_factory = lambda **kw: StubThread(default_events())
        resp = await client.post(f"/runs/{run_id}/resume", json={"prompt": "unwedge"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "succeeded"


async def test_no_timeout_when_disabled(tmp_path):
    profile = _profile({"executor": "codex", "turn_timeout_s": None})
    runs_root = _paths(profile, tmp_path)
    executor = CodexExecutor(
        profile, runs_root=runs_root, thread_factory=lambda **kw: StubThread(default_events())
    )
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = RunStore(root=runs_root, retention=50)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "fine"})
        assert resp.status_code == 200
        assert app_module._run_store.get(resp.json()["run_id"]).status == "succeeded"


class SilentHangingThread:
    """Hangs before emitting ANY event — the id exists only on the thread
    object. Regression for Codex review: the resume handle must be persisted
    before streaming, or a timeout here becomes non-resumable."""

    id = "thr_early"

    async def run_streamed(self, prompt):
        async def gen():
            await asyncio.sleep(30)
            yield {}  # pragma: no cover — makes gen() an async generator

        class _S:
            events = gen()

        return _S()


async def test_timeout_before_any_event_still_recovers_thread_id(tmp_path):
    profile = _profile({"executor": "codex"})
    runs_root = _paths(profile, tmp_path)
    executor = CodexExecutor(
        profile, runs_root=runs_root, thread_factory=lambda **kw: SilentHangingThread()
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(executor.run_job("wedge", "run-early"), timeout=0.3)
    assert executor.latest_thread_id("run-early") == "thr_early"


def test_latest_thread_id_survives_long_event_streams(tmp_path):
    """Regression for Codex review: thread.started is typically the FIRST
    event; a tail-window scan loses it once a long turn pushes it out."""
    profile = _profile({"executor": "codex"})
    runs_root = _paths(profile, tmp_path)
    executor = CodexExecutor(profile, runs_root=runs_root, thread_factory=lambda **kw: None)

    events_path = executor._events_path("run-long")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("w") as f:
        f.write(json.dumps({"type": "thread.started", "thread_id": "thr_first"}) + "\n")
        for i in range(1500):
            f.write(json.dumps({"type": "item.completed", "item": {"type": "stdout", "text": str(i)}}) + "\n")
    assert executor.latest_thread_id("run-long") == "thr_first"


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
    _, executor = _spawn_executor(tmp_path, ["/bin/sh", "-c", "echo boom >&2; exit 3"])
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


async def _run_with_sink(tmp_path, monkeypatch, *, fail):
    profile = _profile({
        "executor": "codex",
        "artifact_sink": {"url": "https://loimi.mesh/mcp/", "bearer_token_env": "LOIMI_TOKEN"},
    })
    runs_root = _paths(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = CodexExecutor(
        profile, runs_root=runs_root, thread_factory=lambda **kw: StubThread(default_events())
    )
    app_module._run_store = RunStore(root=runs_root, retention=50)

    stub = _StubSink(fail=fail)
    monkeypatch.setattr(app_module, "build_sink", lambda spec, *, bearer_token=None: stub)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "fix it"})
    assert resp.status_code == 200, resp.text
    return stub, app_module._run_store.get(resp.json()["run_id"])


async def test_sink_success_marks_artifact_stored(tmp_path, monkeypatch):
    stub, record = await _run_with_sink(tmp_path, monkeypatch, fail=False)
    assert record.status == "succeeded"
    assert record.artifact_stored is True
    (diff, metadata) = stub.stored[0]
    assert metadata["run_id"] == record.run_id
    assert metadata["thread_id"] == "thr_stub123"


async def test_sink_failure_never_touches_run_status(tmp_path, monkeypatch):
    _, record = await _run_with_sink(tmp_path, monkeypatch, fail=True)
    assert record.status == "succeeded"  # sink is advisory, run outcome untouched
    assert record.artifact_stored is False
    assert record.diff_path  # the diff on disk stays the source of truth


async def test_no_sink_configured_leaves_field_none(tmp_path):
    profile = _profile({"executor": "codex"})
    runs_root = _paths(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = CodexExecutor(
        profile, runs_root=runs_root, thread_factory=lambda **kw: StubThread(default_events())
    )
    app_module._run_store = RunStore(root=runs_root, retention=50)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "fix it"})
    assert app_module._run_store.get(resp.json()["run_id"]).artifact_stored is None
