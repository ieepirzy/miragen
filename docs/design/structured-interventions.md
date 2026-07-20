# Structured interventions and target event provenance (issue #33 Phase G)

**Status:** implemented (`miragen/executor/base.py` sentinel detection,
`miragen/models.py` wire models, `/runs/{id}/resume` answer flow)
**Mechanism decision (ADR-009-class, owner, 2026-07-20):** workspace
sentinel file — executor-agnostic, no SDK dependency, deterministic schema
validation. A miragen-hosted MCP `ask_human` tool can layer on later by
writing the same file; nothing in the contract changes.

## 1. The question mechanism

An executor that reaches a decision only a human can make writes ONE file
and ends its turn:

```jsonc
// <workspace>/.miragen/intervention.json
{
  "question": "Should the retry queue live in Postgres or Redis?",  // required
  "kind": "architecture-decision",                                   // optional
  "options": [ {"id": "postgres", "label": "Postgres outbox"}, ... ],// optional
  "evidence": "See notes in docs/queue.md",                          // optional
  "affected_repositories": ["app"]                                   // optional
}
```

The default EDF-resolved instructions teach this protocol; hand-authored
executor profiles can adopt it by instructing the same.

After the turn's stream completes, the base tier:

1. parses and validates the file — **the schema is the contract**: a
   malformed file is set aside as `intervention.invalid.json`, an
   `intervention.invalid` event records why, and the run proceeds normally.
   There is no prose-parsing fallback, per the plan's explicit rule.
2. stamps a miragen-assigned `intervention_id` (agent-supplied ids are
   discarded) and `requested_at`,
3. archives the file to `.miragen/interventions/<id>.json` so a resumed
   turn never re-triggers it,
4. emits `intervention.requested` (with the full payload) into the
   sequenced event stream, and
5. suspends the run: `status=suspended`, `exit_reason="intervention"`,
   `pending_intervention` on the run record. The question beats both
   harvest and the budget check that turn — the question must be recorded
   either way, and the cumulative budget check guards the next turn.

Thread + workspace survive as resume state, exactly like budget/timeout
suspensions. Queue aggregation, notifications, actors, and UI decisions
stay in MiraRun, which sees the suspension via the run record and the
`intervention.requested` event.

## 2. The answer/resume contract

`POST /runs/{id}/resume` now takes `prompt`, `answer`, or both:

```json
{
  "answer": {
    "intervention_id": "…",         // must match pending_intervention (409 otherwise)
    "decision": "postgres",          // chosen option id
    "text": "Use the outbox pattern.",
    "approval_ref": "appr_777",      // server-side authorization evidence
    "answered_by": "ilari"
  }
}
```

- The answer is bound to the pending intervention by id; a mismatched or
  absent pending intervention is a 409. At least one of `decision`/`text`
  is required; extra fields are stored verbatim.
- `intervention.answered` (with the full answer, including `approval_ref`)
  is appended to the same sequenced event stream **before** the turn runs —
  authorization evidence is durable API state, ordered strictly after the
  request it answers.
- With no caller `prompt`, miragen renders a deterministic, mechanical
  resume message from the answer (id, decision + option label, text). Real
  prompt rendering stays in the control plane; a caller-supplied `prompt`
  wins and the answer is still recorded.
- A plain-prompt resume of an intervention-suspended run records
  `intervention.superseded` — the question's disposition is never silent.
- `pending_intervention` clears on reopen either way; history lives in the
  events.

## 3. Target event provenance (also closes the Phase C leftover)

Any executor event MAY carry a `target` object; none is required in v1:

```json
{
  "type": "item.completed", "seq": 41, "schema": "miragen/executor-event/v1",
  "item": { "type": "command_execution", "command": "psql …" },
  "target": {
    "target_id": "tgt_1",
    "target_name": "staging-db",
    "operation_class": "read",          // inspect | read | write | destructive
    "credential_grant_ref": "grant_9",
    "approval_ref": "appr_3"
  }
}
```

`TargetOperationProvenance` (models.py) is the typed contract; extra
adapter-owned fields are allowed. Emitters arrive with the Stage-3 target
adapters — the envelope and field semantics are fixed now so projections
can be built against them.

## 4. Enforcement boundary

What v1 mechanically guarantees: the question, its answer, and any
`approval_ref` are structured, immutable, sequenced API state — a resume
cannot claim an approval that isn't in the stream, and prompt text alone
neither asks nor answers an intervention. What v1 does NOT claim: that an
executor will always stop when it should (behavioral policy via
instructions), or that target writes are gated — deterministic
production-write gates live in the target adapters and the control plane's
policy decision point (Stage 3), keyed by exactly these `approval_ref` /
`credential_grant_ref` fields.

## 5. Out of scope

Intervention queues/inboxes, notification delivery, actor authorization,
answer UX, expiry/escalation policies (MiraRun); target adapters and
credential brokering (Stage 3); the MCP `ask_human` tool variant (layers on
without contract changes).
