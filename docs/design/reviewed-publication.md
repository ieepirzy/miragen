# Reviewed whole-run publication

**Status:** implemented (`miragen/publication.py`, `POST /runs/{id}/publications`)  
**Capability:** `reviewed-publication/v1` (advertised on `GET /health`)

### Capability semantics

`reviewed-publication/v1` in `capabilities` means: **this build exposes the
HTTP endpoint**. It does **not** mean a publication backend is configured on
the running agent profile.

| Signal | Meaning |
|---|---|
| `capabilities` contains `reviewed-publication/v1` | Endpoint supported |
| `publication.backend_configured` (on `/health`) | `executor.artifact_sink` is set; publish can succeed |
| `POST .../publications` → 400 "not configured" | Endpoint present, backend missing |

Orchestrators should treat the capability as “API available” and still require
backend configuration (or read `publication.backend_configured`) before
assuming publishes will work.

## Why

Orchestrators such as MiraRun must not write artifacts, provenance, or history
into a document store (e.g. Loimi) themselves, and must not duplicate
graduation on every successful agent turn. Publication is a **human-reviewed**
control action: miragen holds the authoritative run/diff; the external store
becomes the canonical owner of graduated artifacts only after an explicit call.

## Backend-agnostic contract

miragen is open source and must not hard-code product integrations. The HTTP
surface is store-agnostic:

| Field | Meaning |
|---|---|
| `backend` | Registered backend kind (`loimi`, …) |
| `external_run_id` | Opaque run id in that backend |
| `external_artifact_ids` | Opaque artifact ids |

When `backend == "loimi"`, the response also fills convenience aliases
`loimi_run_id` / `loimi_artifact_ids` with the **same** opaque values so MiraRun
can keep loimi-named fields without forking the contract.

New backends implement `PublicationBackend.publish_run` and register in
`build_publication_backend` — no change to MiraRun’s HTTP client shape.

## Endpoint

```
POST /runs/{run_id}/publications
Authorization: X-Miragen-Token (when MIRAGEN_INTERNAL_TOKEN is set)
```

### Request

```json
{
  "idempotency_key": "mirarun:publication:{uuid}",
  "provenance": {
    "mirarun_run_intent_id": "uuid",
    "environment_id": "uuid",
    "environment_revision": 1,
    "requested_by": "uuid"
  }
}
```

`provenance` allows extra fields.

### Preconditions (deterministic 400)

- Run exists
- `status == "succeeded"`
- `diff_path` set and file present on disk
- Profile has `executor.artifact_sink` configured

### Response

```json
{
  "publication_id": "miragen-hex-id",
  "run_id": "miragen-run-id",
  "status": "published",
  "duplicate": false,
  "backend": "loimi",
  "external_run_id": "opaque",
  "external_artifact_ids": ["opaque"],
  "loimi_run_id": "opaque",
  "loimi_artifact_ids": ["opaque"]
}
```

### Idempotency

Same `idempotency_key` returns the same `publication_id` and external refs with
`duplicate: true` and **no** further backend writes.

### Failures

| Case | HTTP | Run status |
|---|---|---|
| Validation / preconditions | 4xx | unchanged |
| Backend/network unavailable | 503 `retryable: true` | unchanged |
| Backend application error | 502 | unchanged |

## Auto-sink retired

Previously, a successful executor turn called `_store_artifact` whenever
`artifact_sink` was set. That conflicted with reviewed publication. The auto
path is **removed**; `artifact_sink` only configures the backend used by this
endpoint.

`miragen/executor/sink.py` is a **low-level** `store_document` helper only
(tests / ad-hoc). It is not wired into the executor success path — do not
reintroduce that call.

## Ownership

| Layer | Owns |
|---|---|
| miragen | Executor state, workspace, harvested diff, publication idempotency records |
| publication backend (e.g. Loimi) | Artifacts, provenance, lineage, meaningful history after publish |
| MiraRun | Publication *request* state + opaque miragen/backend references only |
