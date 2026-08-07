# OTLP telemetry

miragen exports run traces over **OTLP/HTTP** (`miragen/telemetry.py`),
speaking the same wire format and attribute vocabulary as
`mira_sdk.telemetry.MiraTelemetry` — the two halves of the Mira stack land
in the same backend and are filterable by one `mira.*` vocabulary. Any
OTLP/HTTP receiver works: an OTel Collector, Langfuse's
`/api/public/otel` endpoint (OTLP over HTTP is the only transport Langfuse
ingests — no gRPC), or a bare receiver during development.

Off by default. Enable with environment variables:

| Variable | Meaning |
|---|---|
| `MIRAGEN_OTLP_ENDPOINT` | OTLP/HTTP traces endpoint; setting it enables export |
| `MIRAGEN_OTLP_TOKEN` | Optional `Authorization: Bearer` token |
| `MIRAGEN_OTLP_AUTH` | Optional verbatim `Authorization` value (wins over the token) |
| `MIRAGEN_DEPLOYMENT_ENV` | Optional `deployment.environment` resource attribute |

The secrets work with the `*_FILE` loader like every other credential.

`GET /health` reports `telemetry.otlp_configured` so an empty backend is
explainable before anyone debugs it.

## Non-blocking, structurally

Spans are queued through a `BatchSpanProcessor` (background export thread)
— never a `SimpleSpanProcessor`. A telemetry backend being down must never
fail or stall a run; that guarantee lives in this module, not in call-site
discipline. Executor-turn translation additionally runs *after* the turn,
from the durable event stream, so a translation bug can lose telemetry but
can never affect execution. Export failures are counted
(`MiragenTelemetry.export_stats`) and logged, and `lifespan` flushes with a
bounded timeout on shutdown.

## Run identity on every span

Trace-level attributes must be present on **every** span or cross-span
filtering/aggregation breaks late and expensively. miragen stamps them via
a span processor reading a run-scoped contextvar — so spans opened by
pydantic-ai's own instrumentation carry them too, without pydantic-ai
knowing anything about miragen:

- resource (process-stable): `service.name=miragen`, `service.version`,
  `mira.agent.id` (profile name), `mira.agent.mode`,
  `deployment.environment`
- every span in a run: `mira.run.id`, `mira.run.trigger`,
  `mira.agent.tier` (`model` | `executor`)

## Both tiers, one trace model

**Model tier** — pydantic-ai's own OTel instrumentation is wired to
miragen's provider through the `Instrumentation` capability
(`factory.build_agent`): the run, every model request, and every tool
execution become GenAI-semconv spans, nested under the `agent run` root
span the app tier opens. (`POST /run/stream` runs are recorded in the run
store and event log but not wrapped in a run span yet.)

**Executor tier** — the self-harnessed loop can't be instrumented from
here, but its event stream can be translated span-for-span after each
turn, with the events' own timestamps: one `executor turn` root span
(status, exit reason, `gen_ai.usage.*` totals) with children for workspace
setup, repository preparation, tool items (`miragen.tool.*`, ERROR on
non-zero exit), diff harvest, and interventions. The
`interventions/answered/superseded` lifecycle is traced with
`miragen.intervention.*` attributes including `approval_ref`.

Span attributes carry **mechanical facts only** — no message payloads, no
diffs, no file contents. Large content stays in the run record, the event
stream, and the artifact sink; a span references it, it doesn't embed it.
And deliberately no LLM-in-the-loop analytics (no judge scores, no
model-authored classifications) — spans state what happened, not what a
model thinks about it.

## Langfuse wiring (reference deployment)

Langfuse authenticates OTLP with HTTP Basic (base64 of
`public_key:secret_key`), so use the verbatim header:

```yaml
environment:
  MIRAGEN_OTLP_ENDPOINT: https://langfuse.example/api/public/otel/v1/traces
  MIRAGEN_OTLP_AUTH_FILE: /run/secrets/langfuse_auth   # "Basic <base64 pk:sk>"
```

An OTel Collector in between (recommended once more than miragen exports)
owns auth and fan-out instead — miragen then points at the collector with
no auth or a bearer token.
