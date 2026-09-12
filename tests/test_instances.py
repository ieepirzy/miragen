"""Instance model (instances/v1) — docs/design/instance-model.md.

One container per profile; instances are named state scopes within it:
per-instance history, a per-instance turn mutex, and a container-wide
concurrency cap. Ephemeral runs (no instance) keep pre-instance behaviour.
"""

import asyncio
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport

import miragen.app  # noqa: F401 — ensure module is registered in sys.modules
app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.models import DEFAULT_INSTANCE, AgentProfile
from miragen.runs import RunStore


def _make_profile(**kw):
    return AgentProfile.model_validate({
        "name": "test-agent",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
        **kw,
    })


def _mock_run_result(output="agent output"):
    usage = MagicMock()
    usage.requests = 1
    usage.input_tokens = 10
    usage.output_tokens = 5
    result = MagicMock()
    result.output = output
    result.usage = usage
    result.all_messages = MagicMock(return_value=[])
    return result


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._active_runs = 0
    app_module._busy_instances.clear()


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.run = AsyncMock(return_value=_mock_run_result())
    return agent


@pytest.fixture
async def client(mock_agent, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "HISTORIES_DIR", tmp_path / "histories")
    app_module._profile = _make_profile()
    app_module._agent = mock_agent
    app_module._run_store = RunStore(root=tmp_path / "runs")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── Admission primitives ─────────────────────────────────────────────────────


class TestAcquireRunSlot:
    def test_slot_acquired_and_released(self):
        app_module._profile = _make_profile()
        release = app_module._acquire_run_slot("foo")
        assert app_module._active_runs == 1
        assert "foo" in app_module._busy_instances
        release()
        assert app_module._active_runs == 0
        assert "foo" not in app_module._busy_instances

    def test_release_is_idempotent(self):
        app_module._profile = _make_profile()
        release = app_module._acquire_run_slot("foo")
        release()
        release()
        assert app_module._active_runs == 0

    def test_busy_instance_refused(self):
        app_module._profile = _make_profile()
        app_module._acquire_run_slot("foo")
        with pytest.raises(app_module.RunBusy, match="instance 'foo'"):
            app_module._acquire_run_slot("foo")

    def test_other_instance_still_admitted(self):
        app_module._profile = _make_profile()
        app_module._acquire_run_slot("foo")
        release = app_module._acquire_run_slot("bar")
        assert app_module._active_runs == 2
        release()

    def test_container_cap_refused(self, monkeypatch):
        app_module._profile = _make_profile()
        monkeypatch.setenv("MIRAGEN_MAX_CONCURRENT", "1")
        app_module._acquire_run_slot(None)
        with pytest.raises(app_module.RunBusy, match="max_concurrent_runs"):
            app_module._acquire_run_slot(None)

    def test_profile_limit_used_when_no_env(self, monkeypatch):
        monkeypatch.delenv("MIRAGEN_MAX_CONCURRENT", raising=False)
        app_module._profile = _make_profile(
            limits={"max_concurrent_runs": 2, "tokens_per_run": 1000}
        )
        assert app_module._max_concurrent_runs() == 2

    def test_env_overrides_profile(self, monkeypatch):
        monkeypatch.setenv("MIRAGEN_MAX_CONCURRENT", "7")
        app_module._profile = _make_profile(limits={"max_concurrent_runs": 2})
        assert app_module._max_concurrent_runs() == 7

    def test_default_limit(self, monkeypatch):
        monkeypatch.delenv("MIRAGEN_MAX_CONCURRENT", raising=False)
        app_module._profile = _make_profile()
        assert app_module._max_concurrent_runs() == app_module.DEFAULT_MAX_CONCURRENT_RUNS


# ── HTTP surface ─────────────────────────────────────────────────────────────


