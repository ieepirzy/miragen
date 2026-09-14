"""The runtime tool library: scheduling (schedule_wakeup / list / cancel)
over the managed-schedules machinery, one-shot `at` bindings that delete
themselves after firing, the interactive-mode gate, and the
you-cancel-only-yours boundary.
"""

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import miragen.app  # noqa: F401
app_module = sys.modules["miragen.app"]
from miragen.models import AgentProfile, RunProvenance
from miragen.runtime_tools.scheduling import (
    SchedulingBackend,
    build_scheduling_mcp,
    build_scheduling_tools,
)
from miragen.schedules import ScheduleSpec, ScheduleStore


def _make_profile(mode="hybrid", **kw):
    triggers = [{"type": "http"}] if mode != "autonomous" else [
        {"type": "cron", "schedule": "0 9 * * *"}]
    return AgentProfile.model_validate({
        "name": "test-agent", "mode": mode, "triggers": triggers,
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
        **kw,
    })


@pytest.fixture
def backend(tmp_path):
    store = ScheduleStore(root=tmp_path / "schedules")
    reconciled, dropped = [], []
    backend = SchedulingBackend(
        store,
        reconcile=lambda binding: reconciled.append(binding.name),
        drop_job=lambda name: dropped.append(name),
        next_fire_at=lambda name: "2026-09-14T05:00:00+00:00",
    )
    backend._reconciled, backend._dropped = reconciled, dropped
    return backend


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._schedule_store = None
    app_module._scheduling = None
    app_module._run_store = None


# ── ScheduleSpec: the one-shot variant ───────────────────────────────────────


class TestOneShotSpec:
    def test_exactly_one_of_three(self):
        with pytest.raises(Exception, match="exactly one"):
            ScheduleSpec()
        with pytest.raises(Exception, match="exactly one"):
            ScheduleSpec(cron="0 9 * * *", at=datetime.now(timezone.utc))
        assert ScheduleSpec(at=datetime.now(timezone.utc)).one_shot is True
        assert ScheduleSpec(cron="0 9 * * *").one_shot is False

    async def test_one_shot_binding_deletes_itself_after_firing(self, tmp_path):
        store = ScheduleStore(root=tmp_path / "schedules")
        app_module._schedule_store = store
        app_module._profile = _make_profile()
        store.upsert(
            "once", schedule=ScheduleSpec(at=datetime.now(timezone.utc)),
            prompt="wake up",
        )
        with patch("miragen.app.run_agent_scheduled", AsyncMock()) as fired:
            await app_module._run_managed_schedule("once")
        fired.assert_awaited_once()
        assert store.get("once") is None  # gone after its single fire

    async def test_recurring_binding_survives_firing(self, tmp_path):
        store = ScheduleStore(root=tmp_path / "schedules")
        app_module._schedule_store = store
        app_module._profile = _make_profile()
        store.upsert("daily", schedule=ScheduleSpec(cron="0 9 * * *"), prompt="go")
        with patch("miragen.app.run_agent_scheduled", AsyncMock()):
            await app_module._run_managed_schedule("daily")
        assert store.get("daily") is not None

    def test_past_at_clamps_to_prompt_fire(self):
        spec = ScheduleSpec(at=datetime.now(timezone.utc) - timedelta(hours=1))
        trigger = app_module._apscheduler_trigger(spec)
        assert trigger.run_date > datetime.now(timezone.utc)


# ── the backend ──────────────────────────────────────────────────────────────


