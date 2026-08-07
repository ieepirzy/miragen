"""OTLP span export for miragen runs — the mira-sdk-compatible telemetry seam.

One `MiragenTelemetry` instance is a TracerProvider scoped to this agent
process (miragen serves one agent per container; runs vary per span, the
agent doesn't). It speaks the same wire format and attribute vocabulary as
`mira_sdk.telemetry.MiraTelemetry` — OTLP/HTTP, bearer-token auth, `mira.*`
attributes — so miragen spans and mira-sdk spans land in the same backend
(Langfuse's `/api/public/otel`, an OTel Collector, anything OTLP/HTTP)
filterable by one vocabulary.

Non-blocking is structural, not a convention to remember: this module only
ever wires a `BatchSpanProcessor` (queue + background export thread), never
a `SimpleSpanProcessor`. A telemetry backend being unreachable must never
fail or stall the run that emitted the span.

Run identity on EVERY span: `mira.run.id` (and trigger/tier attributes) are
stamped by a span processor from a contextvar set for the duration of each
run — not attached per-span by callers. Attaching them only to the root span
is the mistake that breaks cross-span filtering and aggregation once the
backend is in front of you (the executor-tier spans synthesized here get
them explicitly, and every pydantic-ai-instrumented span inherits them via
the processor).

Both tiers, one trace model:
- model tier: pydantic-ai's own OTel instrumentation (GenAI semconv) is
  wired with this provider via `InstrumentationSettings` in the factory;
  `run_span()` wraps the run so those spans nest under one root.
- executor tier: the self-harnessed loop can't be instrumented from here,
  but its durable event stream is translated span-for-span after each turn
  (`emit_executor_turn`) with the events' own timestamps — the harder tier
  to observe live is the easier one to trace faithfully post-hoc.

Deliberately absent (for now, by decision): any LLM-in-the-loop analytics —
no judge scores, no model-authored classifications. Spans carry only
mechanical facts already in the run record / event stream.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Sequence

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, Status, StatusCode

from miragen.models import AgentProfile, RunUsage

logger = logging.getLogger("miragen.telemetry")

# Attribute value caps: spans carry mechanical facts, never payloads — large
# content (diffs, outputs, files) belongs in an artifact store referenced by
# hash, not in a span attribute (miradb #406 decision 11).
_ATTR_MAX = 500

_DEFAULT_MAX_QUEUE_SIZE = 2048
_DEFAULT_MAX_EXPORT_BATCH_SIZE = 512
_DEFAULT_SCHEDULE_DELAY_MILLIS = 5000
_DEFAULT_EXPORT_TIMEOUT_MILLIS = 30000

# Run-scoped attributes stamped onto every span started while a run is
# current (see _RunContextProcessor). Set via run_attributes()/run_span().
_run_attributes: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "miragen_run_attributes", default=None
)


@dataclass
class ExportStats:
    """Export-level bookkeeping, readable at any time — the actionable half
    of "did my telemetry get out" (same scope decision as mira-sdk: queue
    overflow is the SDK logger's job, not re-counted here)."""

    attempted_batches: int = 0
    failed_batches: int = 0
    failed_spans: int = 0


class _RunContextProcessor(SpanProcessor):
    """Stamps the current run's attributes onto every span at start.

    This is what makes `mira.run.id` filterable across a whole trace instead
    of living only on the root span — including spans pydantic-ai's own
    instrumentation opens, which this module never touches directly."""

    def on_start(self, span: Span, parent_context=None) -> None:
        attrs = _run_attributes.get()
        if attrs:
            for key, value in attrs.items():
                if value is not None:
                    span.set_attribute(key, value)

    def on_end(self, span: ReadableSpan) -> None:  # pragma: no cover - no-op
        pass

    def shutdown(self) -> None:  # pragma: no cover - no-op
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # pragma: no cover
        return True


class _CountingExporter(SpanExporter):
    """Wraps a SpanExporter to maintain `ExportStats`. Delegates export
    behavior — including OTLPSpanExporter's own retry-with-backoff — to the
    wrapped exporter unchanged; this only observes the outcome."""

    def __init__(self, delegate: SpanExporter, stats: ExportStats) -> None:
        self._delegate = delegate
        self._stats = stats

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self._stats.attempted_batches += 1
        try:
            result = self._delegate.export(spans)
        except Exception:
            # Exporters are documented to return FAILURE rather than raise;
            # defensive backstop against one that doesn't.
            logger.warning("miragen telemetry export raised", exc_info=True)
            result = SpanExportResult.FAILURE
        if result != SpanExportResult.SUCCESS:
            self._stats.failed_batches += 1
            self._stats.failed_spans += len(spans)
        return result

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)


