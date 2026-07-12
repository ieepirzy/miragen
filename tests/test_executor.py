"""Executor backend tier — profile validation, state machine, workspace/diff
harvest, event stream, and the resume/abandon/diff/events endpoints.

The Codex SDK is stubbed via CodexExecutor's thread_factory seam; workspaces
and git harvest are real.
"""

import json
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import miragen.app  # ensure module is registered in sys.modules

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.executor import CodexExecutor
from miragen.models import AgentProfile
from miragen.runs import RunStore


def _executor_profile(**kw):
    body = {
        "name": "codex-worker",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "executor": {
            "executor": "codex",
            "instructions": "You are a repository surgeon.",
            "mcp_servers": [
                {"name": "loimi", "url": "https://loimi.mesh/mcp/", "bearer_token_env": "LOIMI_TOKEN"}
            ],
        },
        **kw,
    }
    return AgentProfile.model_validate(body)


# ── Stub thread layer ─────────────────────────────────────────────────────────


class StubStreamed:
    def __init__(self, events):
        self._events = events

    @property
    async def events(self):
        raise AssertionError  # replaced below; kept for symmetry

    async def __aiter__(self):
        for e in self._events:
            yield e


class StubThread:
    def __init__(self, events, thread_id="thr_stub123", touch=None):
        self._events = events
        self.id = thread_id
        self._touch = touch
        self.prompts: list[str] = []

    async def run_streamed(self, prompt):
        self.prompts.append(prompt)
        if self._touch:
            self._touch()

        async def gen():
            for e in self._events:
                yield e

        class _S:
            events = gen()

        return _S()


