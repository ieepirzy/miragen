import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

import miragen.app  # ensure module is registered in sys.modules
app_module = sys.modules["miragen.app"]
from miragen.app import app, _handle_on_complete, _load_file_secrets
from miragen.models import AgentProfile, OnComplete


def _make_profile(**kw):
    return AgentProfile.model_validate({
        "name": "test-agent",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
        **kw,
    })


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None


@pytest.fixture
def profile():
    return _make_profile()


@pytest.fixture
def profile_with_daily_limit():
    return _make_profile(limits={"tokens_per_day": 100})


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.run = AsyncMock(return_value=MagicMock(output="agent output"))
    return agent


@pytest.fixture
async def client(profile, mock_agent):
    app_module._profile = profile
    app_module._agent = mock_agent
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _mock_run_result(output="agent output", requests=1, input_tokens=10, output_tokens=5):
    """A PydanticAI AgentRunResult stand-in with real values where extract_run_details reads them."""
    usage = MagicMock()
    usage.requests = requests
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    result = MagicMock()
    result.output = output
    result.usage = MagicMock(return_value=usage)
    result.all_messages = MagicMock(return_value=[])
    return result


@pytest.fixture
def mock_agent_with_usage():
    agent = MagicMock()
    agent.run = AsyncMock(return_value=_mock_run_result())
    return agent


@pytest.fixture
async def store_client(profile, mock_agent_with_usage, tmp_path):
    from miragen.runs import RunStore

    app_module._profile = profile
    app_module._agent = mock_agent_with_usage
    app_module._run_store = RunStore(root=tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestHealth:
    async def test_returns_ok_with_agent_name(self, client, profile):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["agent"] == "test-agent"

    async def test_no_profile_returns_null_agent(self):
        app_module._profile = None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["agent"] is None


class TestRunEndpoint:
    async def test_successful_run(self, client):
        with patch("miragen.app.run_agent", AsyncMock(return_value="hello world")):
            resp = await client.post("/run", json={"prompt": "hi"})
        assert resp.status_code == 200
        assert resp.json()["output"] == "hello world"

    async def test_agent_not_ready_returns_503(self):
        app_module._agent = None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run", json={"prompt": "hi"})
        assert resp.status_code == 503

    async def test_agent_error_returns_500(self, client):
        with patch("miragen.app.run_agent", AsyncMock(side_effect=RuntimeError("boom"))):
            resp = await client.post("/run", json={"prompt": "hi"})
        assert resp.status_code == 500
        assert "boom" in resp.json()["detail"]

    async def test_header_prompt_prepended(self, mock_agent):
        profile = _make_profile(
            triggers=[{"type": "http", "header_prompt": "Context: be brief."}],
            inject_timestamp=False,
        )
        captured = {}

        async def capture(prompt, use_history=False, record=None):
            captured["prompt"] = prompt
            return "ok"

        app_module._profile = profile
        app_module._agent = mock_agent
        with patch("miragen.app.run_agent", capture):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/run", json={"prompt": "Do the thing."})

        assert "Context: be brief." in captured["prompt"]
        assert "Do the thing." in captured["prompt"]

    async def test_no_header_prompt_passes_prompt_unchanged(self, client):
        captured = {}

        async def capture(prompt, use_history=False, record=None):
            captured["prompt"] = prompt
            return "ok"

        with patch("miragen.app.run_agent", capture):
            await client.post("/run", json={"prompt": "raw prompt"})

        assert "raw prompt" in captured["prompt"]

    async def test_timestamp_injected_by_default(self, client):
        captured = {}

        async def capture(prompt, use_history=False, record=None):
            captured["prompt"] = prompt
            return "ok"

        with patch("miragen.app.run_agent", capture):
            await client.post("/run", json={"prompt": "hello"})

        import re
        assert re.search(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]", captured["prompt"])

    async def test_timestamp_suppressed_when_disabled(self, mock_agent):
        profile = _make_profile(inject_timestamp=False)
        captured = {}

        async def capture(prompt, use_history=False, record=None):
            captured["prompt"] = prompt
            return "ok"

        app_module._profile = profile
        app_module._agent = mock_agent
        with patch("miragen.app.run_agent", capture):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/run", json={"prompt": "raw prompt"})

        assert captured["prompt"] == "raw prompt"


class TestRunStreamEndpoint:
    async def test_not_ready_returns_503(self):
        app_module._agent = None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run/stream", json={"prompt": "hi"})
        assert resp.status_code == 503

    async def test_streams_sse_events(self, client, mock_agent):
        async def fake_stream_text(delta):
            for chunk in ["Hello", " world"]:
                yield chunk

        stream_ctx = AsyncMock()
        stream_ctx.__aenter__ = AsyncMock(return_value=MagicMock(stream_text=fake_stream_text))
        stream_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_agent.run_stream = MagicMock(return_value=stream_ctx)

        resp = await client.post("/run/stream", json={"prompt": "hi"})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = resp.text
        assert "data: Hello" in body
        assert "data: [DONE]" in body


def _stream_ctx(chunks=("Hello", " world"), messages=()):
    async def fake_stream_text(delta):
        for chunk in chunks:
            yield chunk

    stream = MagicMock()
    stream.stream_text = fake_stream_text
    stream.all_messages = MagicMock(return_value=list(messages))
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=stream)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestRunStreamRunRecords:
    async def test_stream_sets_run_id_header_and_finishes_record(
        self, profile, mock_agent_with_usage, tmp_path
    ):
        from miragen.runs import RunStore

        mock_agent_with_usage.run_stream = MagicMock(return_value=_stream_ctx())
        app_module._profile = profile
        app_module._agent = mock_agent_with_usage
        store = RunStore(root=tmp_path)
        app_module._run_store = store

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run/stream", json={"prompt": "hi"})

        run_id = resp.headers.get("X-Miragen-Run-Id")
        assert run_id, "stream response must carry X-Miragen-Run-Id"
        record = store.get(run_id)
        assert record is not None
        assert record.status == "succeeded"
        assert record.output == "Hello world"

    async def test_stream_without_store_has_no_header(self, client, mock_agent):
        mock_agent.run_stream = MagicMock(return_value=_stream_ctx())
        resp = await client.post("/run/stream", json={"prompt": "hi"})
        assert resp.status_code == 200
        assert "X-Miragen-Run-Id" not in resp.headers