class MiragenTelemetry:
    """One TracerProvider for this agent process, exporting over OTLP/HTTP.

    Resource attributes carry the process-stable identity (service, agent,
    deployment environment); run-varying identity goes through the run
    contextvar so it reaches every span (see module docstring).
    """

    def __init__(
        self,
        *,
        endpoint: str,
        agent_name: str,
        agent_mode: str | None = None,
        service_name: str = "miragen",
        service_version: str | None = None,
        deployment_environment: str | None = None,
        token: str | None = None,
        auth_header: str | None = None,
        extra_resource_attributes: Mapping[str, str] | None = None,
        span_exporter: SpanExporter | None = None,
    ) -> None:
        """`endpoint` is the OTLP/HTTP traces endpoint (e.g. Langfuse's
        `https://langfuse.example/api/public/otel/v1/traces`, or an OTel
        Collector). `token` is sent as `Authorization: Bearer <token>`;
        `auth_header` is the verbatim Authorization value for backends that
        don't speak bearer (Langfuse OTLP ingest wants `Basic <base64>`)
        and wins over `token`. `span_exporter` overrides the OTLP exporter
        entirely (tests, or a non-OTLP destination) — when set,
        `endpoint`/`token`/`auth_header` are ignored."""
        attributes: dict[str, Any] = {
            "service.name": service_name,
            "mira.agent.id": agent_name,
        }
        if service_version is not None:
            attributes["service.version"] = service_version
        if deployment_environment is not None:
            attributes["deployment.environment"] = deployment_environment
        if agent_mode is not None:
            attributes["mira.agent.mode"] = agent_mode
        if extra_resource_attributes:
            attributes.update(extra_resource_attributes)

        self._stats = ExportStats()
        authorization = auth_header or (f"Bearer {token}" if token else None)
        delegate = span_exporter or OTLPSpanExporter(
            endpoint=endpoint,
            headers={"Authorization": authorization} if authorization else None,
        )
        exporter = _CountingExporter(delegate, self._stats)
        self.provider = TracerProvider(resource=Resource.create(attributes))
        self.provider.add_span_processor(_RunContextProcessor())
        self.provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=_DEFAULT_MAX_QUEUE_SIZE,
                max_export_batch_size=_DEFAULT_MAX_EXPORT_BATCH_SIZE,
                schedule_delay_millis=_DEFAULT_SCHEDULE_DELAY_MILLIS,
                export_timeout_millis=_DEFAULT_EXPORT_TIMEOUT_MILLIS,
            )
        )
        self._tracer = self.provider.get_tracer("miragen")

    @property
    def export_stats(self) -> ExportStats:
        return self._stats

    # ── Spans ──────────────────────────────────────────────────────────────

    @contextmanager
    def run_attributes(self, **attributes: Any) -> Iterator[None]:
        """Make `attributes` the current run identity: stamped onto every
        span started inside the with-block (None values dropped)."""
        token = _run_attributes.set({k: v for k, v in attributes.items() if v is not None})
        try:
            yield
        finally:
            _run_attributes.reset(token)

    @contextmanager
    def run_span(
        self,
        name: str,
        *,
        run_id: str,
        trigger: str | None = None,
        tier: str | None = None,
        **attributes: Any,
    ) -> Iterator[Span]:
        """Open the run's root span with the run identity current, so every
        child span — including pydantic-ai's own — carries it too. An
        exception inside the block is recorded and re-raised unchanged."""
        with self.run_attributes(
            **{"mira.run.id": run_id, "mira.run.trigger": trigger, "mira.agent.tier": tier}
        ):
            with self._tracer.start_as_current_span(name) as span:
                for key, value in attributes.items():
                    if value is not None:
                        span.set_attribute(key, value)
                yield span

    def shutdown(self, timeout_millis: int = 5000) -> None:
        """Flush queued spans (bounded by `timeout_millis`) and stop the
        background export thread. Call once, at process exit — the bound
        comes from force_flush; TracerProvider.shutdown() takes none."""
        self.provider.force_flush(timeout_millis)
        self.provider.shutdown()

    # ── Executor-turn translation ──────────────────────────────────────────

    def emit_executor_turn(
        self,
        events: list[dict[str, Any]],
        *,
        run_id: str,
        trigger: str | None,
        executor: str,
        status: str,
        exit_reason: str | None = None,
        usage: RunUsage | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        resume_count: int = 0,
    ) -> None:
        """Translate one executor turn's slice of the durable event stream
        into spans, using the events' own timestamps. Called after the turn
        (never inside it): translation cost and export queueing stay out of
        the execution path entirely, and a translation bug can only lose
        telemetry, never affect a run.

        Emits one root span for the turn and children for the mechanically
        interesting events: workspace setup, repo preparation, tool items,
        harvest, interventions. Message payloads are NOT copied into spans —
        content lives in the run record/event stream/artifact store.
        """
        try:
            self._emit_executor_turn(
                events,
                run_id=run_id,
                trigger=trigger,
                executor=executor,
                status=status,
                exit_reason=exit_reason,
                usage=usage,
                started_at=started_at,
                finished_at=finished_at,
                resume_count=resume_count,
            )
        except Exception:
            logger.warning("executor-turn telemetry translation failed", exc_info=True)

    def _emit_executor_turn(
        self,
        events: list[dict[str, Any]],
        *,
        run_id: str,
        trigger: str | None,
        executor: str,
        status: str,
        exit_reason: str | None,
        usage: RunUsage | None,
        started_at: datetime | None,
        finished_at: datetime | None,
        resume_count: int,
    ) -> None:
        times = [t for t in (_event_time_ns(e) for e in events) if t is not None]
        start_ns = _to_ns(started_at) or (min(times) if times else _now_ns())
        end_ns = _to_ns(finished_at) or (max(times) if times else start_ns)

        with self.run_attributes(
            **{
                "mira.run.id": run_id,
                "mira.run.trigger": trigger,
                "mira.agent.tier": "executor",
                "miragen.executor": executor,
            }
        ):
            root = self._tracer.start_span(
                "executor turn", kind=SpanKind.INTERNAL, start_time=start_ns
            )
            root.set_attribute("miragen.run.status", status)
            root.set_attribute("miragen.run.resume_count", resume_count)
            if exit_reason is not None:
                root.set_attribute("miragen.run.exit_reason", exit_reason)
            if usage is not None:
                # GenAI semconv usage names — the same vocabulary pydantic-ai's
                # instrumentation emits for the model tier.
                if usage.input_tokens is not None:
                    root.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
                if usage.output_tokens is not None:
                    root.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
                if usage.cached_input_tokens is not None:
                    root.set_attribute(
                        "gen_ai.usage.cached_input_tokens", usage.cached_input_tokens
                    )
            if status == "failed":
                root.set_status(Status(StatusCode.ERROR, exit_reason or "failed"))

            parent_ctx = trace.set_span_in_context(root)
            for event in events:
                self._emit_event_span(event, parent_ctx)
            root.end(end_time=end_ns)

    def _emit_event_span(self, event: dict[str, Any], parent_ctx: otel_context.Context) -> None:
        kind = event.get("type")
        ts_ns = _event_time_ns(event) or _now_ns()

        def _span(name: str, *, start_ns: int, attrs: dict[str, Any], error: str | None = None):
            span = self._tracer.start_span(name, context=parent_ctx, start_time=start_ns)
            for key, value in attrs.items():
                if value is not None:
                    span.set_attribute(key, _cap(value))
            if error is not None:
                span.set_status(Status(StatusCode.ERROR, _cap(error)))
            span.end(end_time=ts_ns)

        if kind == "lifecycle.setup.completed":
            duration_ms = event.get("duration_ms") or 0
            _span(
                "workspace setup",
                start_ns=ts_ns - int(duration_ms * 1e6),
                attrs={"miragen.setup.phase": event.get("phase")},
            )
        elif kind == "lifecycle.repo.prepared":
            duration_ms = event.get("duration_ms") or 0
            _span(
                "repository prepared",
                start_ns=ts_ns - int(duration_ms * 1e6),
                attrs={
                    "miragen.repo.name": event.get("name"),
                    "miragen.repo.ref": event.get("ref"),
                    "miragen.repo.commit": event.get("commit"),
                    "miragen.repo.writable": event.get("writable"),
                },
            )
        elif kind == "lifecycle.harvest.completed":
            duration_ms = event.get("duration_ms") or 0
            _span(
                "diff harvest",
                start_ns=ts_ns - int(duration_ms * 1e6),
                attrs={
                    "miragen.harvest.diff_bytes": event.get("diff_bytes"),
                    "miragen.harvest.change_categories": _join(event.get("change_categories")),
                    "miragen.harvest.affected_repositories": _join(
                        event.get("affected_repositories")
                    ),
                },
            )
        elif kind == "item.completed":
            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type in ("agent_message", "agent-message", "reasoning"):
                return  # content, not tool work — stays out of spans
            failed = (
                isinstance(item.get("exit_code"), int) and item["exit_code"] != 0
            ) or item.get("status") in ("failed", "error")
            _span(
                "executor tool",
                start_ns=ts_ns,
                attrs={
                    "miragen.tool.type": item_type,
                    "miragen.tool.name": item.get("name"),
                    "miragen.tool.command": item.get("command"),
                    "miragen.tool.exit_code": item.get("exit_code"),
                },
                error="tool failed" if failed else None,
            )
        elif kind == "intervention.requested":
            _span(
                "intervention requested",
                start_ns=ts_ns,
                attrs={
                    # The question text is payload (it can quote findings,
                    # paths, credentials); only mechanical facts export.
                    "miragen.intervention.id": event.get("intervention_id"),
                    "miragen.intervention.kind": event.get("kind"),
                    "miragen.intervention.source": event.get("source"),
                },
            )
        elif kind in ("intervention.answered", "intervention.superseded"):
            answer = event.get("answer") or {}
            _span(
                kind.replace(".", " "),
                start_ns=ts_ns,
                attrs={
                    "miragen.intervention.id": event.get("intervention_id"),
                    "miragen.intervention.decision": answer.get("decision"),
                    "miragen.intervention.approval_ref": answer.get("approval_ref"),
                    "miragen.intervention.answered_by": answer.get("answered_by"),
                },
            )
        elif kind == "turn.failed":
            error = event.get("error")
            _span(
                "turn failed",
                start_ns=ts_ns,
                attrs={},
                error=str(error) if error else "turn failed",
            )
        # thread.started / turn.started / turn.completed / lifecycle.setup.started
        # shape the root span rather than earning their own.


