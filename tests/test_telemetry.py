"""Telemetry completeness (issue #33 Phase E) — cached input tokens, the
setup/active/wall/blocked interval formulas, resume counts, and tool-call
summaries, with nullable semantics for executors that can't report a metric.
"""

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

import miragen.app  # noqa: F401

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.executor.base import _usage_from_payload
from miragen.models import RunUsage, sum_usage
from miragen.runs import RunStore

from tests.test_executor import StubThread, _executor_profile, default_events, make_executor


# ── Cached input tokens ──────────────────────────────────────────────────────


def test_usage_payload_parses_normalized_cached_tokens():
    usage = _usage_from_payload({"input_tokens": 100, "output_tokens": 5, "cached_input_tokens": 80})
    assert usage.cached_input_tokens == 80


def test_usage_payload_parses_openai_style_nested_cached_tokens():
    usage = _usage_from_payload({
        "input_tokens": 100, "output_tokens": 5,
        "input_tokens_details": {"cached_tokens": 64},
    })
    assert usage.cached_input_tokens == 64


def test_usage_payload_without_cached_tokens_stays_null():
    usage = _usage_from_payload({"input_tokens": 100, "output_tokens": 5})
    assert usage.cached_input_tokens is None


def test_sum_usage_accumulates_cached_tokens_and_preserves_null():
    a = RunUsage(requests=1, input_tokens=10, cached_input_tokens=8)
    b = RunUsage(requests=1, input_tokens=10, cached_input_tokens=2)
    assert sum_usage(a, b).cached_input_tokens == 10
    c = RunUsage(requests=1, input_tokens=10)
    assert sum_usage(c, RunUsage(requests=1)).cached_input_tokens is None
    # one reporting side is enough to start the total
    assert sum_usage(a, c).cached_input_tokens == 8


# ── Interval formulas: wall / blocked / active / setup, resume counts ────────


def test_reopen_counts_resume_and_accumulates_blocked_interval(tmp_path):
    store = RunStore(root=tmp_path / "runs")
    record = store.start(agent_name="a", trigger="http", prompt="p")
    record = store.finish(record, status="suspended", exit_reason="budget")
    # simulate the run having sat suspended for 10 minutes
    record = record.model_copy(update={
        "finished_at": datetime.now(timezone.utc) - timedelta(minutes=10)
    })
    reopened = store.reopen(record)
    assert reopened.resume_count == 1
    assert 599 < reopened.blocked_s < 605
    assert reopened.active_s is None  # unknown while running

    # a second suspend/resume keeps accumulating
    finished = store.finish(reopened, status="suspended", exit_reason="budget")
    again = store.reopen(finished)
    assert again.resume_count == 2
    assert again.blocked_s >= reopened.blocked_s


def test_finish_computes_active_as_wall_minus_blocked(tmp_path):
    store = RunStore(root=tmp_path / "runs")
    record = store.start(agent_name="a", trigger="http", prompt="p")
    record = record.model_copy(update={
        "started_at": datetime.now(timezone.utc) - timedelta(seconds=100),
        "blocked_s": 40.0,
    })
    finished = store.finish(record, status="succeeded")
    assert 99 < finished.duration_s < 102  # wall clock includes blocked time
    assert 59 < finished.active_s < 62    # active = wall - blocked


def test_finish_keeps_unreported_telemetry_null(tmp_path):
    store = RunStore(root=tmp_path / "runs")
    record = store.start(agent_name="a", trigger="http", prompt="p")
    finished = store.finish(record, status="succeeded")
    assert finished.setup_s is None
    assert finished.tool_call_count is None
    assert finished.tool_call_failures is None
    assert finished.blocked_s is None
    assert finished.resume_count == 0


# ── Executor turn telemetry ──────────────────────────────────────────────────


async def test_run_job_reports_setup_time_and_tool_summary(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    result = await executor.run_job("task", "tele-1")
    assert result.setup_s is not None and result.setup_s >= 0
    # default_events: one command_execution (tool) + one agent_message (not)
    assert result.tool_call_count == 1
    assert result.tool_call_failures == 0


async def test_run_job_counts_tool_failures(tmp_path):
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "x", "exit_code": 1}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "y", "exit_code": 0}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "status": "failed"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    executor = make_executor(_executor_profile(), tmp_path, events=events)
    result = await executor.run_job("task", "tele-2")
    assert result.tool_call_count == 3
    assert result.tool_call_failures == 2


async def test_run_job_without_item_events_reports_null_tool_metrics(tmp_path):
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    executor = make_executor(_executor_profile(), tmp_path, events=events)
    result = await executor.run_job("task", "tele-3")
    assert result.tool_call_count is None
    assert result.tool_call_failures is None


async def test_message_only_item_stream_reports_zero_not_null(tmp_path):
    """Item events flowed, so tool metrics ARE reportable — a run that used
    no tools reports 0, distinct from an adapter that can't report at all."""
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    executor = make_executor(_executor_profile(), tmp_path, events=events)
    result = await executor.run_job("task", "tele-4")
    assert result.tool_call_count == 0
    assert result.tool_call_failures == 0


# ── End-to-end accumulation across resume ────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._executor = None


async def test_resumed_run_accumulates_telemetry(tmp_path):
    profile = _executor_profile(limits={"tokens_per_run": 100})
    events = default_events(tokens=(90, 60))  # 150 > 100 → suspend
    executor = make_executor(profile, tmp_path, events=events)
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/run", json={"prompt": "big task"})
        run_id = resp.json()["run_id"]
        record = app_module._run_store.get(run_id)
        assert record.status == "suspended"
        first_setup = record.setup_s
        assert first_setup is not None and record.tool_call_count == 1

        app_module._profile.limits = None
        resp = await c.post(f"/runs/{run_id}/resume", json={"prompt": "go on"})
        assert resp.status_code == 200
        resumed = resp.json()
        assert resumed["status"] == "succeeded"
        assert resumed["resume_count"] == 1
        assert resumed["blocked_s"] is not None and resumed["blocked_s"] >= 0
        assert resumed["active_s"] == pytest.approx(
            resumed["duration_s"] - resumed["blocked_s"], abs=0.01
        )
        # accumulated across both turns
        assert resumed["setup_s"] >= first_setup
        assert resumed["tool_call_count"] == 2
        assert resumed["usage"]["input_tokens"] == 180


def test_no_pricing_tables_in_miragen():
    """Phase E guardrail: cost/pricing stays a control-plane concern."""
    import pathlib
    source = "".join(
        p.read_text() for p in pathlib.Path("miragen").rglob("*.py")
    )
    assert "price" not in source.lower()
    assert "cost_per" not in source.lower()