class TestHistorySidecar:
    async def test_run_with_history_appends_sidecar_line(
        self, profile, mock_agent_with_usage, tmp_path, monkeypatch
    ):
        import json as _json
        from miragen.runs import RunStore

        monkeypatch.setattr(app_module, "HISTORY_FILE", tmp_path / "history.json")
        sidecar = tmp_path / "history.runs.jsonl"
        monkeypatch.setattr(app_module, "HISTORY_SIDECAR", sidecar)

        app_module._profile = profile
        app_module._agent = mock_agent_with_usage
        store = RunStore(root=tmp_path / "runs")
        app_module._run_store = store

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run", json={"prompt": "hi", "use_history": True})

        assert resp.status_code == 200
        run_id = resp.json()["run_id"]
        lines = sidecar.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = _json.loads(lines[0])
        assert entry["run_id"] == run_id
        assert entry["message_count"] == 0
        assert "saved_at" in entry

    async def test_stream_with_history_appends_sidecar_line(
        self, profile, mock_agent_with_usage, tmp_path, monkeypatch
    ):
        import json as _json
        from miragen.runs import RunStore

        monkeypatch.setattr(app_module, "HISTORY_FILE", tmp_path / "history.json")
        sidecar = tmp_path / "history.runs.jsonl"
        monkeypatch.setattr(app_module, "HISTORY_SIDECAR", sidecar)

        mock_agent_with_usage.run_stream = MagicMock(return_value=_stream_ctx())
        app_module._profile = profile
        app_module._agent = mock_agent_with_usage
        app_module._run_store = RunStore(root=tmp_path / "runs")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run/stream", json={"prompt": "hi", "use_history": True})

        entry = _json.loads(sidecar.read_text().strip().splitlines()[0])
        assert entry["run_id"] == resp.headers["X-Miragen-Run-Id"]

    async def test_sidecar_failure_never_fails_the_run(
        self, profile, mock_agent_with_usage, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(app_module, "HISTORY_FILE", tmp_path / "history.json")
        # Point the sidecar at an unwritable location (a directory).
        monkeypatch.setattr(app_module, "HISTORY_SIDECAR", tmp_path)

        app_module._profile = profile
        app_module._agent = mock_agent_with_usage

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run", json={"prompt": "hi", "use_history": True})

        assert resp.status_code == 200


class TestHandleOnComplete:
    async def test_no_on_complete_does_nothing(self):
        app_module._profile = _make_profile()
        await _handle_on_complete("output")

    async def test_no_profile_does_nothing(self):
        app_module._profile = None
        await _handle_on_complete("output")

    async def test_calls_log_to_handler(self):
        app_module._profile = _make_profile()
        app_module._profile = _make_profile()
        profile = _make_profile()
        profile.__dict__["on_complete"] = OnComplete(log_to="miradb")
        app_module._profile = profile

        handler = AsyncMock()
        with patch("miragen.app.registered_handlers", return_value={"miradb": handler}):
            await _handle_on_complete("result")

        handler.assert_awaited_once_with("test-agent", "result")

    async def test_calls_notify_handler(self):
        profile = _make_profile()
        profile.__dict__["on_complete"] = OnComplete(notify="telegram")
        app_module._profile = profile

        handler = AsyncMock()
        with patch("miragen.app.registered_handlers", return_value={"telegram": handler}):
            await _handle_on_complete("result")

        handler.assert_awaited_once_with("test-agent", "result")

    async def test_post_to_fires_webhook(self):
        profile = _make_profile()
        profile.__dict__["on_complete"] = OnComplete(post_to="https://example.com/hook")
        app_module._profile = profile

        mock_response = MagicMock()
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock(return_value=mock_response)))
        mock_http.__aexit__ = AsyncMock(return_value=False)

        with patch("miragen.app.registered_handlers", return_value={}), \
             patch("miragen.app.httpx.AsyncClient", return_value=mock_http):
            await _handle_on_complete("payload")

        mock_http.__aenter__.return_value.post.assert_awaited_once()

    async def test_unregistered_handler_skipped(self):
        profile = _make_profile()
        profile.__dict__["on_complete"] = OnComplete(log_to="unknown_handler")
        app_module._profile = profile

        with patch("miragen.app.registered_handlers", return_value={}):
            await _handle_on_complete("output")


