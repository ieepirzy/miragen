# Managed schedules and trigger provenance (issue #33 Phase F)

**Status:** proposed — design only, no implementation yet
**Depends on:** the Phase B provenance model (`RunProvenance`) and the Phase C event envelope, both on `main` once #37 merges.

## 1. Goal and constraints

Give an external control plane (MiraRun) safe create/update/delete control
over *when an agent fires*, without taking over *how it executes* and without
rewriting profile files behind a running service.

Hard constraints, from the issue and the MiraRun implementation plan:

- Scheduling execution already exists (APScheduler, profile triggers) and
  **must be reused** — this phase adds a management surface, not a scheduler.
- MiraRun owns routine/trigger desired state; MiraGen owns firing and durable
  execution. MiraGen never interprets product entities.
- `agent.yaml` stays hand-authored and static. A control plane that edits it
  behind the running process is the failure mode this design exists to avoid.
- Prompt rendering, parameter schemas, template versioning, and public
  webhook auth remain MiraRun concerns. MiraGen receives **completed
  prompts**, exactly as `POST /executor-runs` does.

## 2. The ScheduleBinding resource

A named, durable object, deliberately parallel to a profile trigger but
owned by the API rather than the profile:

```jsonc
{
  "name": "nightly-refactor",            // ^[a-z0-9][a-z0-9_-]{0,62}$ — API identity
  "schedule": { "cron": "0 3 * * *" },   // XOR { "every_s": 3600 } — same validation as profile triggers
  "prompt": "…completed prompt…",        // rendered by the control plane, dispatched verbatim
  "enabled": true,                       // false = binding persists, job unregistered
  "provenance": { "routine_id": "…", "trigger_id": "…", "…": "…" },  // open, stored verbatim
  "metadata": { "…": "…" },              // typed-parameter escape hatch: recorded, never interpreted
  "version": 4                           // server-assigned, monotonic; CAS token
}
```

Notes:

- `schedule` reuses the existing cron/interval validation (5-field cron via
  APScheduler; `every_s >= 10`). No new grammar. All cron evaluation is UTC —
  timezone/DST policy is the control plane's rendering problem, documented,
  not configurable here.
- `provenance` is the Phase B `RunProvenance` shape (extra fields allowed).
  `metadata` is a separate free-form map so product parameters don't pollute
  the provenance namespace; both are stamped onto every fired run.
- No prompt templates, no parameter substitution, no completion chains —
  a binding whose prompt must change is *updated* by the reconciler.

## 3. Storage and startup semantics

- One JSON file per binding under the agent volume:
  `/agent/schedules/<name>.json`, atomic write (tmp + `os.replace`), same
  durability posture as run records. The volume, not the container, is the
  source of truth — bindings survive restarts and redeploys.
- At startup (lifespan), after profile triggers register, every enabled
  binding registers an APScheduler job under the id `managed:<name>`.
  Profile-trigger jobs keep their existing ids — the namespaces cannot
  collide, and a managed binding may not shadow a profile trigger name.
- Live mutations (PUT/DELETE) update the file first, then reconcile the
  APScheduler job (add/replace/remove). File first: a crash between the two
  self-heals at next startup from disk.

## 4. HTTP surface (internal-token guarded, like everything else)

| Endpoint | Behavior |
|---|---|
| `GET /schedules` | list bindings with versions and next-fire times |
| `GET /schedules/{name}` | one binding |
| `PUT /schedules/{name}` | idempotent upsert; see CAS below |
| `DELETE /schedules/{name}` | remove binding + unregister job; in-flight runs untouched |

**Concurrent reconciliation (the version/conflict checkbox):**
`PUT` accepts optional `expected_version`. Omitted → create-only (409 if the
binding exists — a blind upsert from a reconciler that never read is a bug).
Present → compare-and-swap: mismatch is a 409 carrying the current binding,
so a reconciler re-reads, re-diffs, and retries. The server assigns
`version` (+1 per successful write); callers never invent it — same
philosophy as the canonical hash. Two MiraRun reconciler replicas racing
converge instead of flapping.

`DELETE` also accepts `expected_version` (optional, recommended) with the
same 409 semantics.

## 5. Firing semantics

- A fire goes through the existing `run_agent_scheduled` path — same daily
  budget skip, same suspension handling, same executor/model-tier dispatch.
  **(Decision 3, recommended: yes, budget applies — a managed schedule is
  not a budget bypass.)**
- The run record gets `trigger: "managed"` (new `RunTrigger` literal — not
  `"cron"`, so projections can tell profile-driven fires from control-plane
  bindings) and the binding's `provenance` + `metadata` merged onto
  `RunRecord.provenance`, plus `schedule_name` and the fire time. Attribution
  needs no new machinery: the Phase B fields carry it.
- Fires are not idempotency-keyed: each fire is a new run whose provenance
  carries `(schedule_name, fired_at)`; the control plane deduplicates via
  run records/events as it already must for cron.

## 6. Open decisions (flagged for the owner)

1. **`on_complete` on managed fires — recommended: skip.** A control plane
   that installed the binding is already observing runs; dispatching the
   profile's `log_to`/`notify`/`post_to` as well double-reports every fire.
   Counter-argument: an operator mixing hand-authored `on_complete` with
   managed bindings might expect uniformity. If skip is accepted, the
   behavior is documented on the binding API, not configurable per binding
   (a `dispatch_on_complete` flag can be added later without breaking).
2. **Interactive-mode agents — recommended: reject.** The profile rule
   ("interactive agents cannot have cron triggers") exists because
   self-activation contradicts the declared mode; a managed binding is still
   self-activation. `PUT /schedules` on an interactive-mode agent → 409
   telling the caller to run the agent in hybrid mode. Managed bindings are
   allowed on autonomous and hybrid agents.
3. **Daily budget — recommended: applies** (folded into §5). The
   `on_exceeded: notify` path fires as today.

## 7. Failure and restart semantics

- Unparsable binding file at startup: skipped with a loud log, never
  deleted — same posture as unreadable run records.
- APScheduler registration failure on PUT: file write is rolled back
  (previous content restored) and the PUT returns 500 — the store never
  claims a binding the scheduler doesn't hold.
- Missed fires while the container was down are **not** replayed
  (`misfire_grace_time` stays default). Catch-up semantics are a routine
  policy — MiraRun's, not MiraGen's.

## 8. Testing plan

- CRUD + CAS: create-only conflict, stale `expected_version` 409 with
  current-state body, delete with/without version.
- Restart: bindings re-register from disk; disabled bindings don't.
- Fired-run provenance: `trigger == "managed"`, schedule name, merged
  provenance/metadata on the record.
- Coexistence: profile cron and managed binding on one agent fire
  independently; namespace collision impossible.
- Budget skip and (per decision 1) on_complete suppression on managed fires.
- Interactive-mode rejection (per decision 2).

## 9. Out of scope

Webhook auth and GitHub event mapping, prompt templates/parameter schemas,
routine history, completion chains, run-completion triggers, and any
schedule ownership migration of existing profile triggers — all MiraRun
(plan §7.4) or later phases. No changes to executor state semantics,
workspace handling, or the launch contract.
