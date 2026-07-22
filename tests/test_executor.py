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


# ── Stub session layer ──────────────────────────────────────────────────────
#
# The codex adapter's test seam is `session_factory(prompt, *, thread_id,
# first_turn, options) -> async iterator of notifications`. In tests the
# "notifications" are already-normalized payload dicts, which pass straight
# through `_normalize_notification` — so the base-tier state machine is driven
# with the same normalized events regardless of the real SDK. StubThread keeps
# its name/shape (several test modules import it) but is now that callable:
# it records prompts, optionally touches the workspace mid-turn, and can raise.


class StubThread:
    def __init__(self, events, thread_id="thr_stub123", touch=None, raise_exc=None):
        self._events = events
        self.id = thread_id
        self._touch = touch
        self._raise = raise_exc
        self.prompts: list[str] = []

    def __call__(self, prompt, *, thread_id=None, first_turn=True, options=None):
        self.prompts.append(prompt)
        events, touch, raise_exc = self._events, self._touch, self._raise

        async def gen():
            if raise_exc is not None:
                raise raise_exc
            if touch is not None:
                touch()
            for e in events:
                yield e

        return gen()


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
    session = thread or StubThread(
        events if events is not None else default_events(), raise_exc=raise_exc
    )
    return CodexExecutor(profile, runs_root=runs_root, session_factory=session)


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


async def test_harvest_diff_includes_executors_own_mid_turn_commit(tmp_path):
    """Regression: harvest must diff against the workspace baseline, not HEAD.
    If the executor commits mid-turn, HEAD moves past the baseline and a bare
    `git diff --cached` (no revision) only shows the uncommitted remainder,
    silently dropping everything already committed."""
    profile = _executor_profile()
    run_id = "run-mid-commit"

    def commit_mid_turn():
        (ws / "committed.py").write_text("print('committed by executor')\n")
        import subprocess

        subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(ws), "-c", "user.email=x@y.z", "-c", "user.name=x",
             "commit", "-q", "-m", "executor's own commit"],
            check=True,
        )
        (ws / "uncommitted.py").write_text("print('left uncommitted')\n")

    executor = make_executor(profile, tmp_path, thread=StubThread(default_events(), touch=commit_mid_turn))
    ws = Path(profile.executor.workspace_root) / run_id

    result = await executor.run_job("fix the bug", run_id)
    assert result.status == "succeeded"
    diff = Path(result.diff_path).read_text()
    assert "committed.py" in diff
    assert "uncommitted.py" in diff


async def test_miragen_metadata_excluded_from_classic_diff(tmp_path):
    """P2 regression: internal .miragen files (harvest output, intervention
    archive, malformed sentinels left by a prior turn) must never leak into
    the classic-workspace diff on a later successful harvest."""
    executor = make_executor(_executor_profile(), tmp_path)
    ws = Path(executor.spec.workspace_root) / "run-hygiene"
    executor._prepare_workspace(ws)
    (ws / "real.py").write_text("print('work')\n")
    # leftover control metadata from a prior (suspended/intervention) turn
    (ws / ".miragen" / "interventions").mkdir(parents=True, exist_ok=True)
    (ws / ".miragen" / "interventions" / "abc.json").write_text('{"question": "?"}')
    (ws / ".miragen" / "intervention.invalid.json").write_text("garbage")

    diff = Path(await executor._harvest_diff(ws)).read_text()
    assert "real.py" in diff
    assert ".miragen" not in diff and "intervention" not in diff


async def test_workspace_prep_failure_is_resumable_crash(tmp_path):
    """Regression: a workspace-prep failure (bad workspace_root, missing git,
    permission error) must surface as a 'failed' ExecutorResult — same as any
    other crash — not an unhandled exception that skips RunStore.finish() and
    leaves the run record stuck at 'running'."""
    executor = make_executor(_executor_profile(), tmp_path)
    executor._prepare_workspace = lambda ws, *a, **kw: (_ for _ in ()).throw(OSError("permission denied"))

    result = await executor.run_job("try", "run-prep-fail")
    assert result.status == "failed"
    assert result.exit_reason == "crash"
    assert "permission denied" in result.error


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