class TestRunAgentCron:
    async def test_logs_and_dispatches_on_complete(self):
        profile = _make_profile(mode="autonomous",
                                 triggers=[{"type": "cron", "schedule": "0 * * * *"}])
        app_module._profile = profile

        with patch("miragen.app.run_agent", AsyncMock(return_value="done")), \
             patch("miragen.app._handle_on_complete", AsyncMock()) as mock_oc:
            from miragen.app import run_agent_cron
            await run_agent_cron("Run now.")
            mock_oc.assert_awaited_once_with("done")

    async def test_cron_error_logged_not_raised(self):
        profile = _make_profile(mode="autonomous",
                                 triggers=[{"type": "cron", "schedule": "0 * * * *"}])
        app_module._profile = profile

        with patch("miragen.app.run_agent", AsyncMock(side_effect=RuntimeError("fail"))):
            from miragen.app import run_agent_cron
            await run_agent_cron("Run now.")

    async def test_on_complete_error_logged_not_raised(self):
        profile = _make_profile(mode="autonomous",
                                 triggers=[{"type": "cron", "schedule": "0 * * * *"}])
        app_module._profile = profile

        with patch("miragen.app.run_agent", AsyncMock(return_value="done")), \
             patch("miragen.app._handle_on_complete", AsyncMock(side_effect=RuntimeError("webhook failed"))):
            from miragen.app import run_agent_cron
            await run_agent_cron("Run now.")

    async def test_cron_run_leaves_a_run_record(self, tmp_path):
        from miragen.runs import RunStore

        profile = _make_profile(mode="autonomous",
                                 triggers=[{"type": "cron", "schedule": "0 * * * *"}])
        app_module._profile = profile
        app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result(output="briefing")))
        app_module._run_store = RunStore(root=tmp_path)

        with patch("miragen.app._handle_on_complete", AsyncMock()):
            from miragen.app import run_agent_cron
            await run_agent_cron("Run now.")

        runs = app_module._run_store.list()
        assert len(runs) == 1
        assert runs[0].trigger == "cron"
        assert runs[0].status == "succeeded"


