import os
import sys
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


@pytest.fixture
def profile():
    return _make_profile()


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

        async def capture(prompt, use_history=False):
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

        async def capture(prompt, use_history=False):
            captured["prompt"] = prompt
            return "ok"

        with patch("miragen.app.run_agent", capture):
            await client.post("/run", json={"prompt": "raw prompt"})

        assert "raw prompt" in captured["prompt"]

    async def test_timestamp_injected_by_default(self, client):
        captured = {}

        async def capture(prompt, use_history=False):
            captured["prompt"] = prompt
            return "ok"

        with patch("miragen.app.run_agent", capture):
            await client.post("/run", json={"prompt": "hello"})

        import re
        assert re.search(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]", captured["prompt"])

    async def test_timestamp_suppressed_when_disabled(self, mock_agent):
        profile = _make_profile(inject_timestamp=False)
        captured = {}

        async def capture(prompt, use_history=False):
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