# ── Env wiring ───────────────────────────────────────────────────────────────


def telemetry_from_env(profile: AgentProfile) -> MiragenTelemetry | None:
    """Build telemetry from environment, or None when unconfigured (the
    default: zero overhead, no background thread).

    MIRAGEN_OTLP_ENDPOINT     OTLP/HTTP traces endpoint (enables telemetry)
    MIRAGEN_OTLP_TOKEN        optional bearer token
    MIRAGEN_OTLP_AUTH         optional verbatim Authorization header value
                              (e.g. Langfuse's `Basic <base64>`); wins over
                              MIRAGEN_OTLP_TOKEN
    MIRAGEN_DEPLOYMENT_ENV    optional deployment.environment resource attr

    All three secrets work with the `*_FILE` loader (`_load_file_secrets`).
    """
    endpoint = os.environ.get("MIRAGEN_OTLP_ENDPOINT")
    if not endpoint:
        return None
    version: str | None
    try:
        from importlib.metadata import version as _version

        version = _version("miragen")
    except Exception:
        version = None
    return MiragenTelemetry(
        endpoint=endpoint,
        agent_name=profile.name,
        agent_mode=profile.mode,
        service_version=version,
        deployment_environment=os.environ.get("MIRAGEN_DEPLOYMENT_ENV"),
        token=os.environ.get("MIRAGEN_OTLP_TOKEN"),
        auth_header=os.environ.get("MIRAGEN_OTLP_AUTH"),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _cap(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _ATTR_MAX:
        return value[:_ATTR_MAX]
    return value


def _join(value: Any) -> str | None:
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value) or None
    return None


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1e9)


def _to_ns(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1e9)


def _event_time_ns(event: dict[str, Any]) -> int | None:
    raw = event.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        return _to_ns(datetime.fromisoformat(raw))
    except ValueError:
        return None