class TestLifespanRunStore:
    async def test_sweeps_interrupted_records_on_startup(self, tmp_path):
        from datetime import datetime, timezone
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from miragen.models import RunRecord
        from miragen.runs import RunStore

        # Simulate a run left "running" by a killed process.
        stale = RunRecord(
            run_id="1" * 32, agent_name="test-agent", trigger="cron",
            status="running", prompt="stale run",
            started_at=datetime.now(timezone.utc),
        )
        RunStore(root=tmp_path)._write(stale)

        profile = _make_profile(mode="autonomous", triggers=[{"type": "cron", "schedule": "0 * * * *"}])
        mock_agent = MagicMock()
        test_scheduler = AsyncIOScheduler()

        with patch("miragen.app.load_profile", return_value=profile), \
             patch("miragen.app.build_agent", return_value=(mock_agent, None)), \
             patch("miragen.app._load_file_secrets"), \
             patch.object(app_module, "_scheduler", test_scheduler), \
             patch("miragen.app.RunStore", lambda **kw: RunStore(root=tmp_path)):
            async with app_module.lifespan(app):
                record = app_module._run_store.get(stale.run_id)

        assert record.status == "interrupted"
        assert record.finished_at is not None

    async def test_run_store_instantiated_with_env_retention(self, tmp_path, monkeypatch):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        monkeypatch.setenv("MIRAGEN_RUN_RETENTION", "7")
        monkeypatch.chdir(tmp_path)

        profile = _make_profile(mode="autonomous", triggers=[{"type": "cron", "schedule": "0 * * * *"}])
        mock_agent = MagicMock()
        test_scheduler = AsyncIOScheduler()

        with patch("miragen.app.load_profile", return_value=profile), \
             patch("miragen.app.build_agent", return_value=(mock_agent, None)), \
             patch("miragen.app._load_file_secrets"), \
             patch.object(app_module, "_scheduler", test_scheduler):
            async with app_module.lifespan(app):
                assert app_module._run_store.retention == 7