class TestRunWithInstance:
    async def test_instance_recorded_on_run(self, client):
        resp = await client.post("/run", json={"prompt": "hi", "instance": "alpha"})
        assert resp.status_code == 200
        record = app_module._run_store.get(resp.json()["run_id"])
        assert record.instance == "alpha"

    async def test_stateless_run_has_no_instance(self, client):
        resp = await client.post("/run", json={"prompt": "hi"})
        record = app_module._run_store.get(resp.json()["run_id"])
        assert record.instance is None

    async def test_use_history_defaults_to_default_instance(self, client):
        resp = await client.post("/run", json={"prompt": "hi", "use_history": True})
        record = app_module._run_store.get(resp.json()["run_id"])
        assert record.instance == DEFAULT_INSTANCE
        assert app_module._history_file(DEFAULT_INSTANCE).exists()

    async def test_invalid_instance_name_is_422(self, client):
        resp = await client.post("/run", json={"prompt": "hi", "instance": "../evil"})
        assert resp.status_code == 422

    async def test_named_instances_have_isolated_history(self, client):
        await client.post("/run", json={"prompt": "hi", "use_history": True, "instance": "alpha"})
        await client.post("/run", json={"prompt": "hi", "use_history": True, "instance": "beta"})
        assert app_module._history_file("alpha").exists()
        assert app_module._history_file("beta").exists()
        assert not app_module._history_file(DEFAULT_INSTANCE).exists()

    async def test_busy_instance_answers_429_with_retry_after(self, client):
        app_module._busy_instances.add("alpha")
        app_module._active_runs = 1
        resp = await client.post("/run", json={"prompt": "hi", "instance": "alpha"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert "alpha" in resp.json()["detail"]

    async def test_container_cap_answers_429(self, client, monkeypatch):
        monkeypatch.setenv("MIRAGEN_MAX_CONCURRENT", "1")
        app_module._active_runs = 1
        resp = await client.post("/run", json={"prompt": "hi"})
        assert resp.status_code == 429
        assert "max_concurrent_runs" in resp.json()["detail"]

    async def test_slot_released_after_run(self, client):
        await client.post("/run", json={"prompt": "hi", "instance": "alpha"})
        assert app_module._active_runs == 0
        assert not app_module._busy_instances

    async def test_slot_released_after_failed_run(self, client, mock_agent):
        mock_agent.run = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await client.post("/run", json={"prompt": "hi", "instance": "alpha"})
        assert resp.status_code == 500
        assert app_module._active_runs == 0
        assert not app_module._busy_instances

    async def test_two_concurrent_turns_on_one_instance_serialize(self, client, mock_agent):
        """The race the mutex exists for: the second concurrent turn of one
        instance is refused while the first is mid-flight."""
        gate = asyncio.Event()

        async def slow_run(*a, **kw):
            await gate.wait()
            return _mock_run_result()

        mock_agent.run = AsyncMock(side_effect=slow_run)
        first = asyncio.create_task(
            client.post("/run", json={"prompt": "1", "use_history": True, "instance": "a"})
        )
        while not app_module._busy_instances:
            await asyncio.sleep(0.01)
        # wait_for: a broken mutex ADMITS the second turn, which then blocks
        # on the gate — this must fail fast as a timeout, not hang the suite.
        second = await asyncio.wait_for(
            client.post("/run", json={"prompt": "2", "use_history": True, "instance": "a"}),
            timeout=5,
        )
        assert second.status_code == 429
        gate.set()
        assert (await first).status_code == 200

    async def test_async_run_claims_slot_synchronously(self, client, mock_agent):
        gate = asyncio.Event()

        async def slow_run(*a, **kw):
            await gate.wait()
            return _mock_run_result()

        mock_agent.run = AsyncMock(side_effect=slow_run)
        accepted = await client.post("/run/async", json={"prompt": "1", "instance": "a"})
        assert accepted.status_code == 202
        refused = await client.post("/run/async", json={"prompt": "2", "instance": "a"})
        assert refused.status_code == 429
        gate.set()
        while app_module._active_runs:
            await asyncio.sleep(0.01)


class TestHealthConcurrency:
    async def test_health_reports_admission_state(self, client):
        app_module._active_runs = 2
        app_module._busy_instances.add("alpha")
        body = (await client.get("/health")).json()
        assert body["concurrency"]["active_runs"] == 2
        assert body["concurrency"]["busy_instances"] == ["alpha"]
        assert body["concurrency"]["max_concurrent_runs"] >= 1

    async def test_capability_advertised(self, client):
        body = (await client.get("/health")).json()
        assert "instances/v1" in body["capabilities"]


# ── Scheduled fires ──────────────────────────────────────────────────────────


class TestScheduledInstance:
    async def test_named_instance_fire_uses_history(self, client):
        await app_module.run_agent_scheduled("go", instance="daily")
        assert app_module._history_file("daily").exists()
        runs = app_module._run_store.list(limit=1)
        assert runs[0].instance == "daily"
        assert runs[0].use_history is True

    async def test_anonymous_fire_is_ephemeral(self, client):
        await app_module.run_agent_scheduled("go")
        assert not app_module.HISTORIES_DIR.exists()
        runs = app_module._run_store.list(limit=1)
        assert runs[0].instance is None
        assert runs[0].use_history is False

    async def test_fire_skipped_when_instance_busy(self, client):
        app_module._busy_instances.add("daily")
        app_module._active_runs = 1
        await app_module.run_agent_scheduled("go", instance="daily")
        assert app_module._run_store.list(limit=5) == []

    async def test_fire_releases_slot(self, client):
        await app_module.run_agent_scheduled("go", instance="daily")
        assert app_module._active_runs == 0
        assert not app_module._busy_instances


# ── /instances listing + deletion ────────────────────────────────────────────


class TestInstancesEndpoint:
    async def test_lists_history_and_run_instances(self, client):
        await client.post("/run", json={"prompt": "hi", "use_history": True, "instance": "alpha"})
        await client.post("/run", json={"prompt": "hi", "instance": "beta"})
        body = (await client.get("/instances")).json()
        names = {i["name"]: i for i in body["instances"]}
        assert set(names) == {"alpha", "beta"}
        assert names["alpha"]["history_message_count"] == 0  # mock saves no messages
        assert names["alpha"]["last_run"]["status"] == "succeeded"
        assert names["beta"]["last_run"] is not None

    async def test_empty_listing(self, client):
        body = (await client.get("/instances")).json()
        assert body == {"count": 0, "instances": []}

    async def test_busy_instance_listed_as_running(self, client):
        app_module._busy_instances.add("alpha")
        body = (await client.get("/instances")).json()
        assert body["instances"][0]["name"] == "alpha"
        assert body["instances"][0]["running"] is True

    async def test_delete_removes_history_and_sidecar(self, client):
        await client.post("/run", json={"prompt": "hi", "use_history": True, "instance": "alpha"})
        assert app_module._history_file("alpha").exists()
        resp = await client.delete("/instances/alpha")
        assert resp.status_code == 200
        assert not app_module._history_file("alpha").exists()
        assert not app_module._history_sidecar("alpha").exists()

    async def test_delete_unknown_is_404(self, client):
        assert (await client.delete("/instances/nope")).status_code == 404

    async def test_delete_busy_is_409(self, client):
        app_module._busy_instances.add("alpha")
        assert (await client.delete("/instances/alpha")).status_code == 409

    async def test_delete_invalid_name_is_422(self, client):
        assert (await client.delete("/instances/NOT..VALID")).status_code == 422


# ── Legacy migration ─────────────────────────────────────────────────────────


class TestLegacyHistoryMigration:
    def test_legacy_file_adopted_as_default(self, tmp_path, monkeypatch):
        legacy = tmp_path / "history.json"
        legacy_sidecar = tmp_path / "history.runs.jsonl"
        legacy.write_text("[]")
        legacy_sidecar.write_text("{}\n")
        monkeypatch.setattr(app_module, "LEGACY_HISTORY_FILE", legacy)
        monkeypatch.setattr(app_module, "LEGACY_HISTORY_SIDECAR", legacy_sidecar)
        monkeypatch.setattr(app_module, "HISTORIES_DIR", tmp_path / "histories")

        app_module._migrate_legacy_history()

        assert not legacy.exists()
        assert app_module._history_file(DEFAULT_INSTANCE).read_text() == "[]"
        assert app_module._history_sidecar(DEFAULT_INSTANCE).read_text() == "{}\n"

    def test_migration_never_overwrites_existing_default(self, tmp_path, monkeypatch):
        legacy = tmp_path / "history.json"
        legacy.write_text("[1]")
        histories = tmp_path / "histories"
        histories.mkdir()
        (histories / "default.json").write_text("[2]")
        monkeypatch.setattr(app_module, "LEGACY_HISTORY_FILE", legacy)
        monkeypatch.setattr(app_module, "HISTORIES_DIR", histories)

        app_module._migrate_legacy_history()

        assert legacy.exists()  # left alone
        assert (histories / "default.json").read_text() == "[2]"

    def test_no_legacy_file_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_module, "LEGACY_HISTORY_FILE", tmp_path / "missing.json")
        monkeypatch.setattr(app_module, "HISTORIES_DIR", tmp_path / "histories")
        app_module._migrate_legacy_history()
        assert not (tmp_path / "histories").exists()


# ── Profile / binding models ─────────────────────────────────────────────────


class TestInstanceModels:
    def test_trigger_accepts_instance(self):
        profile = AgentProfile.model_validate({
            "name": "a", "mode": "autonomous",
            "triggers": [{"type": "cron", "schedule": "0 9 * * *", "instance": "daily"}],
            "spec": {"model": "m", "instructions": "i"},
        })
        assert profile.triggers[0].instance == "daily"

    def test_trigger_rejects_bad_instance_name(self):
        with pytest.raises(Exception):
            AgentProfile.model_validate({
                "name": "a", "mode": "autonomous",
                "triggers": [{"type": "cron", "schedule": "0 9 * * *", "instance": "Bad Name"}],
                "spec": {"model": "m", "instructions": "i"},
            })

    def test_limits_max_concurrent_alone_is_valid(self):
        profile = _make_profile(limits={"max_concurrent_runs": 3})
        assert profile.limits.max_concurrent_runs == 3

    def test_schedule_binding_carries_instance(self, tmp_path):
        from miragen.schedules import ScheduleSpec, ScheduleStore

        store = ScheduleStore(root=tmp_path)
        binding = store.upsert(
            "nightly", schedule=ScheduleSpec(cron="0 3 * * *"), prompt="go", instance="ops"
        )
        assert binding.instance == "ops"
        assert store.get("nightly").instance == "ops"
