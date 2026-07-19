# MiraRun substrate contracts: EDF resolution, idempotent launch, sequenced events

**Status:** implemented (issue #33 Phases A, B, C — the Stage-1 blockers)
**Design inputs:** [issue #33](https://github.com/ieepirzy/miragen/issues/33), mirarun `docs/architecture/implementation-plan.md`, mirarun `docs/contracts/edf/v1alpha1.schema.json`

miragen is the execution substrate under the planned MiraRun control plane.
This document records the three execution-side contracts added for that role,
and the decisions behind them. Everything that already existed — executor
lifecycle, durable run records, budgets/timeouts, suspend/resume/abandon,
baseline-tag diff harvesting, scheduling, MCP injection, artifact sinks — is
reused unchanged.

## 1. Vocabulary

Three artifacts stay conceptually distinct (issue #33 Phase A):

| Artifact | What it is | Where it lives |
|---|---|---|
| **EDF** | desired environment; versioned, human-editable | control plane (MiraRun) |
| **resolved profile** | executable miragen configuration (a real `AgentProfile`) | output of `resolve_edf()` |
| **run snapshot** | immutable resolved definition + hash + concrete revisions for ONE run | `runs/snapshots/<run_id>.json` |

## 2. EDF validation and resolution (`miragen/edf.py`)

`validate_edf()` strictly validates a `mirarun.io/v1alpha1` `Environment`
document — the same corpus mirarun's JSON Schema describes, plus the
semantic rules a JSON Schema cannot express (mount-path normalization and
non-overlap, unique names, dangling secret references, dead network config,
duration grammar). Unknown fields fail validation for the selected
`apiVersion`; a new `apiVersion` requires explicit conversion, not leniency.

`resolve_edf(document, context)` is **pure and deterministic** — no
filesystem, network, or clock access — and returns:

```text
resolve(edf, context) -> {
  resolver_version, api_version, name,
  sha256, canonical, preset_versions,
  resolved_profile,        # validated AgentProfile (executor tier, http trigger)
  repository_plan,         # per-repo: name/provider/connectionRef/ref/mountPath/writable/commit
  secret_bindings,         # name/providerRef/environmentVariable — identity only, never values
  workspace_plan,          # variables, network, lifecycle (recorded desired state)
  target_adapter_plan,     # recorded, not provisioned (Phase G)
  warnings,
}
```

Mapping decisions worth recording:

- **Executor kinds:** `codex` and `claude-code` resolve; `spawn` is
  deliberately unresolvable (it needs an argv template v1alpha1 cannot
  express) and unknown kinds are errors, not warnings.
- **`sandbox.mode: full-access` → `danger-full-access`** — the scary name is
  the point; the EDF's friendlier alias maps onto it explicitly.
- **`approvals.unattended: true` → `approval_policy: never`** (the unattended-
  safe default), `false` → `on-request`.
- **`network.outbound`** toggles the executor sandbox's `network_access`.
  `allowedHosts` is *recorded* in the snapshot but host-level egress
  enforcement is deployment-owned — the resolver says so in `warnings`
  rather than pretending otherwise.
- **MCP servers:** only `streamable-http` resolves (that is what executor
  config injection supports). `auth.bearerTokenSecretRef` must name a
  declared secret; it resolves to that secret's **environment-variable
  name** — the value never exists anywhere in this pipeline.
- **Instructions** are resolution context (`ResolutionContext.instructions`,
  defaulting to a neutral constant), not desired state: they do not affect
  the canonical hash.
- **Tool presets** are versioned and expanded at validation time
  (explicitly-set flags win), so the canonical document always carries
  concrete flags and the output records `preset_versions`.
- **Repository plans and lifecycle phases are validated and recorded but not
  yet executed** — multi-repository workspace preparation is Phase D, which
  touches workspace isolation and diff semantics and therefore stops for
  explicit review per the issue. `commit` fields exist and stay `null` until
  Phase D fills them.

### Canonical hash contract

```
validate → apply explicit defaults → expand presets → sort object keys
→ RFC 8785-style JSON (compact separators, UTF-8, no ASCII escaping) → SHA-256
```

Array order remains significant. Every field is present in the canonical form
(`null` where unset), so shape is stable across writers. Secret values cannot
appear (the schema has nowhere to put them); stable secret-reference identity
is hashed. `tests/test_edf.py` pins golden hash vectors — if those tests
fail, canonicalization changed, which is a **breaking contract change**
requiring a new resolver/schema version, not a test update.

## 3. HTTP surface

### `POST /profiles/resolve`

Validates and resolves without starting a run. 422 carries structured
`{loc, message}` errors. The response includes `agent_compatibility` —
informational here, enforced at launch (see below).

### `POST /executor-runs` (Phase B)

Idempotent, provenance-carrying launch of one executor run:

```json
{
  "prompt": "completed prompt — dispatched verbatim",
  "idempotency_key": "mirarun:run-intent-001",
  "edf": { "...": "optional" },
  "context": { "instructions": "optional" },
  "expected_sha256": "optional caller-side hash to verify against",
  "provenance": { "environment_id": "...", "any_product_field": "kept verbatim" }
}
```

- **Durable-first acceptance:** the run record — provenance, executor/model,
  repository revisions, `snapshot_sha256` — and the snapshot document are
  persisted *before* the 202 is returned and before dispatch. There are no
  awaits between the idempotency lookup and the record write, so a same-key
  race cannot slip between them in the single-process app.
- **Idempotency:** a repeated `idempotency_key` returns the original run
  (200, `duplicate: true`, current status) and never launches twice.
- **Recovery (the ambiguity window):** if the process dies after acceptance
  but before/while dispatching, the startup sweep marks the record
  `interrupted`; retrying the key then surfaces that run and its true
  status, and the caller decides (new key to relaunch, or resume if a
  thread exists). Durability without a queue, exactly as the mirarun plan
  asks.
- **Trust-boundary re-resolution:** when an EDF is supplied, miragen
  resolves it *again* and refuses (409) on `expected_sha256` mismatch.
- **Topology guard:** Stage 1 keeps the one-agent-per-service deployment, so
  a launch executes with the **configured** executor spec. An EDF that
  resolves to a different executor kind/model/sandbox than this service runs
  is a 409 with the concrete issues — it belongs to a different deployment.
  Making the resolved profile *drive* per-run executor construction is the
  ADR-003 topology decision and intentionally out of scope here.
- **Prompt is verbatim:** no timestamp stamping, no `header_prompt`. Prompt
  rendering is a control-plane concern; miragen persists and dispatches what
  it was given.
- Authoritativeness is unchanged: miragen owns run state; the control plane
  owns product entities. `provenance` is stored and returned verbatim
  (`extra="allow"`), never interpreted.

### `GET /runs/{id}/snapshot`

Returns the immutable snapshot (`miragen/run-snapshot/v1`): canonical
document, hash, resolved profile, repository/secret plans, warnings.
Re-resolving `snapshot.canonical` reproduces `snapshot.sha256` — that is the
acceptance test for hash reproduction.

### `GET /runs/{id}/events` (Phase C)

One durable sequenced stream, two read modes:

- **tail** (no `after`): newest `limit` events — the original polling
  contract, unchanged shape.
- **cursor** (`?after=N&limit=M`): events with `seq > N`, oldest first, plus
  `next_after` and `has_more`. Replaying any cursor is idempotent;
  `(run_id, seq)` is the deduplication key; a projector rebuilds from
  `after=0` at any time. SSE/webhooks can be layered later without changing
  this envelope.

### `GET /health`

Now advertises `version` (installed distribution) and `capabilities` — the
versioned contract surfaces this build serves (`edf-resolve/…`,
`executor-launch/v1`, `run-snapshot/v1`, `events-cursor/v1`) — so clients
(miragen-mcp, MiraRun) can feature-detect instead of guessing from version
numbers.

## 4. Event envelope

Every persisted event is one flat JSONL object carrying, alongside its
payload fields:

| Field | Meaning |
|---|---|
| `seq` | per-run monotonic sequence, 1-based; continues across resume |
| `schema` | `miragen/executor-event/v1` |
| `ts` | ISO-8601 UTC timestamp (pre-existing) |
| `type` | event type (pre-existing) |

The envelope is flat — new keys on the same objects — so existing tail
readers keep working. **Legacy files** (pre-envelope) are read with
line-number-derived sequences; since writers were always append-only and
single-writer per run, line order is event order, and a resumed run's writer
continues numbering after the last effective sequence. Unparsable lines
occupy a sequence slot so numbering never shifts.

The base tier now also emits lifecycle timing events around the adapter's
stream: `lifecycle.setup.started` / `lifecycle.setup.completed` (workspace
preparation, with `duration_ms`) and `lifecycle.harvest.completed`
(`duration_ms`, `diff_bytes`). Adapter-normalized events (`thread.started`,
`turn.*`, `item.*`) are unchanged.

A related hardening: diff harvest now runs inside the same failure envelope
as the turn itself, so a harvest failure produces a resumable `failed(crash)`
result instead of an unhandled exception that leaves the record `running`.

## 5. What this deliberately does not do

Per the issue's phasing and stop-for-review rules:

- **Phase D** — actual multi-repository checkout, per-repo baselines/diffs,
  lifecycle execution, ephemeral checkout credentials. Plans and `commit`
  fields are in place; execution changes workspace isolation and diff
  semantics and needs its own review.
- **Phase E** — cached-token and blocked-interval telemetry.
- **Phase F** — managed schedule reconciliation.
- **Phase G** — structured intervention events and target provenance.
- No product entities, pricing, or analytics in miragen; no executor
  `cancel()`; `/approvals` is not stretched into an executor intervention
  model.