async def test_budget_exhaustion_persists_across_resume(tmp_path):
    """Regression: the budget check must see cumulative usage across resumed
    turns, not just the latest turn's — otherwise a run suspended for going
    over budget could resume, add a small extra turn, and be reported
    'succeeded' while its true total usage is still over the cap."""
    profile = _executor_profile(limits={"tokens_per_run": 100})
    executor = make_executor(profile, tmp_path, events=default_events(tokens=(90, 60)))  # 150 > 100
    result = await executor.run_job("big task", "run-budget-resume")
    assert result.status == "suspended"

    executor._session_factory = StubThread(default_events(tokens=(5, 5)))
    result2 = await executor.run_job(
        "keep going", "run-budget-resume",
        thread_id=result.thread_id, first_turn=False, prior_usage=result.usage,
    )
    assert result2.status == "suspended"
    assert result2.exit_reason == "budget"


async def test_events_are_persisted_jsonl(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    await executor.run_job("task", "run5")
    events = executor.read_events("run5")
    kinds = [e["type"] for e in events]
    # base-tier lifecycle events bracket the adapter's own stream
    assert kinds[0] == "lifecycle.setup.started" and kinds[-1] == "lifecycle.harvest.completed"
    assert "thread.started" in kinds and "turn.completed" in kinds
    assert all("ts" in e for e in events)
    # envelope: per-run monotonic 1-based sequence + schema version
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["schema"] == "miragen/executor-event/v1" for e in events)


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


async def test_resume_stays_suspended_if_cumulative_budget_still_exceeded(executor_client, tmp_path):
    app_module._profile.limits = type(app_module._profile).model_validate({
        **json.loads(app_module._profile.model_dump_json()),
        "limits": {"tokens_per_run": 100},
    }).limits
    resp = await executor_client.post("/run", json={"prompt": "big task"})
    run_id = resp.json()["run_id"]
    record = app_module._run_store.get(run_id)
    assert record.status == "suspended"
    assert record.usage.input_tokens == 100 and record.usage.output_tokens == 50  # default_events(), 150 > 100

    # Resume without raising the budget: this turn's own usage is small, but
    # combined with the prior turn's it's still over the per-run cap.
    app_module._executor._session_factory = StubThread(default_events(tokens=(5, 5)))
    resp = await executor_client.post(f"/runs/{run_id}/resume", json={"prompt": "keep going"})
    assert resp.status_code == 200, resp.text
    resumed = resp.json()
    assert resumed["status"] == "suspended"
    assert resumed["usage"]["input_tokens"] == 105  # 100 + 5, accumulated


async def test_scheduled_run_skips_on_complete_when_suspended(tmp_path):
    """Regression: a scheduled executor run that suspends on budget must not
    trigger on_complete — run_agent returns normally (doesn't raise) for a
    suspension, so run_agent_scheduled must check the final run status
    rather than treating any non-exception return as 'done'."""
    from unittest.mock import AsyncMock, patch

    profile = _executor_profile(
        mode="autonomous",
        triggers=[{"type": "cron", "schedule": "0 * * * *"}],
        limits={"tokens_per_run": 100},
        on_complete={"notify": "telegram"},
    )
    executor = make_executor(profile, tmp_path, events=default_events(tokens=(90, 60)))  # 150 > 100
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)

    with patch("miragen.app._handle_on_complete", AsyncMock()) as mock_oc:
        from miragen.app import run_agent_scheduled
        await run_agent_scheduled("Run now.")
        mock_oc.assert_not_awaited()

    runs = app_module._run_store.list()
    assert runs[0].status == "suspended"


async def test_abandon_with_workspace_discard(executor_client, tmp_path):
    app_module._executor._session_factory = StubThread(default_events(), raise_exc=RuntimeError("boom"))
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
