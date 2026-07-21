"""Managed schedule bindings (issue #33 Phase F) — durable store with CAS,
the /schedules reconciliation API, scheduler job registration, and managed
fire semantics (trigger/provenance stamping, verbatim prompts, on_complete
dispatch, budget skip, interactive-mode rejection).
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from httpx import ASGITransport, AsyncClient

import miragen.app  # noqa: F401

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.models import AgentProfile, RunProvenance
from miragen.runs import RunStore
from miragen.schedules import BindingConflictError, ScheduleSpec, ScheduleStore


def _autonomous_profile():
    return AgentProfile.model_validate({
        "name": "worker",
        "mode": "autonomous",
        "triggers": [{"type": "cron", "schedule": "0 9 * * *"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "work"},
    })


def _interactive_profile():
    return AgentProfile.model_validate({
        "name": "worker",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "work"},
    })


BINDING = {
    "schedule": {"cron": "0 3 * * *"},
    "prompt": "Nightly refactor pass.",
    "provenance": {"routine_id": "rtn_1", "trigger_id": "trg_1"},
    "metadata": {"purpose": "nightly"},
}


# ── ScheduleSpec / store ─────────────────────────────────────────────────────


def test_schedule_spec_requires_exactly_one():
    with pytest.raises(ValueError, match="exactly one"):
        ScheduleSpec()
    with pytest.raises(ValueError, match="exactly one"):
        ScheduleSpec(cron="0 3 * * *", every_s=60)
    with pytest.raises(ValueError, match="invalid cron"):
        ScheduleSpec(cron="not a cron")
    assert ScheduleSpec(every_s=60).every_s == 60


def test_store_create_only_and_cas(tmp_path):
    store = ScheduleStore(root=tmp_path / "schedules")
    b1 = store.upsert("nightly", schedule=ScheduleSpec(cron="0 3 * * *"), prompt="p")
    assert b1.version == 1

    # create-only: existing binding without expected_version is a conflict
    with pytest.raises(BindingConflictError) as e:
        store.upsert("nightly", schedule=ScheduleSpec(cron="0 4 * * *"), prompt="p")
    assert e.value.current.version == 1

    # CAS update bumps the server-assigned version
    b2 = store.upsert(
        "nightly", schedule=ScheduleSpec(cron="0 4 * * *"), prompt="p2", expected_version=1
    )
    assert b2.version == 2 and b2.schedule.cron == "0 4 * * *"

    # stale version → conflict carrying current state
    with pytest.raises(BindingConflictError) as e:
        store.upsert("nightly", schedule=ScheduleSpec(cron="0 5 * * *"), prompt="p", expected_version=1)
    assert e.value.current.version == 2

    # updating a missing binding is a conflict, not a silent create
    with pytest.raises(BindingConflictError):
        store.upsert("ghost", schedule=ScheduleSpec(cron="0 3 * * *"), prompt="p", expected_version=1)


def test_store_delete_semantics(tmp_path):
    store = ScheduleStore(root=tmp_path / "schedules")
    store.upsert("x", schedule=ScheduleSpec(every_s=60), prompt="p")
    with pytest.raises(BindingConflictError):
        store.delete("x", expected_version=99)
    removed = store.delete("x", expected_version=1)
    assert removed.name == "x"
    assert store.get("x") is None
    with pytest.raises(KeyError):
        store.delete("x")


def test_store_survives_restart_and_skips_garbage(tmp_path):
    root = tmp_path / "schedules"
    ScheduleStore(root=root).upsert("keep", schedule=ScheduleSpec(every_s=600), prompt="p")
    (root / "broken.json").write_text("{not json")
    fresh = ScheduleStore(root=root)
    assert [b.name for b in fresh.list()] == ["keep"]


# ── API + scheduler reconciliation ───────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._executor = None
    app_module._schedule_store = None


@pytest.fixture
async def schedule_client(tmp_path, monkeypatch):
    scheduler = AsyncIOScheduler()
    scheduler.start(paused=True)  # real jobstore lookups, no firing
    monkeypatch.setattr(app_module, "_scheduler", scheduler)
    app_module._profile = _autonomous_profile()
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    app_module._schedule_store = ScheduleStore(root=tmp_path / "schedules")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, scheduler
    scheduler.shutdown(wait=False)


async def test_put_creates_binding_and_registers_job(schedule_client):
    c, scheduler = schedule_client
    resp = await c.put("/schedules/nightly", json=BINDING)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 1 and body["enabled"] is True
    assert scheduler.get_job("managed:nightly") is not None

    listed = (await c.get("/schedules")).json()
    assert listed["count"] == 1 and listed["schedules"][0]["name"] == "nightly"


async def test_put_conflicts_and_cas_update(schedule_client):
    c, scheduler = schedule_client
    assert (await c.put("/schedules/nightly", json=BINDING)).status_code == 201

    # blind re-create → 409 with current state
    resp = await c.put("/schedules/nightly", json=BINDING)
    assert resp.status_code == 409
    assert resp.json()["detail"]["current"]["version"] == 1

    # CAS update → 200, version 2, job replaced
    updated = {**BINDING, "schedule": {"every_s": 3600}, "expected_version": 1}
    resp = await c.put("/schedules/nightly", json=updated)
    assert resp.status_code == 200
    assert resp.json()["version"] == 2

    # stale CAS → 409 with version 2 in current
    resp = await c.put("/schedules/nightly", json={**BINDING, "expected_version": 1})
    assert resp.status_code == 409
    assert resp.json()["detail"]["current"]["version"] == 2


async def test_disable_keeps_file_removes_job(schedule_client):
    c, scheduler = schedule_client
    await c.put("/schedules/nightly", json=BINDING)
    resp = await c.put(
        "/schedules/nightly", json={**BINDING, "enabled": False, "expected_version": 1}
    )
    assert resp.status_code == 200
    assert scheduler.get_job("managed:nightly") is None
    assert (await c.get("/schedules/nightly")).json()["enabled"] is False


async def test_delete_removes_binding_and_job(schedule_client):
    c, scheduler = schedule_client
    await c.put("/schedules/nightly", json=BINDING)
    resp = await c.delete("/schedules/nightly", params={"expected_version": 99})
    assert resp.status_code == 409
    resp = await c.delete("/schedules/nightly", params={"expected_version": 1})
    assert resp.status_code == 200
    assert scheduler.get_job("managed:nightly") is None
    assert (await c.get("/schedules/nightly")).status_code == 404
    assert (await c.delete("/schedules/nightly")).status_code == 404


async def test_interactive_mode_skips_startup_registration(tmp_path, monkeypatch):
    """Startup reconciliation must honor the mode contract too: a stale
    schedules volume on an interactive redeploy is left on disk but not run."""
    scheduler = AsyncIOScheduler()
    scheduler.start(paused=True)
    monkeypatch.setattr(app_module, "_scheduler", scheduler)
    app_module._profile = _interactive_profile()
    store = ScheduleStore(root=tmp_path / "schedules")
    store.upsert("nightly", schedule=ScheduleSpec(cron="0 3 * * *"), prompt="p")
    app_module._schedule_store = store
    try:
        assert app_module._register_managed_schedules() == 0
        assert scheduler.get_job("managed:nightly") is None
        assert store.get("nightly") is not None  # left on disk, not deleted
    finally:
        scheduler.shutdown(wait=False)


async def test_interactive_mode_rejected(schedule_client):
    c, _ = schedule_client
    app_module._profile = _interactive_profile()
    resp = await c.put("/schedules/nightly", json=BINDING)
    assert resp.status_code == 409
    assert "hybrid" in resp.json()["detail"]
    assert app_module._schedule_store.get("nightly") is None  # nothing persisted


async def test_scheduler_failure_rolls_back_store(schedule_client, monkeypatch):
    c, _ = schedule_client
    monkeypatch.setattr(
        app_module, "_reconcile_managed_job",
        lambda binding: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    resp = await c.put("/schedules/nightly", json=BINDING)
    assert resp.status_code == 500
    assert app_module._schedule_store.get("nightly") is None  # rolled back


async def test_startup_registration_from_disk(schedule_client):
    c, scheduler = schedule_client
    store = app_module._schedule_store
    store.upsert("a", schedule=ScheduleSpec(cron="0 3 * * *"), prompt="p")
    store.upsert("b", schedule=ScheduleSpec(every_s=600), prompt="p", )
    disabled = store.upsert("c", schedule=ScheduleSpec(every_s=600), prompt="p")
    store.upsert("c", schedule=ScheduleSpec(every_s=600), prompt="p", enabled=False,
                 expected_version=disabled.version)

    assert app_module._register_managed_schedules() == 2
    assert scheduler.get_job("managed:a") is not None
    assert scheduler.get_job("managed:b") is not None
    assert scheduler.get_job("managed:c") is None


# ── Fire semantics ───────────────────────────────────────────────────────────


def _mock_run_result(output="done"):
    result = MagicMock()
    result.output = output
    usage = MagicMock(requests=1, input_tokens=10, output_tokens=5)
    result.usage = usage
    result.all_messages.return_value = []
    return result


@pytest.fixture
def fire_env(tmp_path):
    app_module._profile = _autonomous_profile()
    app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result()))
    app_module._limits = None
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    app_module._schedule_store = ScheduleStore(root=tmp_path / "schedules")
    return app_module._schedule_store


async def test_managed_fire_stamps_trigger_and_provenance(fire_env):
    fire_env.upsert(
        "nightly",
        schedule=ScheduleSpec(cron="0 3 * * *"),
        prompt="Do the nightly pass.",
        provenance=RunProvenance.model_validate({"routine_id": "rtn_1", "custom": "kept"}),
        metadata={"purpose": "nightly"},
    )
    with patch("miragen.app._handle_on_complete", AsyncMock()) as mock_oc:
        await app_module._run_managed_schedule("nightly")
        # decision: managed fires dispatch on_complete exactly like profile fires
        mock_oc.assert_awaited_once()

    (summary,) = app_module._run_store.list()
    record = app_module._run_store.get(summary.run_id)
    assert record.trigger == "managed"
    assert record.status == "succeeded"
    # completed prompt dispatched verbatim — no timestamp stamping
    assert record.prompt == "Do the nightly pass."
    prov = record.provenance.model_dump()
    assert prov["schedule_name"] == "nightly"
    assert prov["routine_id"] == "rtn_1"
    assert prov["custom"] == "kept"
    assert prov["schedule_metadata"] == {"purpose": "nightly"}
    assert "fired_at" in prov


async def test_managed_fire_skips_disabled_or_deleted_binding(fire_env):
    fire_env.upsert("gone", schedule=ScheduleSpec(every_s=600), prompt="p")
    fire_env.delete("gone")
    await app_module._run_managed_schedule("gone")
    b = fire_env.upsert("off", schedule=ScheduleSpec(every_s=600), prompt="p")
    fire_env.upsert("off", schedule=ScheduleSpec(every_s=600), prompt="p",
                    enabled=False, expected_version=b.version)
    await app_module._run_managed_schedule("off")
    assert app_module._run_store.list() == []


async def test_managed_fire_respects_daily_budget(fire_env, monkeypatch):
    fire_env.upsert("nightly", schedule=ScheduleSpec(cron="0 3 * * *"), prompt="p")
    monkeypatch.setattr(app_module, "_daily_budget_status", lambda: (500, 100))
    app_module._profile.limits = MagicMock(on_exceeded="skip")
    await app_module._run_managed_schedule("nightly")
    assert app_module._run_store.list() == []  # skipped, no run started