class TestSchedulingBackend:
    def test_one_shot_creation(self, backend):
        result = backend.create(prompt="resume the deploy", run_id="r1",
                                in_minutes=30)
        assert result["status"] == "accepted"
        assert result["one_shot"] is True
        binding = backend.store.get(result["name"])
        assert binding.schedule.at is not None
        assert binding.provenance.model_dump()["scheduled_by"] == "agent"
        assert backend._reconciled == [result["name"]]

    def test_exactly_one_timing_argument(self, backend):
        none = backend.create(prompt="p", run_id=None)
        both = backend.create(prompt="p", run_id=None, in_minutes=5,
                              cron="0 9 * * *")
        assert none["status"] == both["status"] == "rejected"

    def test_recurring_interval_floor(self, backend):
        result = backend.create(prompt="p", run_id=None, every_minutes=0.01)
        assert result["status"] == "accepted"
        assert backend.store.get(result["name"]).schedule.every_s == 10

    def test_scheduler_rejection_rolls_the_store_back(self, tmp_path):
        store = ScheduleStore(root=tmp_path / "schedules")

        def exploding_reconcile(binding):
            raise RuntimeError("scheduler down")

        backend = SchedulingBackend(store, exploding_reconcile,
                                    lambda n: None, lambda n: None)
        result = backend.create(prompt="p", run_id=None, in_minutes=5)
        assert result["status"] == "rejected"
        assert store.list() == []  # no zombie binding the scheduler doesn't hold

    def test_cancel_only_own_schedules(self, backend):
        own = backend.create(prompt="p", run_id="r1", in_minutes=5)
        backend.store.upsert(
            "operator-nightly", schedule=ScheduleSpec(cron="0 3 * * *"),
            prompt="op", provenance=RunProvenance.model_validate(
                {"requested_by": "operator"}),
        )
        assert backend.cancel(own["name"])["status"] == "cancelled"
        refused = backend.cancel("operator-nightly")
        assert refused["status"] == "rejected"
        assert backend.store.get("operator-nightly") is not None
        assert backend.cancel("missing")["status"] == "not_found"

    def test_list_marks_cancellability(self, backend):
        backend.create(prompt="mine", run_id="r1", in_minutes=5)
        backend.store.upsert("op", schedule=ScheduleSpec(cron="0 3 * * *"),
                             prompt="op")
        flags = {e["name"]: e["cancellable"] for e in backend.list()}
        assert sum(flags.values()) == 1 and flags["op"] is False


# ── model-tier tools + gating ────────────────────────────────────────────────


class TestToolsAndGating:
    async def test_schedule_wakeup_tool_round_trip(self, backend):
        tools = build_scheduling_tools(backend, lambda: "r-1", lambda: "ops")
        schedule_wakeup, list_schedules, cancel_schedule = tools

        created = json.loads(await schedule_wakeup(
            "resume arc 4", in_minutes=15))
        assert created["status"] == "accepted"
        binding = backend.store.get(created["name"])
        assert binding.instance == "ops"  # fires back onto the same instance

        listed = json.loads(await list_schedules())
        assert listed["schedules"][0]["cancellable"] is True

        cancelled = json.loads(await cancel_schedule(created["name"]))
        assert cancelled["status"] == "cancelled"
        assert backend._dropped == [created["name"]]

    def test_interactive_mode_gets_no_scheduling(self, tmp_path):
        """A self-scheduled fire is self-activation — the thing interactive
        mode promises not to do (same rule as PUT /schedules)."""
        for mode, expected in (("interactive", False), ("hybrid", True),
                               ("autonomous", True)):
            profile = _make_profile(mode=mode)
            enabled = profile.mode != "interactive" and profile.runtime_tools.schedule
            assert enabled is expected

    def test_profile_can_opt_out(self):
        profile = _make_profile(runtime_tools={"schedule": False})
        assert profile.runtime_tools.schedule is False

    async def test_mcp_surface(self, backend, tmp_path):
        from miragen.runs import RunStore

        store = RunStore(root=tmp_path / "runs")
        store.start(agent_name="a", trigger="http", prompt="p", instance="ops")
        mcp = build_scheduling_mcp(lambda: (backend, store))
        blocks, structured = await mcp.call_tool("schedule_wakeup", {
            "prompt": "continue the migration", "in_minutes": 10,
        })
        result = json.loads(structured["result"])
        assert result["status"] == "accepted"
        assert backend.store.get(result["name"]).instance == "ops"

    async def test_mcp_unconfigured_is_a_loud_error(self):
        mcp = build_scheduling_mcp(lambda: (None, None))
        with pytest.raises(Exception, match="not enabled"):
            await mcp.call_tool("list_schedules", {})
