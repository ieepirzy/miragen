"""The §18.2 telemetry fixes (memory pass PR 5, miragen half): streaming
runs under a run span, paginated executor export (no silent truncation),
honest tool durations, command redaction, export visibility on /health.
"""

import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock

import miragen.app  # noqa: F401
app_module = sys.modules["miragen.app"]
from miragen.executor import ExecutorResult
from miragen.models import AgentProfile
from miragen.telemetry import ExportStats
from tests.test_otel_export import make_telemetry as _telemetry, spans_by_name


def _make_profile(**kw):
    return AgentProfile.model_validate({
        "name": "test-agent", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
        **kw,
    })


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._telemetry = None
    app_module._executor = None
    app_module._memory = None


class TestStreamRunSpan:
    async def test_stream_runs_under_a_run_span_with_identity(self, tmp_path):
        from httpx import ASGITransport, AsyncClient

        from miragen.app import app
        from miragen.runs import RunStore

        telemetry, exporter = _telemetry()
        app_module._profile = _make_profile()
        app_module._run_store = RunStore(root=tmp_path / "runs")
        app_module._telemetry = telemetry

        class _StreamCtx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def stream_text(self, delta=True):
                yield "hello"

            def all_messages(self):
                return []

            @property
            def usage(self):
                u = MagicMock()
                u.requests, u.input_tokens, u.output_tokens = 1, 10, 5
                return u

        agent = MagicMock()
        agent.run_stream = MagicMock(return_value=_StreamCtx())
        app_module._agent = agent

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post("/run/stream", json={"prompt": "hi"})
            body = resp.text
        assert "hello" in body
        telemetry.provider.force_flush()

        spans = spans_by_name(exporter)
        assert "agent run" in spans, list(spans)
        (root,) = spans["agent run"]
        run_id = resp.headers["X-Miragen-Run-Id"]
        assert root.attributes["mira.run.id"] == run_id
        assert root.attributes["mira.agent.tier"] == "model"
        assert root.attributes["gen_ai.usage.input_tokens"] == 10

    async def test_stream_failure_still_closes_the_span(self, tmp_path):
        from httpx import ASGITransport, AsyncClient

        from miragen.app import app
        from miragen.runs import RunStore

        telemetry, exporter = _telemetry()
        app_module._profile = _make_profile()
        app_module._run_store = RunStore(root=tmp_path / "runs")
        app_module._telemetry = telemetry

        class _ExplodingCtx:
            async def __aenter__(self):
                raise RuntimeError("stream exploded")

            async def __aexit__(self, *exc):
                return False

        agent = MagicMock()
        agent.run_stream = MagicMock(return_value=_ExplodingCtx())
        app_module._agent = agent

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            with pytest.raises(RuntimeError):
                await c.post("/run/stream", json={"prompt": "hi"})
        telemetry.provider.force_flush()
        assert "agent run" in spans_by_name(exporter)  # closed, not leaked


class TestPaginatedExecutorExport:
    async def test_multipage_streams_export_completely(self):
        telemetry = MagicMock()
        telemetry.run_span = MagicMock()
        emitted = {}
        telemetry.emit_executor_turn = MagicMock(
            side_effect=lambda events, **kw: emitted.update(count=len(events))
        )

        pages = [
            SimpleNamespace(events=[{"type": "x", "seq": i} for i in range(10_000)],
                            has_more=True, next_after=10_000),
            SimpleNamespace(events=[{"type": "x", "seq": i} for i in range(10_000, 12_500)],
                            has_more=False, next_after=12_500),
        ]
        executor = MagicMock()
        executor.spec.turn_timeout_s = None
        executor.spec.executor = "codex"
        executor.spec.workspace_root = "/tmp/ws"
        executor.last_seq = MagicMock(return_value=0)
        executor.read_events_page = MagicMock(side_effect=pages)
        executor.run_job = AsyncMock(return_value=ExecutorResult(status="succeeded", output="ok"))

        app_module._profile = _make_profile()
        app_module._executor = executor
        app_module._telemetry = telemetry

        await app_module._run_executor_turn("prompt", None)

        assert emitted["count"] == 12_500  # nothing silently dropped
        # And the cursor walked the watermark, not offset zero twice.
        second_call = executor.read_events_page.call_args_list[1]
        assert second_call.kwargs.get("after") == 10_000


class TestHonestToolSpans:
    def test_reported_duration_widens_the_span(self):
        telemetry, exporter = _telemetry()
        ts = datetime.now(timezone.utc)
        telemetry.emit_executor_turn(
            [{
                "type": "item.completed", "ts": ts.isoformat(),
                "item": {"type": "command_execution", "name": "shell",
                         "exit_code": 0, "duration_ms": 1500,
                         "command": "secret --token abc"},
            }],
            run_id=uuid.uuid4().hex, trigger="http", executor="codex",
            status="succeeded",
        )
        telemetry.provider.force_flush()
        (tool,) = spans_by_name(exporter)["executor tool"]
        assert tool.end_time - tool.start_time == int(1.5e9)
        assert "miragen.tool.duration_unknown" not in tool.attributes
        # Redaction is unconditional, duration or not.
        assert "miragen.tool.command" not in tool.attributes
        assert "secret" not in str(tool.attributes)


class TestHealthExportStats:
    async def test_health_reports_export_outcomes(self):
        from httpx import ASGITransport, AsyncClient

        from miragen.app import app

        telemetry = MagicMock()
        telemetry.export_stats = ExportStats(
            attempted_batches=7, failed_batches=2, failed_spans=90
        )
        app_module._profile = _make_profile()
        app_module._telemetry = telemetry

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            body = (await c.get("/health")).json()
        assert body["telemetry"]["export"] == {
            "attempted_batches": 7, "failed_batches": 2, "failed_spans": 90,
        }

    async def test_unconfigured_telemetry_reports_null_export(self):
        from httpx import ASGITransport, AsyncClient

        from miragen.app import app

        app_module._profile = _make_profile()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            body = (await c.get("/health")).json()
        assert body["telemetry"] == {"otlp_configured": False, "export": None}