class TestLifespanSchedulerRegistration:
    async def test_registers_cron_interval_and_startup_jobs(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger as APCronTrigger
        from apscheduler.triggers.date import DateTrigger
        from apscheduler.triggers.interval import IntervalTrigger as APIntervalTrigger

        profile = _make_profile(
            mode="hybrid",
            triggers=[
                {"type": "cron", "schedule": "0 * * * *"},
                {"type": "interval", "every_s": 30},
                {"type": "interval", "every_s": 60},
                {"type": "startup", "delay_s": 5},
                {"type": "http"},
            ],
        )
        mock_agent = MagicMock()
        test_scheduler = AsyncIOScheduler()

        with patch("miragen.app.load_profile", return_value=profile), \
             patch("miragen.app.build_agent", return_value=(mock_agent, None)), \
             patch("miragen.app._load_file_secrets"), \
             patch.object(app_module, "_scheduler", test_scheduler):
            async with app_module.lifespan(app):
                jobs = {job.id: job for job in test_scheduler.get_jobs()}

        assert set(jobs) == {
            "test-agent:cron",
            "test-agent:interval:0",
            "test-agent:interval:1",
            "test-agent:startup:0",
        }
        assert isinstance(jobs["test-agent:cron"].trigger, APCronTrigger)
        assert isinstance(jobs["test-agent:interval:0"].trigger, APIntervalTrigger)
        assert jobs["test-agent:interval:0"].trigger.interval.total_seconds() == 30
        assert jobs["test-agent:interval:1"].trigger.interval.total_seconds() == 60
        assert isinstance(jobs["test-agent:startup:0"].trigger, DateTrigger)

    async def test_multiple_startup_triggers_get_distinct_ids(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        profile = _make_profile(
            mode="hybrid",
            triggers=[
                {"type": "http"},
                {"type": "startup", "delay_s": 0},
                {"type": "startup", "delay_s": 10},
            ],
        )
        mock_agent = MagicMock()
        test_scheduler = AsyncIOScheduler()

        with patch("miragen.app.load_profile", return_value=profile), \
             patch("miragen.app.build_agent", return_value=(mock_agent, None)), \
             patch("miragen.app._load_file_secrets"), \
             patch.object(app_module, "_scheduler", test_scheduler):
            async with app_module.lifespan(app):
                job_ids = {job.id for job in test_scheduler.get_jobs()}

        assert "test-agent:startup:0" in job_ids
        assert "test-agent:startup:1" in job_ids

    async def test_existing_cron_only_profile_unchanged(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        profile = _make_profile(
            mode="autonomous",
            triggers=[{"type": "cron", "schedule": "0 9 * * 1-5", "default_prompt": "Briefing."}],
        )
        mock_agent = MagicMock()
        test_scheduler = AsyncIOScheduler()

        with patch("miragen.app.load_profile", return_value=profile), \
             patch("miragen.app.build_agent", return_value=(mock_agent, None)), \
             patch("miragen.app._load_file_secrets"), \
             patch.object(app_module, "_scheduler", test_scheduler):
            async with app_module.lifespan(app):
                job_ids = {job.id for job in test_scheduler.get_jobs()}

        assert job_ids == {"test-agent:cron"}


class TestRunRecordsIntegration:
    async def test_run_creates_succeeded_record_with_run_id(self, store_client):
        resp = await store_client.post("/run", json={"prompt": "hi"})
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]
        assert run_id is not None

        runs = app_module._run_store.list()
        assert len(runs) == 1
        assert runs[0].run_id == run_id
        assert runs[0].status == "succeeded"
        assert runs[0].trigger == "http"

    async def test_failed_run_records_error_and_still_returns_500(self, profile, tmp_path):
        from miragen.runs import RunStore

        agent = MagicMock()
        agent.run = AsyncMock(side_effect=RuntimeError("boom"))
        app_module._profile = profile
        app_module._agent = agent
        app_module._run_store = RunStore(root=tmp_path)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run", json={"prompt": "hi"})

        assert resp.status_code == 500
        runs = app_module._run_store.list()
        assert len(runs) == 1
        assert runs[0].status == "failed"

    async def test_run_async_returns_202_and_reaches_terminal_state(self, store_client):
        resp = await store_client.post("/run/async", json={"prompt": "hi"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "running"
        run_id = body["run_id"]

        record = app_module._run_store.get(run_id)
        for _ in range(100):
            if record.status != "running":
                break
            await asyncio.sleep(0)
            record = app_module._run_store.get(run_id)

        assert record.status == "succeeded"
        assert record.output == "agent output"
        assert record.trigger == "http_async"

    async def test_health_includes_last_run(self, store_client):
        await store_client.post("/run", json={"prompt": "hi"})
        resp = await store_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["last_run"]["status"] == "succeeded"

    async def test_health_last_run_null_when_no_runs(self, client):
        resp = await client.get("/health")
        assert resp.json()["last_run"] is None


class TestRunsListEndpoint:
    async def test_respects_limit(self, store_client):
        for i in range(3):
            await store_client.post("/run", json={"prompt": f"run {i}"})
        resp = await store_client.get("/runs", params={"limit": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert len(data["runs"]) == 2

    async def test_filters_by_status(self, store_client):
        await store_client.post("/run", json={"prompt": "ok"})
        resp = await store_client.get("/runs", params={"status": "succeeded"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["runs"][0]["status"] == "succeeded"

    async def test_no_run_store_returns_503(self, client):
        resp = await client.get("/runs")
        assert resp.status_code == 503


class TestGetRunEndpoint:
    async def test_get_by_id(self, store_client):
        create_resp = await store_client.post("/run", json={"prompt": "hi"})
        run_id = create_resp.json()["run_id"]

        resp = await store_client.get(f"/runs/{run_id}")
        assert resp.status_code == 200
        assert resp.json()["run_id"] == run_id
        assert resp.json()["prompt"]

    async def test_unknown_id_returns_404_with_recent(self, store_client):
        await store_client.post("/run", json={"prompt": "hi"})
        resp = await store_client.get("/runs/deadbeefdeadbeef")
        assert resp.status_code == 404
        assert "recent" in resp.json()["detail"]

    async def test_ambiguous_prefix_returns_404_with_candidates(self, store_client, tmp_path):
        from datetime import datetime, timedelta, timezone
        from miragen.models import RunRecord

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        r1 = RunRecord(
            run_id="aaaaaaaa1111111111111111111111aa",
            agent_name="test-agent", trigger="http", status="succeeded",
            prompt="1", started_at=base,
        )
        r2 = RunRecord(
            run_id="aaaaaaaa2222222222222222222222bb",
            agent_name="test-agent", trigger="http", status="succeeded",
            prompt="2", started_at=base + timedelta(seconds=1),
        )
        app_module._run_store._write(r1)
        app_module._run_store._write(r2)

        resp = await store_client.get("/runs/aaaaaaaa")
        assert resp.status_code == 404
        assert len(resp.json()["detail"]["candidates"]) == 2


class TestApprovalsEndpoints:
    @pytest.fixture(autouse=True)
    def fresh_broker(self):
        import miragen.broker as broker_module

        original = broker_module._broker
        broker_module._broker = broker_module.ApprovalBroker()
        yield
        broker_module._broker = original

    async def test_list_approvals_empty(self, client):
        resp = await client.get("/approvals")
        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "approvals": []}

    async def test_pending_approval_appears_in_list(self, client):
        from miragen.broker import get_broker
        from miragen.models import ApprovalRequest

        request = ApprovalRequest(
            agent_name="test-agent", tool_name="delete_file", tool_args={"path": "/x"}, request_id="r1",
        )
        get_broker().submit(request, timeout_s=30)

        resp = await client.get("/approvals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["approvals"][0]["request"]["tool_name"] == "delete_file"
        assert data["approvals"][0]["request"]["tool_args"] == {"path": "/x"}

    async def test_resolve_approved_unblocks_the_waiting_future(self, client):
        from miragen.broker import get_broker
        from miragen.models import ApprovalRequest

        request = ApprovalRequest(agent_name="test-agent", tool_name="delete_file", tool_args={}, request_id="r1")
        future = get_broker().submit(request, timeout_s=30)

        resp = await client.post("/approvals/r1", json={"approved": True})
        assert resp.status_code == 200
        assert resp.json() == {"resolved": True}

        response = await future
        assert response.approved is True

    async def test_resolve_denied_with_reason_reaches_the_future(self, client):
        from miragen.broker import get_broker
        from miragen.models import ApprovalRequest

        request = ApprovalRequest(agent_name="test-agent", tool_name="delete_file", tool_args={}, request_id="r1")
        future = get_broker().submit(request, timeout_s=30)

        resp = await client.post("/approvals/r1", json={"approved": False, "prompt": "reason"})
        assert resp.status_code == 200

        response = await future
        assert response.approved is False
        assert response.prompt == "reason"

    async def test_resolve_unknown_id_returns_404_and_leaves_others_pending(self, client):
        from miragen.broker import get_broker
        from miragen.models import ApprovalRequest

        request = ApprovalRequest(agent_name="test-agent", tool_name="delete_file", tool_args={}, request_id="r1")
        get_broker().submit(request, timeout_s=30)

        resp = await client.post("/approvals/does-not-exist", json={"approved": True})
        assert resp.status_code == 404
        assert resp.json()["detail"]["pending"] == ["r1"]

        still_pending = await client.get("/approvals")
        assert still_pending.json()["count"] == 1

    async def test_health_includes_pending_approvals_count(self, client):
        from miragen.broker import get_broker
        from miragen.models import ApprovalRequest

        get_broker().submit(
            ApprovalRequest(agent_name="test-agent", tool_name="t", tool_args={}, request_id="r1"),
            timeout_s=30,
        )

        resp = await client.get("/health")
        assert resp.json()["pending_approvals"] == 1

    async def test_end_to_end_gated_tool_call_via_queue_mode_endpoint(self):
        """A gated tool call submits to the broker; GET /approvals surfaces it;
        POST /approvals/{id} against the real HTTP endpoint unblocks the run."""
        from miragen.approval import _run_approval_gate
        import miragen.factory as factory_module

        profile = _make_profile(
            approval_required=["delete_*"], approval_mode="queue", approval_timeout_s=30,
        )
        call = MagicMock()
        call.tool_name = "delete_file"
        call.args_as_dict = MagicMock(return_value={"path": "/data"})
        handler = AsyncMock(return_value="deleted /data")
        factory_module._approval_handler = None

        async def approve_via_endpoint():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                for _ in range(200):
                    pending = (await c.get("/approvals")).json()["approvals"]
                    if pending:
                        request_id = pending[0]["request"]["request_id"]
                        resp = await c.post(f"/approvals/{request_id}", json={"approved": True})
                        assert resp.status_code == 200
                        return
                    await asyncio.sleep(0)
            pytest.fail("no pending approval appeared")

        result, _ = await asyncio.gather(
            _run_approval_gate(profile, call, {}, handler),
            approve_via_endpoint(),
        )

        assert result == "deleted /data"


class TestBudgetGuardrails:
    def _seed_usage(self, tmp_path, *, started_at, input_tokens, output_tokens, agent_name="test-agent"):
        from miragen.models import RunRecord, RunUsage
        from miragen.runs import RunStore

        store = RunStore(root=tmp_path)
        store._write(RunRecord(
            run_id=f"{abs(hash(started_at)) % 10**32:032x}",
            agent_name=agent_name, trigger="http", status="succeeded",
            prompt="p", started_at=started_at,
            usage=RunUsage(requests=1, input_tokens=input_tokens, output_tokens=output_tokens),
        ))
        return store

    def _midnight_utc(self):
        return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    async def test_run_returns_429_when_daily_budget_exceeded(self, profile_with_daily_limit, tmp_path):
        store = self._seed_usage(tmp_path, started_at=self._midnight_utc(), input_tokens=80, output_tokens=30)
        app_module._profile = profile_with_daily_limit
        app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result()))
        app_module._run_store = store

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run", json={"prompt": "hi"})

        assert resp.status_code == 429
        assert "110/100" in resp.json()["detail"]
        assert "00:00 UTC" in resp.json()["detail"]
        app_module._agent.run.assert_not_called()

    async def test_run_succeeds_when_under_budget(self, profile_with_daily_limit, tmp_path):
        store = self._seed_usage(tmp_path, started_at=self._midnight_utc(), input_tokens=10, output_tokens=5)
        app_module._profile = profile_with_daily_limit
        app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result()))
        app_module._run_store = store

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run", json={"prompt": "hi"})

        assert resp.status_code == 200

    async def test_run_async_returns_429_when_exceeded(self, profile_with_daily_limit, tmp_path):
        store = self._seed_usage(tmp_path, started_at=self._midnight_utc(), input_tokens=200, output_tokens=0)
        app_module._profile = profile_with_daily_limit
        app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result()))
        app_module._run_store = store

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run/async", json={"prompt": "hi"})

        assert resp.status_code == 429

    async def test_yesterdays_usage_does_not_count(self, profile_with_daily_limit, tmp_path):
        yesterday = self._midnight_utc() - timedelta(hours=1)
        store = self._seed_usage(tmp_path, started_at=yesterday, input_tokens=10_000, output_tokens=10_000)
        app_module._profile = profile_with_daily_limit
        app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result()))
        app_module._run_store = store

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/run", json={"prompt": "hi"})

        assert resp.status_code == 200

    async def test_profile_without_limits_unaffected(self, store_client):
        resp = await store_client.post("/run", json={"prompt": "hi"})
        assert resp.status_code == 200

    async def test_scheduled_run_skipped_when_budget_exceeded(self, tmp_path):
        profile = _make_profile(
            mode="autonomous", triggers=[{"type": "cron", "schedule": "0 * * * *"}],
            limits={"tokens_per_day": 100},
        )
        store = self._seed_usage(tmp_path, started_at=self._midnight_utc(), input_tokens=200, output_tokens=0)
        app_module._profile = profile
        app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result()))
        app_module._run_store = store

        with patch("miragen.app._handle_on_complete", AsyncMock()) as mock_oc:
            from miragen.app import run_agent_scheduled
            await run_agent_scheduled("Run now.")

        app_module._agent.run.assert_not_called()
        mock_oc.assert_not_called()
        assert len(store.list()) == 1  # only the seeded record — the skipped run created none

    async def test_scheduled_run_proceeds_when_under_budget(self, tmp_path):
        profile = _make_profile(
            mode="autonomous", triggers=[{"type": "cron", "schedule": "0 * * * *"}],
            limits={"tokens_per_day": 100},
        )
        store = self._seed_usage(tmp_path, started_at=self._midnight_utc(), input_tokens=10, output_tokens=5)
        app_module._profile = profile
        app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result()))
        app_module._run_store = store

        with patch("miragen.app._handle_on_complete", AsyncMock()) as mock_oc:
            from miragen.app import run_agent_scheduled
            await run_agent_scheduled("Run now.")

        app_module._agent.run.assert_called_once()
        mock_oc.assert_awaited_once()

    async def test_scheduled_run_notifies_on_exceeded_notify(self, tmp_path):
        profile = _make_profile(
            mode="autonomous", triggers=[{"type": "cron", "schedule": "0 * * * *"}],
            limits={"tokens_per_day": 100, "on_exceeded": "notify"},
            on_complete={"notify": "telegram"},
        )
        store = self._seed_usage(tmp_path, started_at=self._midnight_utc(), input_tokens=200, output_tokens=0)
        app_module._profile = profile
        app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result()))
        app_module._run_store = store

        notify_handler = AsyncMock()
        with patch("miragen.app.registered_handlers", return_value={"telegram": notify_handler}):
            from miragen.app import run_agent_scheduled
            await run_agent_scheduled("Run now.")

        notify_handler.assert_awaited_once()
        agent_name, message = notify_handler.call_args[0]
        assert agent_name == "test-agent"
        assert "budget" in message.lower()

    async def test_scheduled_run_skip_mode_does_not_notify(self, tmp_path):
        profile = _make_profile(
            mode="autonomous", triggers=[{"type": "cron", "schedule": "0 * * * *"}],
            limits={"tokens_per_day": 100, "on_exceeded": "skip"},
            on_complete={"notify": "telegram"},
        )
        store = self._seed_usage(tmp_path, started_at=self._midnight_utc(), input_tokens=200, output_tokens=0)
        app_module._profile = profile
        app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result()))
        app_module._run_store = store

        notify_handler = AsyncMock()
        with patch("miragen.app.registered_handlers", return_value={"telegram": notify_handler}):
            from miragen.app import run_agent_scheduled
            await run_agent_scheduled("Run now.")

        notify_handler.assert_not_called()