def default_events(thread_id="thr_stub123", tokens=(100, 50), message="done, files changed"):
    return [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
        {"type": "turn.completed", "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]}},
    ]


def make_executor(profile, tmp_path, events=None, thread=None, raise_exc=None):
    profile.executor.workspace_root = str(tmp_path / "workspaces")
    profile.executor.codex_home = str(tmp_path / "codex-home")
    runs_root = tmp_path / "runs"

    def factory(*, thread_id, options):
        if raise_exc is not None:
            raise raise_exc
        return thread or StubThread(events if events is not None else default_events())

    return CodexExecutor(profile, runs_root=runs_root, thread_factory=factory)


# ── Profile validation ────────────────────────────────────────────────────────


def test_profile_requires_exactly_one_backend():
    with pytest.raises(ValidationError, match="exactly one backend"):
        AgentProfile.model_validate({
            "name": "x", "mode": "interactive", "triggers": [{"type": "http"}],
        })
    with pytest.raises(ValidationError, match="exactly one backend"):
        AgentProfile.model_validate({
            "name": "x", "mode": "interactive", "triggers": [{"type": "http"}],
            "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "hi"},
            "executor": {"executor": "codex", "instructions": "hi"},
        })


def test_executor_profile_rejects_model_tier_fields():
    with pytest.raises(ValidationError, match="model-tier fields"):
        _executor_profile(tools=["some_tool"])
    with pytest.raises(ValidationError, match="model-tier fields"):
        _executor_profile(approval_required=["delete_*"])


def test_executor_profile_defaults_are_unattended_safe():
    p = _executor_profile()
    assert p.is_executor
    assert p.executor.approval_policy == "never"  # gotcha (a): never stall waiting for a human
    assert p.executor.sandbox_mode == "workspace-write"


# ── Config injection ──────────────────────────────────────────────────────────


def test_prepare_writes_mcp_config(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    executor.prepare()
    config = (tmp_path / "codex-home" / "config.toml").read_text()
    assert "[mcp_servers.loimi]" in config
    assert 'url = "https://loimi.mesh/mcp/"' in config
    # per-agent credential referenced by NAME only — value never in config
    assert 'bearer_token_env_var = "LOIMI_TOKEN"' in config
    assert "LOIMI_TOKEN=" not in config


# ── State machine ─────────────────────────────────────────────────────────────


async def test_success_harvests_diff_and_captures_thread(tmp_path):
    profile = _executor_profile()
    ws_touch: dict = {}

    executor = make_executor(profile, tmp_path)

    # simulate the executor writing into its workspace before we harvest
    run_id = "run1"
    ws = Path(profile.executor.workspace_root) / run_id
    executor._prepare_workspace(ws)
    (ws / "hello.py").write_text("print('warp')\n")

    result = await executor.run_job("fix the bug", run_id)
    assert result.status == "succeeded"
    assert result.thread_id == "thr_stub123"
    assert result.output == "done, files changed"
    assert result.usage.input_tokens == 100 and result.usage.output_tokens == 50

    diff = Path(result.diff_path).read_text()
    assert "hello.py" in diff and "+print('warp')" in diff


async def test_instructions_prepended_only_on_first_turn(tmp_path):
    thread = StubThread(default_events())
    executor = make_executor(_executor_profile(), tmp_path, thread=thread)
    await executor.run_job("do the thing", "run-a")
    assert thread.prompts[0].startswith("You are a repository surgeon.")
    await executor.run_job("continue", "run-a", thread_id="thr_stub123", first_turn=False)
    assert thread.prompts[1] == "continue"


async def test_turn_failed_event_is_resumable_crash(tmp_path):
    events = [
        {"type": "thread.started", "thread_id": "thr_x"},
        {"type": "turn.failed", "error": {"message": "API exploded"}},
    ]
    executor = make_executor(_executor_profile(), tmp_path, events=events)
    result = await executor.run_job("try", "run2")
    assert result.status == "failed"
    assert result.exit_reason == "crash"
    assert result.thread_id == "thr_x"  # resume handle survives the failure
    assert result.diff_path is None  # partial work is resume state, not harvest


async def test_sdk_exception_is_resumable_crash(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path, raise_exc=RuntimeError("spawn failed"))
    result = await executor.run_job("try", "run3")
    assert result.status == "failed"
    assert result.exit_reason == "crash"
    assert result.diff_path is None


async def test_budget_exhaustion_suspends_without_harvest(tmp_path):
    profile = _executor_profile(limits={"tokens_per_run": 100})
    events = default_events(tokens=(90, 60))  # 150 > 100
    executor = make_executor(profile, tmp_path, events=events)
    result = await executor.run_job("big task", "run4")
    assert result.status == "suspended"
    assert result.exit_reason == "budget"
    assert result.diff_path is None
    assert result.thread_id == "thr_stub123"


async def test_events_are_persisted_jsonl(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    await executor.run_job("task", "run5")
    events = executor.read_events("run5")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "thread.started" and kinds[-1] == "turn.completed"
    assert all("ts" in e for e in events)


# ── App wiring: dispatch + executor endpoints ─────────────────────────────────


@pytest.fixture(autouse=True)
def reset_executor_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._executor = None


@pytest.fixture
async def executor_client(tmp_path):
    profile = _executor_profile()
    executor = make_executor(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_run_endpoint_dispatches_to_executor(executor_client):
    resp = await executor_client.post("/run", json={"prompt": "fix it"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["output"] == "done, files changed"
    record = app_module._run_store.get(body["run_id"])
    assert record.status == "succeeded"
    assert record.thread_id == "thr_stub123"
    assert record.workspace and record.diff_path


async def test_resume_and_abandon_flow(executor_client, tmp_path):
    # Force a budget suspension
    app_module._profile.limits = type(app_module._profile).model_validate({
        **json.loads(app_module._profile.model_dump_json()),
        "limits": {"tokens_per_run": 100},
    }).limits
    resp = await executor_client.post("/run", json={"prompt": "big task"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    record = app_module._run_store.get(run_id)
    assert record.status == "suspended" and record.exit_reason == "budget"

    # Resume: raise the budget, give it another turn on the same thread
    app_module._profile.limits = None
    resp = await executor_client.post(f"/runs/{run_id}/resume", json={"prompt": "keep going"})
    assert resp.status_code == 200, resp.text
    resumed = resp.json()
    assert resumed["status"] == "succeeded"
    assert resumed["thread_id"] == "thr_stub123"
    # usage accumulated across turns (two turns of 100 in / 50 out each)
    assert resumed["usage"]["input_tokens"] == 200
    assert resumed["usage"]["output_tokens"] == 100

    # A succeeded run is terminal — not resumable, not abandonable
    resp = await executor_client.post(f"/runs/{run_id}/resume", json={"prompt": "again"})
    assert resp.status_code == 409
    resp = await executor_client.post(f"/runs/{run_id}/abandon")
    assert resp.status_code == 409


async def test_abandon_with_workspace_discard(executor_client, tmp_path):
    app_module._executor._thread_factory = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    resp = await executor_client.post("/run", json={"prompt": "doomed"})
    assert resp.status_code == 500
    run_id = app_module._run_store.list(limit=1)[0].run_id
    record = app_module._run_store.get(run_id)
    assert record.status == "failed" and record.exit_reason == "crash"

    ws = Path(record.workspace)
    assert ws.exists()
    resp = await executor_client.post(f"/runs/{run_id}/abandon", params={"discard_workspace": "true"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "abandoned"
    assert not ws.exists()


async def test_diff_and_events_endpoints(executor_client):
    resp = await executor_client.post("/run", json={"prompt": "fix it"})
    run_id = resp.json()["run_id"]

    resp = await executor_client.get(f"/runs/{run_id}/events")
    assert resp.status_code == 200
    assert resp.json()["count"] >= 4

    resp = await executor_client.get(f"/runs/{run_id}/diff")
    assert resp.status_code == 200  # empty diff is still a harvested diff

    resp = await executor_client.post("/run/stream", json={"prompt": "x"})
    assert resp.status_code == 400  # executors don't stream text
