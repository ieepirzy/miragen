# The miragen memory backend protocol — v1

Status: **stable** (frozen 2026-09-14, after memory-pass PR 4 settled the
wire surface). Design authority: `miragen-memory-agent-architecture-pass.md`
§17. Same ownership rule as the voice schema: **miragen owns this
contract; backends implement it** — miragen is never adapted to a
particular store's API.

A *memory backend* is any HTTP service exposing this contract under
`/memory/v1`. miragen's `MemoryClient` speaks it; the profile's
`memory.backend` selects an implementation:

| Backend | What it is |
|---|---|
| `loimi` | The blessed reference implementation (Loimi's `/memory/v1`): Postgres 16 + pgvector, FORCE row-level security under a dedicated non-superuser role, durable. Reached via `memory.endpoint_env` / `credential_env`. |
| `ephemeral` | Built into miragen (`miragen/memory/ephemeral.py`): in-process, full lifecycle, **zero durability** — dev/demo only. |
| *(yours)* | Anything that passes the conformance suite (below). |

## Conformance is behavioral

Route shapes are necessary, not sufficient. `miragen memory-conformance
<base-url> --operator-token <op-token>` (or `--ephemeral` to self-test the
built-in backend) runs the twelve behavioral checks that define
conformance; both shipped implementations pass 12/12. The checks, which
double as this document's semantics section:

1. **Auth crossings** — no token is 401; the operator credential is not a
   data principal; a principal token does not open the operator surface.
2. **Idempotent replay** — the same `(producer, idempotency_key)` returns
   the original event (`created: false`); the same key with different
   content digest is a 409.
3. **Admission** — a rootless proposal is a `candidate`; an
   evidence-rooted one is `accepted`. Candidates never enter default
   recall or take heads.
4. **Revision CAS** — a stale `expected_seq` is refused with a 409
   carrying `current_seq`; no silent last-writer-wins.
5. **Temporal truth** — a single-valued replacement moves the current
   head while the old value still answers `at=` queries inside its own
   validity interval, and `as_of_record=` replays belief from the
   append-only decisions (late changes never rewrite what was believed).
6. **Authority withholding** — a claim whose verified roots lack the
   predicate's registered head-moving source kinds is retained with
   resolution `withheld`; the standing head is unmoved.
7. **Unregistered predicates** — claims on them are accepted as
   `observation_only` (grouped, never a head); explicit cardinality
   asserts that disagree with reality are 409s.
8. **Quarantine containment** — `quarantine: true` can only lower
   disposition; quarantined revisions never take heads, automatically or
   by explicit resolution.
9. **Correction in place** — evidence-rooted, CAS-guarded; the corrected
   revision links `correction_of` and replaces the erroneous one **in its
   validity interval** (world changes close intervals; corrections do
   not), while record-time queries still replay the pre-correction
   belief.
10. **Absence without leaks** — a record the caller cannot read is
    indistinguishable from one that does not exist (404 both).
11. **Working-state CAS** — `expected_revision` guards every patch, with
    the current revision in the refusal.
12. **Search recall** — candidate generation is OR-token (a natural
    request matches on overlap, ranked by it; precision belongs to the
    caller's relevance selection, never the index), and records with
    invalidated roots do not surface.

## Credential classes

- **Operator token**: provisions the registry — principals (returning
  minted tokens), scopes, capability grants (`read` / `propose` /
  `resolve` / `retract` / `maintain`), predicate registrations
  (cardinality + `authority_source_kinds`) — and performs erasure and
  maintenance (`/admin/*`, `/events/{id}/erase`).
- **Principal token**: everything else. Producer identity is always
  derived server-side from the token, never accepted from a body.

## Surface

Data (principal token):

```
POST  /memory/v1/events                      idempotent source-event capture
GET   /memory/v1/events/{id}
POST  /memory/v1/events/{id}/retract         generation-bumping invalidation
POST  /memory/v1/records                     propose (observation/claim/procedure/intention;
                                             claims carry subject/predicate/qualifiers;
                                             revise via record_id+expected_seq)
GET   /memory/v1/records/{id}[?seq=]         current or labeled-historical revision,
                                             with per-root validity status
POST  /memory/v1/records/{id}/retract
POST  /memory/v1/records/{id}/correct        evidence-rooted in-place correction
GET   /memory/v1/claims                      by identity: scope+subject[+predicate],
                                             at= (valid time), as_of_record= (record time)
POST  /memory/v1/slots/{id}/resolve          explicit CAS resolution to resolved segments
POST  /memory/v1/contexts | GET/PATCH /contexts/{id}   revisioned working state (CAS)
POST  /memory/v1/search                      bounded hybrid candidates (lexical OR-token;
                                             optional dense channel gated on exact
                                             embedding_space identity), canonical
                                             read-time validation before emission
POST  /memory/v1/manifests                   injection manifests
POST  /memory/v1/revisions/{id}/evidence     evidence receipts
```

Optional worker surface (`maintain` capability — a backend without
background machinery, like `ephemeral`, may omit it; miragen's worker
degrades to consolidation-less operation):

```
POST /memory/v1/jobs/claim | /jobs/{id}/complete | /jobs/{id}/fail
GET/PUT /memory/v1/projections/{revision_id}[/embedding]
```

Errors are `{"error": {"code", "message", "details"}}` with conventional
statuses (400 invalid, 401 unauthenticated, 404 absence, 409 CAS/conflict).

## Versioning

The `/memory/v1` prefix is the compatibility contract: additive changes
(new optional fields, new routes) do not bump it; a breaking change ships
as `/memory/v2` alongside. The conformance suite is versioned with this
document and is the acceptance test for any claim of compatibility.

### Source grounding extension

Loimi migration 0011 adds immutable revision/resource baselines, explicit check
receipts, scoped `POST /memory/v1/resources/lookup`, and bounded transactional
consolidation. Miragen's ephemeral backend does not implement this extension.
See [source-grounded memory](source-grounded-memory.md) for contracts, capability
requirements, the CLI/MCP workflow and a runnable HTTP example. Injection
manifests describe rendered content with unconfirmed delivery; they are not
harness acknowledgements.