class TestLoadFileSecrets:
    def test_loads_secret_into_env(self, tmp_path):
        secret = tmp_path / "key"
        secret.write_text("sk-secret-value\n")
        env = {"MY_API_KEY_FILE": str(secret)}
        with patch.dict(os.environ, env, clear=False):
            _load_file_secrets()
            assert os.environ["MY_API_KEY"] == "sk-secret-value"
            assert "MY_API_KEY_FILE" not in os.environ

    def test_removes_file_var_after_load(self, tmp_path):
        secret = tmp_path / "key"
        secret.write_text("value")
        with patch.dict(os.environ, {"SOME_TOKEN_FILE": str(secret)}, clear=False):
            _load_file_secrets()
            assert "SOME_TOKEN_FILE" not in os.environ

    def test_strips_whitespace(self, tmp_path):
        secret = tmp_path / "key"
        secret.write_text("  padded-value  \n")
        with patch.dict(os.environ, {"FOO_FILE": str(secret)}, clear=False):
            _load_file_secrets()
            assert os.environ["FOO"] == "padded-value"

    def test_missing_file_skipped_gracefully(self):
        with patch.dict(os.environ, {"GHOST_KEY_FILE": "/nonexistent/path"}, clear=False):
            _load_file_secrets()
            assert "GHOST_KEY" not in os.environ
            assert "GHOST_KEY_FILE" in os.environ

    def test_multiple_secrets_loaded(self, tmp_path):
        (tmp_path / "a").write_text("val-a")
        (tmp_path / "b").write_text("val-b")
        env = {
            "SERVICE_A_KEY_FILE": str(tmp_path / "a"),
            "SERVICE_B_KEY_FILE": str(tmp_path / "b"),
        }
        with patch.dict(os.environ, env, clear=False):
            _load_file_secrets()
            assert os.environ["SERVICE_A_KEY"] == "val-a"
            assert os.environ["SERVICE_B_KEY"] == "val-b"

    def test_non_file_vars_untouched(self, tmp_path):
        with patch.dict(os.environ, {"NORMAL_VAR": "hello"}, clear=False):
            _load_file_secrets()
            assert os.environ["NORMAL_VAR"] == "hello"

    def test_anthropic_pattern(self, tmp_path):
        secret = tmp_path / "anthropic_key"
        secret.write_text("sk-ant-real-key")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY_FILE": str(secret)}, clear=False):
            _load_file_secrets()
            assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-real-key"
            assert "ANTHROPIC_API_KEY_FILE" not in os.environ

    def test_permission_error_logged_not_raised(self, tmp_path):
        secret = tmp_path / "key"
        secret.write_text("secret-value")
        with patch.dict(os.environ, {"LOCKED_KEY_FILE": str(secret)}, clear=False):
            with patch("pathlib.Path.read_text", side_effect=PermissionError("access denied")):
                _load_file_secrets()
            assert "LOCKED_KEY" not in os.environ
            assert "LOCKED_KEY_FILE" in os.environ
