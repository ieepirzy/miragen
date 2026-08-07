"""OTLP telemetry (miragen/telemetry.py): mira-sdk-compatible span export.

Everything runs against an InMemorySpanExporter — no network, no OTLP
endpoint. What's asserted is the contract the backend depends on: run
identity on EVERY span (the run-context processor), GenAI-semconv usage
attributes, executor event translation with the events' own timestamps,
and the factory wiring pydantic-ai instrumentation to miragen's provider.
"""

import sys
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import miragen.app  # noqa: F401

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.runs import RunStore
from miragen.telemetry import MiragenTelemetry, telemetry_from_env

from tests.test_executor import _executor_profile, default_events, make_executor
from tests.test_model_tier_stub import StubAgent, model_profile


@pytest.fixture(autouse=True)
def reset_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._executor = None
    app_module._telemetry = None


def make_telemetry(**kw):
    exporter = InMemorySpanExporter()
    telemetry = MiragenTelemetry(
        endpoint="unused",
        agent_name="test-agent",
        agent_mode="interactive",
        span_exporter=exporter,
        **kw,
    )
    return telemetry, exporter


def spans_by_name(exporter):
    spans = {}
    for span in exporter.get_finished_spans():
        spans.setdefault(span.name, []).append(span)
    return spans


# ── Run identity on every span ───────────────────────────────────────────────


def test_run_span_stamps_identity_on_children():
    telemetry, exporter = make_telemetry()
    tracer = telemetry.provider.get_tracer("fake-pydantic-ai")
    with telemetry.run_span("agent run", run_id="run123", trigger="http", tier="model"):
        # A span pydantic-ai's own instrumentation would open — this module
        # never touches it, the processor must stamp it anyway.
        with tracer.start_as_current_span("chat claude-x"):
            pass
    telemetry.provider.force_flush()

    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} == {"agent run", "chat claude-x"}
    for span in spans:
        assert span.attributes["mira.run.id"] == "run123"
        assert span.attributes["mira.run.trigger"] == "http"
        assert span.attributes["mira.agent.tier"] == "model"


def test_spans_outside_a_run_carry_no_run_identity():
    telemetry, exporter = make_telemetry()
    with telemetry.provider.get_tracer("t").start_as_current_span("orphan"):
        pass
    telemetry.provider.force_flush()
    (span,) = exporter.get_finished_spans()
    assert "mira.run.id" not in span.attributes


def test_resource_attributes_match_mira_sdk_vocabulary():
    telemetry, exporter = make_telemetry(deployment_environment="staging")
    with telemetry.run_span("agent run", run_id="r1"):
        pass
    telemetry.provider.force_flush()
    (span,) = exporter.get_finished_spans()
    resource = span.resource.attributes
    assert resource["service.name"] == "miragen"
    assert resource["mira.agent.id"] == "test-agent"
    assert resource["deployment.environment"] == "staging"


def test_run_span_records_exception_and_error_status():
    telemetry, exporter = make_telemetry()
    with pytest.raises(RuntimeError):
        with telemetry.run_span("agent run", run_id="r1"):
            raise RuntimeError("model exploded")
    telemetry.provider.force_flush()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR


# ── Executor event translation ───────────────────────────────────────────────


def _stamped(events, start):
    """Give a normalized event list the ts/seq envelope the durable stream has."""
    out = []
    for i, event in enumerate(events):
        out.append({
            **event,
            "seq": i + 1,
            "ts": (start + timedelta(seconds=i)).isoformat(),
        })
    return out


def test_emit_executor_turn_translates_events():
    telemetry, exporter = make_telemetry()
    start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    events = _stamped(
        [
            {"type": "lifecycle.setup.started", "phase": "workspace"},
            {"type": "lifecycle.setup.completed", "phase": "workspace", "duration_ms": 900},
            *default_events(),
            {"type": "lifecycle.harvest.completed", "duration_ms": 40, "diff_bytes": 512,
             "change_categories": ["code"], "affected_repositories": []},
        ],
        start,
    )
    from miragen.models import RunUsage

    telemetry.emit_executor_turn(
        events,
        run_id="runX",
        trigger="launch",
        executor="codex",
        status="succeeded",
        usage=RunUsage(requests=1, input_tokens=100, output_tokens=50),
        started_at=start,
        finished_at=start + timedelta(seconds=10),
    )
    telemetry.provider.force_flush()

    spans = spans_by_name(exporter)
    assert set(spans) == {"executor turn", "workspace setup", "executor tool", "diff harvest"}

    (root,) = spans["executor turn"]
    assert root.attributes["gen_ai.usage.input_tokens"] == 100
    assert root.attributes["gen_ai.usage.output_tokens"] == 50
    assert root.attributes["miragen.run.status"] == "succeeded"
    assert root.attributes["miragen.executor"] == "codex"
    # Root spans the real turn interval, from the caller's clock.
    assert root.end_time - root.start_time == int(10e9)

    (tool,) = spans["executor tool"]
    assert tool.attributes["miragen.tool.type"] == "command_execution"
    assert tool.attributes["miragen.tool.command"] == "pytest -q"
    assert tool.parent.span_id == root.context.span_id

    # Run identity on every synthesized span too.
    for group in spans.values():
        for span in group:
            assert span.attributes["mira.run.id"] == "runX"
            assert span.attributes["mira.agent.tier"] == "executor"

    # Duration-bearing events get their duration back-dated from ts.
    (setup,) = spans["workspace setup"]
    assert setup.end_time - setup.start_time == int(0.9e9)


def test_emit_executor_turn_marks_failures():
    telemetry, exporter = make_telemetry()
    start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    events = _stamped(
        [
            {"type": "item.completed", "item": {"type": "command_execution",
                                                "command": "pytest", "exit_code": 1}},
            {"type": "turn.failed", "error": {"message": "boom"}},
        ],
        start,
    )
    telemetry.emit_executor_turn(
        events, run_id="runY", trigger="http", executor="codex",
        status="failed", exit_reason="crash",
        started_at=start, finished_at=start + timedelta(seconds=2),
    )
    telemetry.provider.force_flush()
    spans = spans_by_name(exporter)
    (root,) = spans["executor turn"]
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["miragen.run.exit_reason"] == "crash"
    (tool,) = spans["executor tool"]
    assert tool.status.status_code == StatusCode.ERROR
    assert "turn failed" in spans


def test_emit_executor_turn_never_raises(caplog):
    telemetry, _ = make_telemetry()
    # Garbage events must degrade to a warning, not break the run path.
    telemetry.emit_executor_turn(
        [{"type": "item.completed", "item": 42, "ts": object()}],
        run_id="r", trigger=None, executor="codex", status="succeeded",
    )


def test_agent_message_content_stays_out_of_spans():
    telemetry, exporter = make_telemetry()
    start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    events = _stamped(
        [{"type": "item.completed", "item": {"type": "agent_message", "text": "secret sauce"}}],
        start,
    )
    telemetry.emit_executor_turn(
        events, run_id="r", trigger=None, executor="codex", status="succeeded",
        started_at=start, finished_at=start,
    )
    telemetry.provider.force_flush()
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["executor turn"]
    assert not any("secret sauce" in str(v) for s in spans for v in s.attributes.values())


# ── Env wiring ───────────────────────────────────────────────────────────────


def test_telemetry_from_env_off_by_default(monkeypatch):
    monkeypatch.delenv("MIRAGEN_OTLP_ENDPOINT", raising=False)
    assert telemetry_from_env(model_profile()) is None


def test_telemetry_from_env_configures_endpoint(monkeypatch):
    monkeypatch.setenv("MIRAGEN_OTLP_ENDPOINT", "https://langfuse.local/api/public/otel/v1/traces")
    monkeypatch.setenv("MIRAGEN_OTLP_TOKEN", "tok")
    telemetry = telemetry_from_env(model_profile())
    assert telemetry is not None
    telemetry.shutdown(timeout_millis=10)


# ── Factory wiring ───────────────────────────────────────────────────────────


def test_build_agent_wires_instrumentation_capability():
    from pydantic_ai.capabilities import Instrumentation

    from miragen.factory import build_agent

    def flatten(capability):
        children = getattr(capability, "capabilities", None) or []
        return [capability, *(c for child in children for c in flatten(child))]

    telemetry, _ = make_telemetry()
    profile = model_profile()
    profile.spec.model = "test"  # pydantic-ai TestModel — no network
    agent, _limits = build_agent(profile, telemetry=telemetry)
    assert any(isinstance(c, Instrumentation) for c in flatten(agent.root_capability))

    agent_off, _ = build_agent(profile)
    assert not any(isinstance(c, Instrumentation) for c in flatten(agent_off.root_capability))


# ── App integration, both tiers ──────────────────────────────────────────────


async def test_executor_run_exports_turn_spans(tmp_path):
    profile = _executor_profile()
    executor = make_executor(profile, tmp_path)
    telemetry, exporter = make_telemetry()
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    app_module._telemetry = telemetry

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "fix it"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    telemetry.provider.force_flush()
    spans = spans_by_name(exporter)
    (root,) = spans["executor turn"]
    assert root.attributes["mira.run.id"] == run_id
    assert root.attributes["miragen.run.status"] == "succeeded"
    assert "executor tool" in spans


async def test_model_run_exports_run_span(tmp_path):
    telemetry, exporter = make_telemetry()
    app_module._profile = model_profile()
    app_module._agent = StubAgent()
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    app_module._telemetry = telemetry

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "go"})
    assert resp.status_code == 200

    telemetry.provider.force_flush()
    spans = spans_by_name(exporter)
    (root,) = spans["agent run"]
    assert root.attributes["mira.run.id"] == resp.json()["run_id"]
    assert root.attributes["mira.agent.tier"] == "model"
    assert root.attributes["gen_ai.usage.input_tokens"] == 120
