# Miragen Memory & Agent Architecture Pass

Status: **Design / implementation target**  
Scope: **Working state + operational long-term memory for Mira and other Miragen agents**  
Revision: **2026-09-13 — accepted architecture tightened for telemetry, memory health, shared scopes, native hooks and agent guidance**  
Principle: **Add durable cognitive state without overhauling the entire agent runtime.**

## Goal

This pass should give Miragen a proper memory/state architecture while preserving the existing strengths of the current agent system.

The key architectural direction is:

> Durable state should live outside the transcript.  
> Traditional tool calling remains a first-class action mechanism.  
> Model reasoning should operate over explicit state, evidence, memory, and verified runtime results.

This is **not** a rewrite of Miragen into a fully programmatic-agent runtime, workflow compiler, recursive multi-agent system, or speculative executor.

The purpose of this pass is to establish the state and memory foundations that those systems could later build on. This includes a functioning memory write/retrieve/inject lifecycle, not only schemas or optional search tools. Mira is a long-running identity across bounded runs; other agents use the same subsystem with different scopes and policies.

Evidence boundary: the additions below are grounded in the supplied draft, full MiraDB observations listed in §15, and external primary sources assessed in §16. Historical Loimi reports in §15 are supplemented by targeted repository inspection in §17. No deployment was tested. New contracts and policies are proposals; §17 refines earlier illustrative choices and identifies concrete changes to current code. The user accepted the preceding design on 2026-09-13. This revision incorporates their follow-up requirements. New detailed configuration names and interface shapes remain implementation proposals, not shipped capabilities.

---


## Recommended Decisions After Red-Team Review

The original design had sound boundaries but weak enforcement details. The implementation proposal is now: **Postgres 16 + pgvector; a protected memory schema and transactional API in Loimi; a Miragen memory module using its existing PydanticAI/runtime integration; structured evidence-backed admission; hybrid retrieval with an explicit empty-result option; and source-root invalidation.** No new graph database, agent framework, broker or per-agent database is needed.

Read **[§17 first](#17-red-team-review-and-concrete-implementation-proposal)** for the failure findings, concrete choices, pollution controls, initial limits and delivery order. Earlier sections explain intent; §17 replaces any earlier ambiguity about these choices. Implementation is still unstarted. **§18 specifies the follow-up requirements:** central service deployment, current telemetry and gaps, memory-health analytics, scope sharing, native harness hooks and mandatory agent guidance.

## 1. Action Model: Preserve Traditional Tools, Add Programmatic Execution Later

Traditional tool calling remains fully supported and is **not** being replaced.

The agent should continue to be able to make ordinary atomic tool calls:

```text
model
  -> tool_call(args)
  -> tool_result
  -> model
```

This is still the correct abstraction for many actions.

Examples:

- fetch one document
- send one message
- update one record
- query one endpoint
- inspect one file
- execute one clearly bounded capability

Longer-term, Miragen may additionally support programmatic action blocks:

```text
model
  -> executable action block
  -> multiple deterministic tool operations
  -> result
  -> model
```

This would complement traditional tools rather than replace them.

### Rule

Use the lowest-complexity action mechanism that fits the task:

```text
traditional tool call
        |
        v
programmatic action block
        |
        v
subagent / larger delegated workflow
```

Programmatic execution is **not required for this pass** unless it naturally falls out of the implementation.

---

## 2. Explicit Working State

Miragen should no longer rely on the conversation transcript as the authoritative representation of current task state.

Introduce a structured working-state object.

Conceptually:

```python
WorkingState(
    current_goal=...,
    completed_steps=[...],
    unresolved_questions=[...],
    known_facts=[...],
    active_constraints=[...],
    pending_actions=[...],
    artifact_refs=[...],
    evidence_refs=[...],
)
```

The exact schema can evolve, but the architectural distinction is important:

```text
transcript      = historical interaction / audit trail
working state   = current operational state
```

Reasoning may be ephemeral.

Working state must be durable enough to survive:

- context compaction
- long-running tasks
- agent restarts
- model swaps
- resumed sessions
- partial execution failures

### Requirements

Working state should:

- be explicit
- be machine-readable
- be independently inspectable
- support partial updates
- preserve provenance where practical
- avoid requiring replay of the full transcript to reconstruct current state

---

## 3. Memory Layers

Miragen should distinguish different forms of memory instead of treating all persistence as one retrieval pool.

Initial conceptual split:

```text
Episodic Memory
    what happened

Semantic Memory
    what is currently believed / known

Procedural Memory
    how to do things

Working State
    what matters right now
```

### 3.1 Episodic Memory

Stores historical observations/events.

Examples:

- user said X
- tool returned Y
- project changed from A to B
- task failed for reason Z
- preference changed
- a decision was made

The episodic layer should preserve history rather than silently overwrite it.

Example:

```text
t1: user prefers aisle seat
t2: user now prefers window seat
```

Both historical observations remain available.

---

### 3.2 Semantic Memory

Stores the current integrated interpretation of past observations.

Example:

```text
current preference:
window seat

derived from:
t1 aisle preference
t2 preference update
```

The semantic layer should be updateable without destroying historical evidence.

Important distinction:

```text
history != current belief
```

This prevents the system from either:

1. deleting useful historical context, or
2. retrieving mutually contradictory historical chunks as if they were equally current.

---

### 3.3 Procedural Memory

Stores reusable behavioral knowledge.

Examples:

- how a particular project should be deployed
- how the user prefers a recurring workflow handled
- known repair procedures
- tool-use patterns
- stable project conventions

Procedural memory should be distinct from ordinary semantic facts.

It may eventually contain:

```text
procedure
preconditions
steps
expected outputs
known failure modes
validation criteria
```

This can remain minimal in the first implementation.

---

## 4. Artifact State vs Evidence State

Miragen should explicitly distinguish:

```text
artifact state
    what currently exists

evidence state
    what has actually been verified
```

This distinction should be generic rather than coding-specific.

### Coding example

Artifact state:

```text
repository contents
generated patch
configuration
build output
```

Evidence state:

```text
tests passed
tests failed
lint passed
deployment verified
known regression remains
mobile behavior untested
```

### Research example

Artifact state:

```text
draft
notes
hypothesis
summary
```

Evidence state:

```text
sources checked
claims verified
claims disputed
open uncertainty
missing primary source
```

### Core rule

The existence of an artifact does not prove its correctness.

The model should never be forced to infer evidence from artifact existence alone.

---

## 5. Runtime Owns Execution Records

The model may make claims about the world, but Miragen should distinguish model belief from authoritative runtime state.

Examples:

```text
model:
"I believe the task is complete."

runtime:
completion predicate satisfied / not satisfied
```

```text
model:
"the tool call succeeded."

runtime:
execution status = success / failure / unknown
```

The runtime should own, where applicable:

- task status
- actual tool results
- execution lifecycle
- permissions
- side-effect status
- completion checks
- verification results
- idempotency state
- attributed observations of external state, with observation time and source revision

This pass does **not** need to solve the complete runtime-control problem.

The immediate requirement is simply:

> Do not encode authoritative state only as natural-language model belief.

A runtime receipt proves that an operation returned a particular result. An HTTP success, process exit code or test runner report does not by itself prove a domain outcome. Evidence predicates must specify what was checked, against which subject/version, with which limitations. External truth can change after observation.

---

## 6. Provenance and Dependencies

Miragen should preserve enough provenance to know where important derived state came from.

A simple representation is sufficient initially:

```python
DerivedValue(
    value=...,
    source_refs=[...],
    depends_on=[...],
)
```

Possible references:

- memory IDs
- tool results
- files
- messages
- artifacts
- previous state fields
- external records

This enables future selective invalidation.

Example:

```text
A ----\
       -> C -> E
B ----/
D ---------> F
```

If B changes:

```text
invalidate:
C
E

retain:
D
F
```

The first implementation does **not** need a full dependency DAG engine.

The requirement is to avoid throwing provenance away such that dependency tracking becomes impossible later.

---

## 7. Controlled Harness / Config Writes

Large-scale dynamic harness generation is out of scope.

Miragen should keep a stable base harness.

However, agents may eventually be allowed to make **small, pre-approved modifications** to their own harness/configuration.

These writes must be bounded.

Examples:

```text
enable a known capability
adjust a retrieval limit
select a planning mode
attach a predefined skill
change an approved threshold
enable a verifier
change a known execution profile
```

Not allowed:

```text
rewrite arbitrary runtime logic
replace core safety/invariant mechanisms
invent unrestricted tools
silently modify permission boundaries
replace the entire harness
```

A useful model is:

```text
stable base harness
      +
approved configuration surface
      +
small agent-proposed patch
```

Potential representation:

```yaml
planning_mode: dag
retrieval_limit: 12

verification:
  enabled: true

skills:
  - repository_navigation

tool_execution:
  programmatic_blocks: false
```

### Requirements

Agent-authored config changes should be:

- schema validated
- auditable
- reversible
- permission bounded
- limited to explicitly exposed fields
- ideally attributable to a reason / triggering state

This capability can remain dormant in the first pass, but the config architecture should not make it impossible.

---

## 8. Operational Long-Term Memory

### 8.0 Generality is a core requirement

The abstractions must work across conversation, research, software, operations, learning, creative work and contexts not yet anticipated. Mira and named projects are consumers and test cases, never mandatory schema concepts. Generality means stable reusable contracts plus optional domain policies, not one universal ontology that every task must satisfy.

Use a small core: **subject/entity, event, claim, procedure, intention, work context, artifact, evidence, scope, time and provenance**. A task is an optional kind of work context; a project is an optional grouping. Permit interaction without an explicit task, an event without a project, work spanning several contexts, and a one-shot run with persistence disabled by policy. Authentication/ownership remains required even when project/task metadata is absent.

Core envelopes contain identity, version, type, scope, time and references. Typed, versioned payload extensions carry domain fields. Do not require every subject to be a person, every artifact to be a file, every procedure to have executable steps, or every outcome to be binary. A simulation state, physical measurement, image region or audio interval can be referenced without flattening its content into prose. Initial retrieval may be text-based, with captions/extractions explicitly linked as derivations rather than substituted for the original modality.

Domain adapters supply entity identifiers, payload validation, source-authority rules, freshness classes, activation conditions and verification predicates. Policies supply budgets and retrieval strategies. Provider adapters supply token counting, message formatting and lifecycle integration. The memory core must not import a repository, calendar, home-automation or model-provider ontology. Keep extension contracts small; no plugin marketplace or general workflow DSL is needed.

A reference has a source kind, stable source ID, revision or observation time, optional locator/span and access context. This supports external documents, APIs, research sources and tool observations through the same ingestion contract. The original external system remains authoritative where applicable; fetched snapshots become attributed observations. Discovery does not automatically promote every fetched page into durable memory.

Replace fixed personal/organization enums with extensible scope records; those names are deployment conventions. `project_id` and `task_id` below are conveniences over context links, not mandatory fields on every record. Durable direct lookup must also work for a conversation or event context without an explicit task.

**Generality gate:** run the same lifecycle and retrieval contracts on (1) a preference change across conversations, (2) research with revised evidence and an unresolved hypothesis, (3) operational recovery after a failed action, and (4) creative work with evolving constraints and non-binary review. The core schema and service code stay unchanged; only payload schemas, adapters and policies differ. Include a project-free event, multiple simultaneous goals and a non-text evidence pointer. These tests demonstrate useful breadth, not a proof of universal applicability.

### 8.1 Ownership and reuse

| Component | Responsibility in this pass |
| --- | --- |
| MiraDB | Existing CRUD observations and source material for this design. No conversion into Miragen's memory backend. |
| Loimi | Central durable artifacts and reusable retrieval/storage capabilities: artifact identities, source/run attribution where available, supersession/derivation, embedding jobs and hybrid search. |
| Miragen memory subsystem | Memory semantics, scoped current state, lifecycle, task representations, consolidation policy, retrieval orchestration and context budgets. |
| Miragen harness adapters | Invoke lifecycle hooks and insert memory packets into the actual model request. Expose explicit search/read tools as well. |
| External systems | Authoritative mutable operational facts. Memory keeps observations and references; it does not replace a booking database, scheduler or repository. |

Use a central persistence path with a Miragen-owned memory API and a Loimi adapter. Loimi runs as a separate service, backed by durable Postgres storage independent of agent containers. Do not embed Loimi in each Miragen container or create one independent memory database per agent. Logical ownership by Miragen does not require a new network service: an internal module plus persistence tables and workers is enough initially.

Loimi's July records report immutable artifacts, supersession, hybrid search with full-text fallback, embedding workers and model/revision provenance. Reuse these after checking current contracts. They do **not** establish that Loimi already has temporal claim resolution, safe cross-agent scope enforcement, memory consolidation or harness injection. Keep these gaps explicit. In particular, namespace/relevance tags must not be assumed to be access controls. [M211, M212, M355, M375, M379]

The inspected Loimi search/embedding paths need additions for memory correctness; add the narrow `/memory/v1` persistence extension and dedicated `memory` schema proposed in §17 within the same central store. Avoid an uncoordinated second source of truth or best-effort dual writes.

### 8.2 Memory type, scope and lifetime are independent

“Project memory” is a scope, not a competing memory type. An episode, procedure, intention or fact can each belong to a project or task.

| Type | Contents | Update behavior |
| --- | --- | --- |
| Episodic | Meaningful events, actions, failures, decisions and outcomes | Append events; correct by adding attributed corrections. |
| Semantic | Current factual claims, preferences, decisions and stable relationships | Version claims; resolve temporal applicability and conflicts explicitly. |
| Procedural | Reusable methods, preconditions, failure modes and verification steps | Version procedures; retain applicability such as repository revision or tool version. |
| Prospective | Unresolved intentions and desired future states | Track candidate/open/resolved/cancelled/expired/superseded lifecycle. |
| Working state | Active goals, constraints, open questions, evidence and pending actions | Revisioned task state, directly loaded rather than similarity searched. |

Scopes are explicit identifiers: tenant/owner, personal or organization space, project, task and optional agent-private scope. Producer agent/run is attribution, not the visibility policy. Session/run IDs identify where a memory arose; they must not make task memory disappear when a run ends.

Project memory survives individual tasks. Task memory survives retries, compaction, agent changes and restarts. Agent-private memory is available only to that identity unless explicitly promoted under configured policy. Shared project episodes can inform multiple authorized agents without copying every episode into each agent's private store.

For Mira, stable identity/preferences, open intentions and project links persist across bounded runs. A personal preference can influence style without becoming an organizational fact. A speculative wish becomes a candidate intention, not automatically a scheduled task. Retrieval of an intention grants no permission to act. Domain integrations such as geofencing remain separate consumers. [M600]

### 8.3 Write path and multi-agent consolidation

1. Capture meaningful source events with authenticated producer/run/task IDs, occurrence time, receipt time, source reference and an idempotency key. Preserve tool execution status as runtime evidence.
2. Commit the event and a consolidation/indexing outbox entry atomically. A retried delivery of the same source event must not create another episode.
3. A bounded worker proposes typed claims, episode summaries, procedures or intentions. Store source spans/references, extractor version and a distinction between direct observation, user assertion and inference.
4. Validate scope, source accessibility, schema, temporal fields and expected current revision. Consolidation is allowed automatically under configured policy; uncertain conflicts remain unresolved rather than creating routine human approval work.
5. Commit accepted revisions and current-head/conflict projections transactionally; queue derived embeddings. Workers may retry without duplicating claims or supersession edges.

Agents write observations or proposals through the API; they cannot assign themselves broader visibility or directly set authoritative current heads. Two agents repeating the same original evidence are not two independent confirmations. Preserve source lineage and correlation; do not convert repetition into confidence.

Each claim has a stable identity key such as `(space, project, subject, predicate, qualifiers)`. The key must distinguish environment, branch and person where relevant. Keep changing validity intervals out of the identity key; otherwise a changed fact cannot reliably supersede its earlier version. Cardinality must be explicit for predicates that support current-state replacement. Different qualifiers may make apparently contradictory claims compatible. Semantic similarity can propose a match but cannot prove identity or authorize overwrite.

A missing real-world source is allowed and represented honestly as unknown. Such records can be retained as tentative observations but cannot enter the default factual-recall lane. Miragen can still attach actual ingest/run provenance; it must not fabricate supporting evidence to fill a mandatory field. This preserves Loimi's datastore-first, optional-provenance direction. [M375]

### 8.4 Revision, supersession, staleness and conflict

Maintain immutable claim revisions plus a current projection that can change transactionally. “Overwrite” means replacing the active projection with a new revision; it does not mean silently editing history.

Use separate fields for lifecycle (`active`, `superseded`, `retracted`), admission (`candidate`, `accepted`, `quarantined`, `archived`), assertion mode (`reported`, `inferred`, `hypothetical`), dispute status and verification receipts. Freshness (`fresh`, `stale`, `unknown`) is computed from source revision and policy at read time, not trusted as a stored boolean. A blanket `verified` flag is insufficient; verification attaches to a specific claim revision and applicability. A stale claim is not automatically false, and a recent claim is not automatically correct.

| Event | Required behavior |
| --- | --- |
| Explicit preference/decision change with matching scope | New revision becomes applicable from its effective time; older revision remains historical. |
| Correction of an erroneous observation | New correction/retraction links to the old revision; distinguish correction from a real-world change. |
| Two incompatible claims with overlapping validity and no decisive source | Preserve alternatives and a conflict record. Do not choose by insertion timestamp or cosine score. |
| Two concurrent updates based on the same revision | Compare-and-swap rejects the stale update; retry against new state or create a conflict. No silent last-writer-wins. |
| Freshness deadline passes without new evidence | Mark stale/recheck-needed; do not invent a replacement value. |
| Authoritative source changes | Add the new observation and revise affected claims; invalidate direct dependent summaries/caches. |
| Summary or interpretation is produced | Link `derived_from`; it does not supersede the underlying episode or source. |

Store both **valid time** (`valid_from`, `valid_to`: when the claim applies in the world) and **record time** (`recorded_at`: when the system learned it). Keep `observed_at`, `last_verified_at` and optional `review_after` for freshness. Unknown effective dates remain unknown. Support “what applied at time T?” separately from “what did we believe as of T?”; corrections arriving late must not erase that distinction. [M370]

Source precedence is predicate-specific and configured: the user defines their preference; a successful runtime result defines execution status; a live source system defines its current operational state. A model's summary cannot override those sources. Do not use a universal agent seniority ranking or an invented probability of truth.

Resolve current heads and validity in canonical state **before emitting context**, including when a vector index, parent summary, exact-ID hit or cache returns an old revision. A superseded match may lead to its current successor, which is rechecked for scope, authorization and relevance. If the successor is inaccessible, omit it without leaking its existence or contents. History mode may return old revisions explicitly labeled as historical.

Time-sensitive claims use configured freshness rules. Revalidate before consequential use when policy requires it; if unavailable, label unknown/stale and use the task's evidence gate. Retrieval caches include scope/policy and memory revision versions, not only query text. Store flattened root-source references for every derived memory in this pass, and check them before injection. Retraction/deletion must also invalidate derived content transitively through these roots. A general incremental computation DAG remains deferred; correctness for invalidated evidence does not.

### 8.5 Task vectors and situation representations

Keep two related representations distinct:

- **Active task representation:** goal, current subtask, constraints, entities, project and unresolved questions, with facet embeddings used for retrieval and optional eviction scoring.
- **Stored episode representation:** situation/activity, transition, relevant entities, temporal/social context, action and outcome. This enables retrieval of similar situations across differently worded episodes. [M600]

The structured fields remain inspectable and are not hand-assigned embedding coordinates. Embed short facet texts with one versioned embedding model. Use separate facets for distinct goals rather than averaging unrelated goals into one direction. The active representation comes primarily from explicit working state and user task changes; a long tool result must not redefine the goal. The stored outcome can aid learning from completed episodes, but live queries must not pretend to know their future outcome. Index situation/action separately from outcome so outcome language does not dominate a query about how to proceed.

A minimal relevance component is:

`semantic_score(memory, task) = max(cosine(memory_embedding, facet_embedding) for facet in task.facets)`

This is a ranking signal, not confidence. Exact entity/qualifier filters prevent broad facets from pulling in irrelevant projects. Keep facet count and expansion bounded. Version and cache the representation by working-state revision; rebuild on material goal changes and task switches. A single goal facet can bootstrap the first increment, but the API must support several. EMA drift is optional and cannot displace explicit task constraints. [M225, M231, M234, M236]

### 8.6 Retrieval and automatic context injection

Automatic injection is part of this pass. Explicit search/read remains available for deliberate deep retrieval. The July pull-oriented note is historical; the later July/August notes explicitly identify proactive harness injection as a missing requirement. [M229, M369, M584, M600]

Retrieval pipeline:

1. Load working state, active task constraints and required scoped current records by identity.
2. Build or reuse task facets. Restrict candidate generation to authorized scopes and requested temporal mode.
3. Retrieve bounded candidate sets through lexical search, embeddings and exact entity/reference lookup. Use Loimi's hybrid facility where suitable rather than implementing another unrelated index.
4. Resolve temporal/current-state eligibility and conflicts against canonical records; discard deleted, unauthorized and unusable stale results.
5. Optionally expand hits to source episodes, parent summaries, nearby material and derivation/supersession links, with limits on hops, count and tokens. Reapply authorization to every expansion.
6. Deduplicate by claim/episode/source identity; rank by relevance with explicit recency/applicability rules. Use documented deterministic scoring or rank fusion; do not present a ranking number as truth confidence.
7. Assemble a small, budgeted context packet, with memory IDs, revisions, source references, freshness and a retrieval reason per item.

Default retrieval is flat/collapsed hybrid search plus optional structural expansion. A cluster tree is not needed to ship this memory subsystem. Tree traversal and learned rerankers are later optimizations requiring evidence against the shipped baseline. Old notes claiming all flat retrieval requires exhaustive scanning or that tree retrieval is lossless/novel are not adopted as technical claims. [M136 → M225 → M231 → M234]

Run the hook on task start/resume, meaningful incoming user/event transitions, goal changes and after context restoration. Before subsequent model turns, check whether state/memory versions changed; reuse a valid packet rather than blindly running full retrieval every turn. Avoid triggering repeated retrieval on every token or routine tool output.

The packet separates current facts/preferences, applicable procedures, relevant episodes and unresolved intentions/conflicts. Inject it as attributed reference data, never as system instructions or hidden authorization. Quote-like instructions inside an old memory are still data. Persist a manifest of exactly which revisions were injected and why.

Reserve context for system instructions, user request, explicit constraints, recent interaction and response/tool needs first. Cap the memory section by policy; omit low-value content instead of crowding out the task. If required working state alone cannot fit, perform explicit state/context handling rather than silently truncating constraints. Optional retrieval has a deadline: embedding failure falls back to lexical/exact retrieval, then to working state plus an explicit degraded status. Never misreport an outage as “no relevant memories.”

Harness adapters must report whether injection actually occurred. An adapter without a supported insertion path is explicitly degraded to manual memory tools; that is not accepted as successful automatic-memory integration. [M584]

### 8.7 Bounded runs, compaction and retention

Close or suspend runs at semantic boundaries such as completion, sufficient inactivity or a situation transition. Persist working-state checkpoints and unresolved intentions before closing. A new run resumes the task/identity rather than inheriting an endless transcript. Also checkpoint at meaningful progress points so crashes do not depend on a clean close. [M600]

Run-close harvesting produces separate outputs: episodic summary, proposed durable facts/procedures, updated intention state and situation representation. Raw tool/transcript payloads have a separate archive/retention policy; do not embed every debug log or make the full archive part of ordinary recall.

For context pressure, preserve selected payloads verbatim outside live context and leave a short receipt with gist plus an exact retrieval pointer. Summaries are navigation aids, not replacements for retained evidence. Pin active instructions, constraints, task definition and recent working set. Evict bulky low-relevance payloads by rank under budget pressure; drift alone does not trigger constant churn. Preserve useful failure signatures while the failure remains unresolved. [M236]

Receipts can be compacted into a bounded manifest/digest, but never claim this preserves all meaning losslessly. Fidelity depends on retained accessible source bytes and accurate pointers. When retention deletes a payload, invalidate its receipt and report it unavailable. Long-term memory can preserve selected source excerpts while raw archives expire; make that retention choice explicit.

Deletion/forgetting is a distinct controlled operation: remove or redact eligible source content and derived copies, embeddings and caches; recompute or invalidate dependent summaries. An append-only history policy must not silently defeat deletion. Keep only permitted minimal tombstone metadata.

### 8.8 Reuse verification before implementation

Inspect current Loimi/Miragen code for these concrete questions; they are engineering discovery tasks, not fresh architecture approval gates:

- What artifact, run, lineage and search contracts exist now, and can memory writes be committed atomically?
- Are namespace restrictions actual authorization, and can every search/expansion/read enforce the caller's scope?
- Are supersession edges merely retrieval boosts, or are current heads enforced? The new memory wrapper requires enforcement.
- Where are embedding model/revision identities stored, and how does index rebuild/fallback work?
- Which Miragen adapters can insert pre-turn context and persist checkpoints today?

Retain the current embedding model only after identifying it; old records show changing dimension/model decisions and a provisional choice, not a permanent mandate. Store model/revision/dimension and source revision for every embedding, prevent mixed-space comparisons, and rebuild indexes under a versioned switch. Vector indexes remain disposable projections of canonical memories.

---

## 9. Deferred: Deterministic Control-Flow Separation

The broader principle is accepted:

> Deterministic control should generally live below semantic model reasoning.

Examples include:

- retries
- pagination
- fan-out
- filtering
- aggregation
- timeout handling
- mechanical branch conditions

However, this is still too broad/fuzzy to make part of the initial memory implementation.

Do not prematurely redesign Miragen around this principle during this pass.

### For now

Avoid architectures that make future separation impossible.

Do not require the model to own runtime bookkeeping that clearly belongs elsewhere.

Revisit this after the memory/state layer is established.

---

## 10. Explicitly Out of Scope

The following are **not part of this pass**:

### Recursive / hierarchical multi-agent execution

Interesting, but expensive and not necessary for the initial memory architecture. Shared memory ingestion from already-existing agents is in scope; building a new recursive delegation runtime is not.

### JIT harness synthesis

Do not generate entire harnesses per task.

### Speculative agent execution

No token-burning prediction of future action chains.

### KV-cache-as-runtime / asynchronous model streams

Interesting research direction. Too experimental for the current production architecture.

### Full workflow compiler / portable execution IR

Worth preserving architectural room for, but not required now.

### General dependency-DAG computation engine

Keep root-source indexing and invalidation of every dependent memory in scope. Defer arbitrary recomputation graphs, optimizers and general workflow dependency scheduling.

---

## 11. Initial Contracts and Storage Model

These are proposed internal contracts, not claims about existing Loimi endpoints. Map them onto current store capabilities after §8.8; avoid gratuitous API duplication.

| Record | Required shape |
| --- | --- |
| Memory envelope | Stable ID/revision, type, scope IDs/visibility, producer agent/run, source refs, schema version, recorded/observed times |
| Episode | Event ID/idempotency key, task/project refs, meaningful event payload, runtime outcome/evidence, optional archive pointer and expiry |
| Claim revision | Claim key, subject/predicate/value/qualifiers, validity interval, lifecycle/freshness/epistemic fields, source/dependency revisions, supersession/correction refs |
| Current projection | Claim key, applicable revision(s) or conflict ID, projection version; enforce unique resolved head per applicable interval |
| Procedure | Preconditions, steps, validation, applicability/version range, evidence and prior revision |
| Intention | Desired outcome, lifecycle, activation conditions, task/project scope, optional expiry/resolution evidence; no implicit execution permission |
| Working state | Task ID/revision, goal/facets, progress, constraints, unresolved questions, pending actions, artifact/evidence refs, memory refs and checkpoint |
| Embedding projection | Memory ID/revision, facet/chunk ID, content hash, model/revision/dimension, indexing status |
| Context manifest | Run/turn, task-state revision, policy/index versions, included memory revisions, reasons/token counts, degraded retrieval status |

A minimal interface:

```python
append_event(event, idempotency_key) -> event_ref
propose_memory(proposal, expected_revision=None) -> accepted_or_conflict
get_working_state(context_id) -> state_with_revision
patch_working_state(context_id, patch, expected_revision) -> state_with_revision
retrieve(query, context_ref, scope, temporal_mode, budget) -> memory_packet
read_memory(memory_id, revision=None, temporal_mode="current") -> record
resolve_claim(claim_key, resolution, expected_revision) -> claim_or_conflict
close_run(run_id, checkpoint_ref, idempotency_key) -> harvest_job_ref
```

The authenticated caller determines allowed scopes; `scope` can narrow access, never expand it. Claim resolution is subject to source/authority policy and transaction checks, not an unrestricted tool for declaring truth. Explicit-ID reads may inspect historical revisions but return lifecycle labels and never silently pass them off as current.

Internal jobs handle consolidation, embedding, revalidation and retention. Expose a small curated agent tool surface for remember/propose, recall/search and read; keep queue management, raw edge writes and authorization below the model.

## 12. Implementation Milestones and Acceptance Gates

### Milestone 1: Persistence seam and working state

Inspect existing contracts, add the Miragen memory module/Loimi adapter, domain-neutral work-context references, revisioned checkpoints and source/evidence references. Define scope enforcement before shared retrieval.

**Acceptance:** an agent restarts mid-task with the same goal, constraints, pending actions and evidence; another task cannot overwrite it. Required state loads without vector search.

### Milestone 2: Episodic ingestion and current claims

Include admission policy and root invalidation before default factual recall is enabled (§17). Add idempotent multi-agent event ingestion, claim revisions, temporal fields, scoped current/conflict projections and transactional supersession. Capture provenance at write time rather than as a later optional milestone.

**Acceptance:** preference changes preserve history while current lookup returns the new value; a late correction supports valid-time and record-time queries; racing writers create no lost update; unresolved conflicts remain visible.

### Milestone 3: Hybrid retrieval and automatic injection

Implement task facets, scoped lexical/vector/exact retrieval, current-state resolution, bounded expansion, context packets, manifests and harness hooks. Reuse existing embedding jobs and fallback behavior where compatible.

**Acceptance:** a new run receives relevant project facts and a prior episode from another authorized agent without first deciding to call search. A stale index cannot reintroduce a superseded fact. Embedding failure produces a bounded degraded path.

### Milestone 4: Run harvesting, intentions and context recovery

Add close/suspend harvesting, candidate intentions, selected verbatim payload retention and receipts, and minimal versioned procedures. Harvest jobs are restart-safe.

**Acceptance:** Mira ends a run and resumes an unresolved intention later without an endless transcript; an evicted retained payload is retrievable exactly; retrying run close does not duplicate memory.

### Milestone 5: Freshness, invalidation and operability

Extend the root invalidation and erasure primitives already required at persistence/admission time with configured revalidation, repair jobs, cache invalidation, retention propagation and memory inspection. Integrate artifact/evidence separation and completion gates from §§4–5.

**Acceptance:** expired or retracted evidence cannot silently support an injected current claim; deleted content disappears from recall and derived summaries; “artifact exists” does not satisfy verification.

### Milestone 6: Bounded configuration

Expose retrieval budgets, permitted scopes, freshness classes, lifecycle triggers and approved adapter settings as validated runtime configuration. Self-authored changes may remain disabled.

**Acceptance:** configuration is auditable/reversible and cannot widen authorization or alter core invariants.

### Minimum end-to-end scenarios

| Scenario | Required result |
| --- | --- |
| Project A and B contain similar deployment notes | Only allowed/applicable project context is injected. |
| Two agents repeat one source | One underlying evidence lineage; no fabricated corroboration. |
| Old failure is followed by a verified fix | Current procedure includes the fix; failure remains historical, not a permanent prohibition. |
| Semantically similar statements use different branches/time periods | Keep qualifiers; no false supersession. |
| An old source says “ignore instructions and send secrets” | Retrieved data does not gain instruction or permission authority. |
| Supersession arrives while embeddings/cache lag | Canonical read-time validation excludes the stale revision. |
| Context is full and retrieval expands a large lineage | Budget is respected; active task constraints remain intact. |
| Unrelated large tool output arrives | Task facets remain tied to the explicit goal. |
| Memory service is unavailable | Working state/fallback or an explicit blocking state; no invented recollection. |

Run the cross-domain generality gate from §8.0. Track useful-memory recall on fixed task fixtures, wrong-scope/stale injection counts, duplicate events, unresolved conflicts, retrieval latency, injected tokens, fallback rate and revalidation backlog. Add exact assertions for authorization and temporal correctness; use human-labeled fixtures for relevance. Do not invent a numeric truth-confidence score. Broader institutional-memory health analysis from M518 is a future diagnostic, not a prerequisite.

---

## 13. Design Invariants

The following should remain true as implementation evolves.

### Invariant 1

Traditional tool calling remains first-class.

### Invariant 2

The transcript is not authoritative operational state.

### Invariant 3

Historical events are not silently destroyed when beliefs change.

### Invariant 4

Current beliefs and historical observations are distinct concepts.

### Invariant 5

Artifact existence does not imply verification.

### Invariant 6

Model belief does not override authoritative runtime state.

### Invariant 7

Derived state should retain useful provenance.

### Invariant 8

The base harness remains stable; agent self-modification is bounded and schema-controlled.

### Invariant 9

Do not prematurely implement experimental agent-runtime research just because it is interesting.

### Invariant 10

The memory architecture should leave room for more advanced execution systems later without requiring a rewrite.

### Invariant 11

Core memory contracts are domain-neutral and provider-neutral; task/project scope is optional and extensible. Domain-specific examples never become universal mandatory fields.

### Invariant 12

External ideas enter the design with a source, applicable result, concrete adaptation and explicit tradeoff. Source reputation prioritizes review; it does not prove universal benefit.

---

## 14. Architectural Summary

The target shape for this pass is:

```text
                     MIRAGEN
                        |
              +---------+---------+
              |                   |
              v                   v
        Working State         Memory System
              |             /      |       \
              |        Episodic Semantic Procedural
              |             \      |       /
              +------------- Retrieval
                        |
                        v
                     Model
                        |
             +----------+----------+
             |                     |
             v                     v
      Traditional Tools      Future Programmatic
                                Action Blocks
             |                     |
             +----------+----------+
                        |
                        v
                     Runtime
                        |
          +-------------+-------------+
          |                           |
          v                           v
   Artifact State              Evidence State
          |                           |
          +-------------+-------------+
                        |
                        v
                   Environment
```

The central idea is not to make Miragen more elaborate for its own sake.

It is to establish clean boundaries:

- memory vs transcript
- history vs current belief
- artifact vs evidence
- model belief vs runtime truth
- atomic tools vs future programmatic actions
- stable harness vs bounded configuration
- present implementation vs experimental future architecture

Those boundaries are the foundation. This revision makes the memory boundary operational: existing agents write durable evidence, the subsystem maintains scoped current claims, and the harness receives bounded relevant context automatically.

## 15. Source Register and Revision Rationale

Full observations were read from MiraDB on 2026-09-12. IDs are lookup identifiers for `get_observation`, not web citations. Historical implementation reports are dated evidence and must be checked against current code. No MiraDB records were modified during this design pass.

| Ref | Date | Observation and use |
| --- | --- | --- |
| M136 | 2026-06-07 | “hierarchical RAG tree for agent context eviction”: origin only; its k-d-tree/default-descent and performance claims are not adopted. |
| M211 / M212 | 2026-07-02 | Canonical artifact schema and API/DDL refinements: central Postgres/pgvector direction, immutable artifacts, curated API, runtime/store boundary. Later records amend details. |
| M225 | 2026-07-03 | “semantic cluster tree v2”: preserves leaves and task-oriented eviction; explicitly revises M136. |
| M229 | 2026-07-03 | “Loimi relevance layer + raw-data carve-out”: provenance-aware expansion, authorization, raw/semantic separation; pull-oriented injection later revised. |
| M231 | 2026-07-03 | “second pass: collapsed retrieval, semantic layer”: collapsed retrieval replaces default tree descent. |
| M234 | 2026-07-03 | “amendments locked: hit expansion, build order”: structural expansion and multi-facet task-vector direction; simple baseline may ship first. |
| M236 | 2026-07-03 | “eviction v0: receipts over demotion, hybrid”: budget-pressure eviction, retained source payloads and receipts. No adoption of unqualified losslessness claims. |
| M346 / M355 | 2026-07-18 | Loimi relevance plan and resolved amendments: model remains provisional; shadow-only tagging overrides initial auto-promotion proposal; mechanical completion versus interpretive derivation. |
| M369 | 2026-07-20 | “Gap: proactive retrieval missing from miragen plan”: automatic retrieval must actually reach the executor/harness. |
| M370 | 2026-07-20 | “Loimi ontology: as-of/staleness gap, superseded-by”: temporal validity and implicit staleness remain distinct from explicit supersession. |
| M375 | 2026-07-21 | “Loimi datastore-first ontology shift”: central heterogeneous store, honest nullable provenance; memory machinery layered above it. |
| M379 | 2026-07-22 | “Loimi v1 MERGED to main”: historical report of embedding worker, hybrid fallback, immutable artifacts and shadow-only ladder. Not a live deployment check. |
| M518 | 2026-08-13 | “Multi-agent memory health metrics”: optional diagnosis of stale failure beliefs and provenance quality. |
| M584 | 2026-08-23 | “Harness hooks for automatic RAG”: runtime-owned injection with explicit retrieval retained. Specific third-party hook availability is not asserted as current. |
| M600 | 2026-08-25 | “Mira personal-ops memory + attention model”: bounded runs, separate harvest products, situation vectors, prospective memory and small context packets. |

**New synthesis in this revision:** orthogonal scope/type/lifetime model; claim keys and concurrency rules; separate lifecycle/freshness/epistemic fields; temporal/current projection enforcement; transactional ingestion/outbox boundary; versioned context manifests; explicit degradation and acceptance cases. These make the earlier ideas implementable and reviewable without pretending they were already implemented or previously approved in this exact form.


## 16. External Evidence and Adoption Decisions

Sources checked on 2026-09-12. Review priority follows the requested order: **OpenAI/Anthropic directly → relevant research with clear actionable results and small costs → other reference implementations**. This is a design review, not a claim of exhaustive literature coverage or a reproduced benchmark. No external package was installed or tested here.

“Little to no drawbacks” is an adoption filter: prefer changes with a clear mechanism, bounded overhead, reversible integration and no new architectural dependency. No candidate is assumed universally free of tradeoffs. Distinguish provider engineering guidance, controlled benchmark results, author-reported results and implementation examples. Do not transfer a benchmark gain into an expected Miragen gain without a matched evaluation.

### 16.1 Highest priority: direct provider guidance

| Source and evidence | Concrete adaptation | Decision and cost |
| --- | --- | --- |
| OpenAI, *Context Engineering — Short-Term Memory Management with Sessions* (2025-09-09), runnable cookbook: compares last-N trimming with summary-based carry-forward; trimming loses older context, while summaries add calls and distortion risk. [Source](https://developers.openai.com/cookbook/examples/agents_sdk/session_memory) | Treat session history management as distinct from durable memory; budget context, retain provenance for summaries, and keep tool-call/result groups coherent during trimming. | **Adopt in this pass.** Retained sources and manifests cost storage. Do not copy the in-memory demo as durable persistence or assume summaries correct prior errors. |
| Anthropic, *Effective context engineering for AI agents* (2025-09-29): recommends focused context, reference-based just-in-time loading, structured notes and task-dependent combinations of up-front and agent-driven retrieval. [Source](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | Small automatic memory packets plus explicit search/read; lightweight pointers to bulky retained payloads. Preserve active goals outside the transcript. | **Adopt in this pass.** Automatic retrieval can add noise; explicit exploration adds latency. Bounded injection and exact reads retain both paths without an always-on deep-search loop. |
| Anthropic, *Effective harnesses for long-running agents* (2025-11-26): reports progress artifacts, bounded increments and explicit verification improving continuity in a web-development harness. The article explicitly leaves broader-domain generalization open. [Source](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | Generalize progress records to work-state checkpoints and verification to domain-supplied evidence predicates. Resume by loading state and checking relevant reality. | **Adopt the mechanism, not the coding workflow.** Checkpoint/verification costs remain; do not require git, browser tests, a feature list or separate initializer agent for every task. Cross-domain transfer is our design inference. |

These sources support context/state practices; none independently validates the entire multi-agent temporal memory architecture proposed here.

### 16.2 Research: portable results before elaborate architectures

**LongMemEval — ICLR 2025.** The benchmark tests extraction, cross-session reasoning, temporal reasoning, updates and abstention. Its experiments support finer-grained session decomposition, fact-enriched retrieval keys and time-aware queries. **Adopt now:** test those five abilities and retain source-linked episode segments instead of indexing only one whole-run summary. **Optional after the baseline:** fact-enriched keys and query expansion, which can add extraction errors or excess matches; enable only if fixture recall improves within budget. Conversational benchmark results do not establish success on operational task execution. [Paper, v2](https://arxiv.org/html/2410.10813v2)

**Zep — temporal memory paper (2025).** It models both record time and fact-validity time, preserves episode provenance and combines lexical/vector/structural retrieval. Its DMR table reports only a small gain over full context with the same models (94.8 vs 94.4 with GPT-4-turbo), and the authors discuss benchmark limitations. **Adopt the temporal/provenance concepts** in §§8.3–8.4; no performance promise. **Do not adopt its newer-information-first invalidation rule:** conflicting evidence must obey source/qualifier policy. A full graph extraction pipeline brings model calls and entity-resolution errors, so remains unnecessary for this pass. [Paper, §§2.2.3 and 4](https://arxiv.org/html/2501.13956v1)

**Are We Ready For An Agent-Native Memory System? — 2026 preprint.** Its multi-system evaluation finds workload-dependent effectiveness and favors localized maintenance over repeated global reorganization in its measured cost/utility comparison. **Adopt only the inexpensive engineering direction:** update directly affected records, measure ingestion as well as retrieval, and evaluate several task families. This supports modularity but does not prove our chosen baseline optimal. Do not import its full evaluation stack as an implementation dependency; results remain tied to the tested configurations. [Paper, §§4.1–4.5](https://arxiv.org/html/2606.24775v1)

### 16.3 Reference implementation: inspect selectively

**Graphiti** is useful concrete reference material for evolving facts, episode provenance, temporal queries and custom types. Its own README lists graph-backend and model-service requirements. **Use as a design/code-reading reference, not a default dependency:** Miragen already has a central-store direction, and adopting the whole graph stack would create additional operations and migration work. Any later reuse requires inspection of the specific module/version and license; this pass inspected the repository documentation, not an implementation correctness audit. [Repository](https://github.com/getzep/graphiti)

### 16.4 Changes that enter the actual implementation plan

1. **Coherent context trimming:** never retain an orphan tool result or remove a tool call while leaving an invalid provider message sequence. Delegate provider-specific grouping to the adapter. Add a resume/compaction fixture that validates the resulting request structure.
2. **Evidence-level retrieval fixtures:** label the source records needed to answer a query, not only a plausible final response. Separate retrieval failure, incorrect consolidation and generation failure in diagnostics.
3. **Temporal query interpretation:** extract an explicit time constraint when the request supplies one; preserve unknown/ambiguous time rather than silently filtering away evidence. Both valid-time and record-time modes are covered by tests.
4. **Bounded maintenance:** consolidation operates on changed events and candidate related claims. Global re-summarization or graph rebuilding does not run on every write. Track queue lag as well as query latency so new memories do not remain invisible unnoticed.
5. **Domain-neutral handoffs:** check shared contracts against the four families in §8.0. Carry over mechanisms from coding examples only through generic state/evidence interfaces.

These additions are part of Milestones 1–5, not a separate research platform. For each later candidate keep a short decision entry: problem addressed, primary source/version, measured setting, proposed adaptation, cost/failure modes, acceptance fixture, and adopt/defer/reject. More complex trees, model-driven global rewrites, multi-agent retrieval ensembles and parametric memory remain experimental/deferred unless they solve an observed baseline failure with acceptable cost.

## 17. Red-Team Review and Concrete Implementation Proposal

### 17.1 Verdict and observed failure modes

The most serious risk is making unreliable information durable and easy to retrieve. Provenance alone authenticates origin; vector similarity only finds related text; neither establishes truth, applicability or usefulness. The design needs separate controls at capture, admission, consolidation and read time.

Targeted source inspection used Loimi commit `d400d5d4a6fa7553f82c57eb9e5b1a2125062ed6` and Miragen commit `1fa46a4f3ee2e05c8704411e4ee78edf4674666c`. Documentation/dependency files were also read from default branches. This was a read-only review, not a complete security audit or a live deployment test.

| Finding | Failure it permits | Proposed decision |
| --- | --- | --- |
| Loimi search applies a default 0.25 supersession penalty; it does not enforce current heads. | A highly similar obsolete fact still appears; a successor outside the candidate set cannot win. | Memory search selects eligible current revisions first and validates again before emission. Do not use `/v0/search` output directly as a current-memory packet. |
| Query embedding returns only a vector; model/revision metadata is dropped and only dimension is checked. | Two incompatible 1024-dimensional spaces compare without an error. | Require a full embedding-space identity on query and document sides; mismatch triggers lexical fallback and an observable error. |
| Loimi search indexes English-analyzed content plus all properties as text. | Metadata noise affects relevance; Finnish and identifiers lack a deliberately designed lexical path. | Curated `search_text`, a `simple` lexical channel, optional language-specific channels and exact identifiers. |
| Loimi's documented bearer/OAuth posture grants broad artifact access; caller-provided agent slugs can be auto-minted. | Claimed agent identity or a namespace cannot establish authority or privacy. | New memory endpoint authenticates a registered principal and capabilities; producer identity is server-derived. Old credentials confer no memory capability automatically. |
| Original schema prevents artifact/edge deletion. | “Forget” cannot truthfully mean deletion if sensitive memory is copied there. | Keep revocable memory content in the dedicated memory schema; implement erasure there. Link existing artifacts without silently copying private memories into generic artifact/blob storage. |
| Miragen has model-tier `run`/`run_stream` and self-harnessed executor `run_job` paths. | Adding a hook to one path silently leaves others without memory, or claims per-model-turn control over an opaque executor. | One common boundary API, capability-tested adapters, explicit injection granularity. |
| The draft deferred dependency invalidation while promising stale-summary protection. | A corrected root fact survives inside summaries of summaries. | Flatten root-source dependencies and enforce invalidation now; defer only a general computation DAG. |
| No precise admission or abstention mechanism was specified. | Trivial events, guesses, jokes and generic advice accumulate; nearest-neighbor search always returns something. | Admission policy and an explicit relevance-selection step that may emit zero memories. |

Evidence: [Loimi search implementation](https://github.com/ieepirzy/Loimi/blob/d400d5d4a6fa7553f82c57eb9e5b1a2125062ed6/src/loimi/service.py), [embedding client](https://github.com/ieepirzy/Loimi/blob/d400d5d4a6fa7553f82c57eb9e5b1a2125062ed6/src/loimi/embedding.py), [schema](https://github.com/ieepirzy/Loimi/blob/d400d5d4a6fa7553f82c57eb9e5b1a2125062ed6/schema.sql), [Loimi deployment/auth documentation](https://github.com/ieepirzy/Loimi/blob/main/README.md), [Miragen model entry points](https://github.com/ieepirzy/miragen/blob/1fa46a4f3ee2e05c8704411e4ee78edf4674666c/miragen/app.py), [executor base](https://github.com/ieepirzy/miragen/blob/1fa46a4f3ee2e05c8704411e4ee78edf4674666c/miragen/executor/base.py).

The current Loimi service also has `as_of` and a `skip_provenance` path: older notes describing these as entirely absent are stale. Neither feature alone supplies the full temporal resolution or admission model required here. README sections themselves contain older status text, which is why concrete code paths take precedence over prose status claims.

### 17.2 Technology choices

| Area | Proposed choice | Reason and boundary |
| --- | --- | --- |
| Persistence | Existing Loimi PostgreSQL 16 deployment; dedicated `memory` schema | One transactional authority, backups and operations stack. No forced Postgres major upgrade. |
| Vector engine | **pgvector**, cosine distance; exact filtered search as correctness baseline, HNSW for larger eligible sets | Reuse stack; maintain an exact baseline and fallback rather than assuming approximate recall. Pin the deployed extension version; iterative scans require 0.8.0 or later. |
| Language/framework | Python 3.12+, Pydantic v2 contracts; existing FastAPI + asyncpg on Loimi; PydanticAI/httpx on Miragen | Fits inspected dependencies. Keep SQL explicit and use Loimi's migration mechanism; no new ORM or agent framework. |
| Embeddings | **BAAI/bge-m3 dense, 1024 dimensions**, pinned model revision and preprocessing configuration, through Loimi's endpoint contract | Concrete bootstrap aligned with existing code. This is a compatibility choice, not a claim of best current quality. Benchmark Finnish/English and exact-entity failures before choosing a replacement. |
| Lexical retrieval | PostgreSQL `tsvector`/GIN on curated text; `simple` always, language-specific configuration when known | Preserve technical identifiers and mixed-language text. This is PostgreSQL text search, not BM25. Store source identifiers in separate indexed fields. |
| Ranking | Reciprocal rank fusion of lexical and dense candidate ranks, then bounded relevance selection | Avoid arbitrary sums of incomparable raw scores. Do not turn relevance-tag confirmation into truth weighting. |
| Background work | PostgreSQL job/outbox table with leases, retries, idempotency and `SKIP LOCKED` claiming | Reuse Loimi worker conventions. No Kafka, Redis queue or Celery required for v1. |
| Extraction/selection | Bounded structured-output jobs using Miragen's configured PydanticAI model | No standing librarian in this pass; retain a capability-limited maintenance job interface for a future librarian. Deployment pins `memory_model` to a tested provider/model; use the main model initially rather than an unvalidated cheaper substitute. |
| Caching | Per-process bounded cache for query embeddings/selection IDs only | Cache keys include model, policy, context and scope versions. Rehydrate canonical content before injection; no persistent Redis cache initially. |
| Observability | Existing OpenTelemetry vocabulary plus Postgres audit/manifest rows | Trace capture → admission → retrieval → injected revisions, with content redacted from general telemetry. |

Pgvector explicitly documents reduced approximate-search recall under filtering; SQL authorization predicates do not guarantee a physically prefiltered HNSW index. Use filtered exact search for small scopes, inspect query plans, and use iterative scans plus exact fallback when approximate results underfill. Partition by real trust domain if separate tenants later share approximate indexes. Strict distance ordering is not an exact-recall guarantee. [Pgvector documentation](https://github.com/pgvector/pgvector#filtering)

BGE-M3's primary model card documents multilingual support and 1024-dimensional dense output; this does not validate Miragen-specific retrieval quality. Pin weights plus tokenizer/preprocessing/normalization identity, verify finite nonzero vectors, and switch a new embedding projection atomically after reindexing. [Model card](https://huggingface.co/BAAI/bge-m3)

### 17.3 Concrete ownership and integration

**Loimi owns persistence enforcement:** add `/memory/v1` routes, capability authentication, memory SQL repositories, revision/admission transitions, jobs, current search and source retrieval. A memory write commits its event, record revision, projection change and indexing job in one database transaction. The old artifact API keeps its existing contract.

**Miragen owns the cognitive lifecycle:** add `miragen/memory/{contracts,client,policy,context,lifecycle}.py` as proposed modules. They decide when to capture, request retrieval, checkpoint and prepare a context packet. A separate worker process from the Miragen package performs bounded model extraction through the same memory API; it receives no database admin credential. Loimi still validates every proposed transition. Version request/response schemas; compatibility tests bind the two repos without requiring an extra shared package initially.

**Existing execution journals remain journals.** Miragen already exposes durable run/event identity. Import events idempotently using `(deployment_id, run_id, seq)` and checkpoint watermarks; do not claim atomicity across a local runtime journal and remote Postgres. An event remains pending until its durable store acknowledgement. Missing synchronization is visible, not silently reported as “remembered.” Required working-state writes must succeed or the run reports persistence degradation before claiming a durable handoff. A crash with an unknown side effect triggers reconciliation, not automatic action replay.

**Model tier:** use one `prepare_context`/`finish_turn` helper for normal, streaming and autonomous entry points. For internal model calls, verify the installed PydanticAI hook surface and use an adapter there; do not infer capabilities from the latest SDK docs. Inject packets as transient request data with IDs, not appended permanent history messages. Capture original source turns, never the injected packet as a new independent observation.

**Executor tier:** prepare context at `ExecutorBackend.run_job`, before adapter dispatch, and observe the common normalized event stream afterward. A provider's internal tool/model turns are opaque unless a real supported hook exists. Advertise `boundary_injection`, `internal_turn_refresh`, `context_replacement` and `evidence_capture` separately. Repeatedly appending new packets to a persistent provider thread leaves old poison behind: when invalidated content cannot be removed, start a fresh thread from checkpoint plus clean context, preserving the workspace. Unknown in-flight operations are reconciled before continuation.

**Authorization:** an authenticated capability grants read/propose/revise/retract for named scopes. Do not let the agent choose its authenticated producer ID or authoritatively label a source “human.” Memory routes use a restricted connection role; migration/maintenance roles are separate. Enable RLS and test every table/read/expansion path, with no superuser, owner bypass or `BYPASSRLS` role in the request path. Set request identity transaction-locally from verified server context, not caller SQL. Agents receive neither SQL access nor credentials that bypass this route. [PostgreSQL RLS](https://www.postgresql.org/docs/16/ddl-rowsecurity.html)

Private source material must not be reachable through Loimi's broad legacy artifact/blob routes. Memory records referring to existing organizational artifacts inherit that organization's actual access boundary; namespace labels do not make an already broadly readable artifact private. Derived records require access to all contributing private roots unless an explicit redacted derivative is approved under policy.

### 17.4 Minimal durable abstractions

Use these eight record families, implemented as tables plus small join/projection tables. Fields specific to a domain belong in versioned payloads.

| Record | Function and important fields |
| --- | --- |
| `SourceEvent` | What was received/observed: authenticated origin, source revision/span, root correlation ID, occurrence/receipt time, content digest, context links, retention. Source text is evidence of what was said, not necessarily truth. |
| `MemoryRecord` + revisions | Typed recall unit: observation, claim, procedure or intention; scope, admission state, payload, source roots and extraction version. One focused assertion or coherent episode, not a whole-run omnibus summary. |
| `ClaimSlot` | Optional canonical current-state key: scope, subject ID, predicate ID, qualifiers, cardinality and version. Only registered predicates get automatic replacement semantics. |
| `EvidenceReceipt` | Claim revision, checked property, subject revision, method, observed result, timestamp and limitations. A record may have supporting and contradicting evidence. |
| `WorkContext` | Optional task/conversation/event grouping, state revision, goal facets, constraints, artifacts and next actions. Many-to-many context links support cross-project work. |
| `SourceRootLink` | Flattened root dependencies of every derived record, indexed both ways. Generated summaries are never new independent evidence roots. |
| `RetrievalProjection` | Curated searchable text, embedding-space ID and vectors, canonical revision, current eligibility. Disposable, rebuildable; separate accepted and archive/candidate access paths. |
| `InjectionManifest` | Actual emitted revisions/source roots, context/policy versions, selection reasons, omission/degradation, token count and trace ID. |

Avoid free-form entity strings as canonical IDs. Use source-system identifiers and exact aliases. An LLM can propose an alias but cannot silently merge people/projects on embedding similarity. Unknown subject or predicate remains an unstructured observation, so new domains work without inventing a schema on the fly.

A `ClaimSlot` declares **single-valued**, **set-valued** or **observation-only** behavior. “Lives in” may have qualified single-valued semantics; “uses programming language” is usually set-valued. An update to one set member cannot erase the others. Negation, uncertainty and scope qualifiers remain explicit. Do not put changing validity timestamps in the slot key.

For single-valued facts, maintain **append-only resolution decisions** with an increasing slot revision. Each decision records the believed valid-time intervals and chosen revisions or conflicts. A mutable current-segment projection is rebuilt from the latest decision. This makes late corrections and “what did we believe then?” representable without editing historical decisions. Serialize per slot with a row lock plus expected revision. Check interval overlap using `tstzrange`/GiST exclusion constraints on resolved segments; use a conflict segment for unresolved alternatives. Unknown validity is explicit and cannot masquerade as an unbounded certainty. [PostgreSQL range constraints](https://www.postgresql.org/docs/16/rangetypes.html#RANGETYPES-CONSTRAINT)

### 17.5 Admission: what becomes memory?

**Capture, retention and default recall are different decisions.** Keep a recoverable event trail under retention policy; only selected content enters the default recall projection. The existing generic “embed every artifact” worker must not indiscriminately index candidates into that projection.

Admission runs deterministic checks first: source exists, span/digest matches, scope permits derivation, no duplicate event, payload limits, source/claim identity and lifecycle are valid. For free text, a model proposes structured memories with exact supporting spans, assertion mode, scope and a concrete future-use reason. A bounded checker assesses whether the source supports the wording, negation and qualifiers. Its verdict is fallible; it cannot turn an inference into external verification. Typed authenticated connector events can bypass linguistic extraction when their mapping is configured.

| Candidate | Default disposition |
| --- | --- |
| Explicit durable preference, constraint, decision or correction from its relevant authority | Accept as an attributed assertion; update a registered slot when applicability is unambiguous. |
| A meaningful event/outcome likely useful for continuation or future reference | Accept as a scoped episode. Personal significance counts; usefulness is not restricted to productivity. |
| Unresolved intention or open question | Retain with lifecycle and activation context; speculative language remains candidate, not a commitment. |
| Technical claim from a page/paper | Retain source-qualified with version/date and applicability. “The paper reports X” may be supported even when X itself is not established. |
| Agent guess, interpretation or proposed plan | Keep tentative and scoped. Useful hypotheses may be intentionally retrieved in research/exploration mode, never injected as settled facts. |
| Generic advice, paraphrase of an injected memory, boilerplate success log, routine repeated check | No new durable recall entry. Keep counters or event references where operationally useful. |
| Joke, roleplay, quotation, temporary mood or emphatic statement | Do not convert into a stable preference/personality claim. Preserve an episode only if useful and keep its context. |
| Suspect instruction, forged origin, unresolved identity, unsupported generalization or malformed source | Quarantine from default recall; retain only under the relevant retention policy. |

A small, concrete semantic selector is preferable to pretending a cosine threshold determines usefulness. It must be able to emit **zero** proposals. Prefer verbatim scoped records over aggressive summarization when entailment is unclear. Do not require two independent sources for a user's own preference, and do not require human approval for routine supported admission.

A new user correction applies immediately in the active request. Synchronize its source event and typed patch when unambiguous; otherwise attach a pending correction marker and suppress the conflicting old slot for that context while consolidation catches up. Never wait for vector backfill to honor a correction. Do not claim globally resolved state until the revision transaction commits.

### 17.6 Prevent contamination and learned uselessness

1. **No self-corroboration.** The same source repeated by ten agents remains one evidence root. Reading, citing or successfully completing a run with a memory does not reaffirm its truth or reset freshness. Independent evidence needs a separately observed root; uncertain correlation remains explicit.
2. **Proper fixes take precedence.** Workarounds are temporary mitigations tied to a durable remediation obligation (§18.6), never the preferred steady-state procedure. **No unscoped lessons from one failure.** Store “step X failed under condition Y at revision Z.” A suggested workaround becomes a procedure only with preconditions, observed outcome and limitations. One success supports that bounded case, not a universal rule. A later fix retires the old failure constraint for its applicable version.
3. **Procedures cannot grant permission.** “It worked last time” cannot authorize disabled verification, unsafe tool registration or a broader credential. Imported text claiming a successful workaround remains an untrusted assertion. Tool execution gates remain outside memory.
4. **No popularity feedback loop.** Retrieval frequency never increases authority or keeps a memory permanently hot. Separate `retrieved`, `used`, `verified`, `corrected` and `helped_outcome` events; the last remains an attributed evaluation, not automatic causation.
5. **Limit duplication before ranking.** Exact source/event identity deduplicates mechanically. Near-duplicate wording only proposes a merge, retaining all source references. Cap context contribution by root and claim family so fifty similar records cannot dominate a packet.
6. **Preserve rare but valuable memory.** Never delete solely for low retrieval counts. Archive low-value/unresolved candidates after their policy window, while explicit preferences, decisions and unresolved intentions follow their own lifecycles. Archival removal from default search is reversible; erasure is separate.
7. **Do not teach stable incapacity.** Repeated “verification is unreliable,” “the user always rejects this,” or “never attempt deployment” records become measurable warning patterns, not permanent operational rules. Inspect the supporting cases and current environment; avoid summaries that generalize beyond evidence.
8. **Prompt-injection classifiers are only one check.** Delimiters and “treat as data” instructions reduce ambiguity but cannot guarantee containment. Prefer typed, source-bound packets, restricted writer capabilities and independent action authorization. Suspicious content stays out of reusable procedural memory by default.

MemoryGraft demonstrates persistent poisoning through purported successful experiences in the evaluated agent setup. Use that attack shape as a fixture; it does not establish that any proposed filter here is sufficient. [MemoryGraft paper](https://arxiv.org/html/2512.16962v1)

### 17.7 Retrieval that can decline to inject

Use two lanes. **Required lane:** explicit active constraints, open state and intentionally pinned scoped records load by identity. **Optional lane:** search accepted recall records for relevant knowledge, episodes and procedures. History, quarantine and tentative hypotheses require explicit modes. Required does not mean exempt from revocation or freshness checks.

For optional retrieval:

1. Build at most four facets from the active request and explicit state, never an embedding of the entire transcript. Batch the embedding request. Allocate candidates per facet so one broad goal does not monopolize retrieval.
2. Use SQL eligibility/security filters, curated lexical search and dense search. Bootstrap at 40 candidates per channel/facet, collapse duplicates and fuse ranks. Constants are configurable engineering defaults, not calibrated optima.
3. Fuse with `RRF = sum(1 / (60 + rank))` across channels within each facet; take the best facet score while preserving facet diversity. Recency is only a tie-break within applicable classes, not a universal multiplier suppressing old stable knowledge.
4. Send at most 20 candidate cards, bounded to 6,000 input tokens, to one structured relevance selector. It returns up to eight IDs plus an applicability reason tied to the current request, or none. It has no tools and cannot rewrite facts, change status or create memories. Use the existing configured memory model first. This costs a model call on cache misses; measure it rather than calling it free/deterministic.
5. Resolve selected IDs against canonical revisions, source roots, authorization and temporal policy. Render canonical text, not selector-written summaries. Include conflicts needed to interpret a selected subject. If required qualifiers/conflicts will not fit, omit the whole optional claim rather than strip the qualification.
6. Emit up to 2,000 optional-memory tokens and at most two cards from one source root; leave unused space unused. The required working-state budget is separately reserved. Record actual emitted revisions and selection provenance.

No rank threshold or top-k list alone means “relevant enough.” With selector failure, automatically inject required/directly requested valid records only; expose semantic search explicitly and report degraded optional recall. Do not fall back to stuffing unvetted nearest neighbors into context. Deep user-requested history search may use a larger explicit budget.

Warm-cache embedding reuse and selector-ID caching are permitted, but every packet rechecks canonical source/state versions before use. A fresh unrelated task can receive zero optional memories. Key freshness updates by affected scope/context rather than globally invalidating every agent on every event. A packet has snapshot semantics: no database can guarantee an external fact stays true after it is read; revalidate relevant evidence at consequential action time.

### 17.8 Invalidation, repair and retention

Every derived record stores flattened original source roots plus immediate parent links. If lineage cannot be completed, it is not eligible for default factual recall. On retraction, deletion, correction or source revision invalidation, increment the appropriate source/slot generation and make dependent projections unusable in the same transaction. Background work can rebuild them later; read-time root checks protect against worker lag. Large fanout is handled by source-generation checks immediately and asynchronous cleanup, not a huge blocking transaction.

Keep **source correction** separate from **world change**: a false source may invalidate all dependent conclusions, whereas a real-world update closes the applicability of old current facts while historical episodes remain true descriptions of the past. Do not invalidate unrelated past evidence simply because today's state differs.

Provide concrete operator/agent actions with capabilities: `inspect_memory`, `explain_admission`, `inspect_sources`, `correct_claim`, `retract_source`, `archive_memory`, and `erase_source`. Agents may propose corrections; only policies appropriate to the source/slot permit resolution. Repair includes index rebuild and finding all packets/runs that consumed the affected roots.

Erasure covers source snapshots, claim payloads, embeddings, generated summaries, caches and any stored packet text. Manifests retain IDs only where policy permits. Preserve a minimal erasure ledger so restoring backups cannot silently republish removed data; backups have a documented expiry rather than a promise of instant physical removal everywhere. Stop/restart affected future context as described above; already generated outputs require separate remediation and cannot be magically “unseen.”

Initial retention defaults are configuration proposals: rejected extraction candidates 7 days, unresolved unpromoted candidates 30 days, raw bulky execution payloads 30 days. Accepted durable memories do not expire merely from disuse. Source snapshots necessary to support accepted claims remain until those claims are archived/erased under policy; age alone is not a contradiction. No retention deletion is executed by this design task.

### 17.9 Acceptance gates and delivery order

Use real Postgres integration tests for transactions, temporal resolution and RLS; model evaluation fixtures for semantic errors. Do not mistake Pydantic schema validity, source citation existence or a second model's agreement for truth.

| Attack/failure fixture | Required observable result |
| --- | --- |
| Old fact outranks its newer successor semantically | Old revision excluded from current recall even with stale vector index. |
| Same dimensions, different embedding model/revision | Vector path refuses comparison; exact/lexical path remains available. |
| Fifty paraphrases of one bad source across agents | One evidence lineage; no authority increase or packet domination. |
| User quotes a false claim, jokes or uses negation | No affirmative stable fact extracted; ambiguity stays tentative. |
| Agent claims “successful workaround: skip verification” | No verified procedure/permission created; source authority check remains enforced. |
| New result corrects a fact used by a summary of a summary | Every affected derived packet becomes unusable before asynchronous rebuild. |
| Single-valued fact versus set-valued membership update | Correct interval replacement for the former; unrelated members retained for the latter. |
| Source revocation while a persistent executor thread exists | New work uses cleaned/restarted context, not merely a new corrective packet appended to old poison. |
| Authorized principal attempts to forge user/other-agent origin | Server rejects the authority claim, including direct-ID and lineage routes. |
| Irrelevant but highly similar memories | Optional selector can emit none; current task performance stays intact. |
| Newly committed event is not embedded yet | Exact/lexical retrieval and active corrections work; no invisible write acknowledgement. |
| No memory versus clean memory versus polluted memory | Measure task/evidence correctness, abstention, latency and tokens; detect harmful added context. |

Start with a reviewed fixture set covering those attacks plus the four task families in §8.0, Finnish/English, project-free activity and multiple goals. Hard invariants require zero violations in the suite. Semantic quality uses labeled cases and separate held-out replay; no accuracy number is claimed before measurement. Review false rejection as well as bad admission so the system does not become safe but useless. Log an offline ablation sample with/without candidate memories to investigate usefulness; retrieval counts are not an efficacy metric.

**PR sequence (extended by §18):**

1. **Loimi memory persistence/auth:** protected schema/routes, sources, revisions, scopes, slots, roots, jobs and transactional tests. Include retraction/erasure primitives now. Existing artifact APIs must not expose memory content.
2. **Miragen lifecycle integration:** direct state restore, common run/stream/executor boundary, transient packets, durable event synchronization and capability reporting. Demonstrate an end-to-end task resume before adding semantic cleverness.
3. **Admission and correction:** typed connector path, bounded text extraction/support checking, registered-slot rules, tentative observations, immediate active correction handling and pollution fixtures.
4. **Pgvector recall:** curated lexical projection, embedding identity validation, exact baseline, HNSW where justified, rank fusion, zero-or-more relevance selection and manifest tracing.
5. **Long-run hardening and observability:** lineage repair, expiry/archival jobs, opaque-thread invalidation, held-out polluted-memory replays, memory-health metrics/diagnostics, collector export and operational budgets. Native Claude Code/Codex hooks, scope resolution and always-supplied agent guidance are part of PRs 1–2, not deferred follow-ups.

Ship a clean end-to-end slice after each step. The release enabling shared durable factual recall requires all relevant correctness gates above. Tree indexes, learned importance scores, global ontology induction, automatic personality rewriting and always-on multi-agent consolidation are deferred.

## 18. Deployment, Observability, Shared Memory and Harness Contract

This section incorporates the user's 2026-09-13 requirements. It refines §§8 and 17 without changing the core distinction between durable state, evidence, retrieval and execution. All new settings below are **proposed**, unless explicitly identified as existing.

### 18.1 Deployment decision: central Loimi service

**Production default: Miragen containers are clients of a separate Loimi service; Postgres owns durable memory.** Destroying/replacing an agent container must not destroy its profile, role or shared memories. Identify agents by stable registered identity, not container ID; use distinct instance/run IDs for attribution. Keep Loimi workers and storage independently restartable. Containers alone provide no durability: the database needs persistent storage, backups and a tested restore path.

Co-location on one machine is fine; separate nodes use the same authenticated HTTP contract. Batch retrieval and source validation to avoid per-memory network round trips. Measure network, database, embedding and model-selection time separately; cross-node latency is acceptable as a deployment choice, not a promised fixed number. Process-local caches are disposable. A local spool holds pending synchronization only and cannot become a competing current-memory authority.

Local development may run Loimi and Postgres as separate services on the same host/Compose project. Embedded Loimi inside the Miragen process/container is not a supported v1 mode; the client protocol leaves room for a future implementation if a concrete need arises. Memory availability is separate from telemetry availability: optional recall may degrade, while required state persistence cannot falsely acknowledge success.

A future **librarian agent** can consume bounded maintenance jobs, inspect sources and propose merges, classifications, revalidation or procedure repairs through the same API. Keep job reasons, budgets, capability grants and idempotency keys now. The librarian gets no unrestricted DB access, cannot redefine authority, and cannot promote content beyond its granted scopes. No standing librarian or unbounded background reflection loop is built in this pass.

### 18.2 What Miragen can emit today

Inspected `miragen/telemetry.py`, `factory.py`, `app.py`, telemetry documentation and tests at the source revision used in §17. Observations concern code, not an exercised collector or deployed configuration.

| Existing behavior | Exact scope / limitation |
| --- | --- |
| Opt-in OTLP/HTTP traces | `MIRAGEN_OTLP_ENDPOINT` is the full **traces URL**, and its presence enables export. Absent means off. `MIRAGEN_OTLP_TOKEN` supports bearer auth; `MIRAGEN_OTLP_AUTH` supplies a verbatim Authorization value and takes precedence. Secret-file loading is supported. |
| Model-tier instrumentation | PydanticAI instrumentation emits model/tool spans with `include_content=False`. Normal `agent.run` is wrapped in a root span and stamps run identity. |
| Executor instrumentation | Durable events are translated after a turn into root/setup/repository/tool/harvest/intervention spans, with status and available token usage. It is reconstructed reporting, not live internal model instrumentation. |
| Identity | Resource: service/version, profile ID/mode, environment. Run attributes: run ID, trigger, model/executor tier; executor backend also recorded. |
| Export behavior | `BatchSpanProcessor`, background export, queue 2048, batch 512, delay 5 seconds. Failed batches/spans counted in process. Queue-overflow reporting relies on SDK logging. |
| Health endpoint | Reports `telemetry.otlp_configured`; it does not establish successful delivery. |
| Other records | Run records/tests include mechanical usage, setup/tool counts and resume accounting. A separate CI build telemetry script exists; it is not a runtime memory-health pipeline. |

**Concrete gaps to fix:**

1. Put streaming calls under the same run-span/context wrapper, including cancellation/failure cleanup; currently `/run/stream` lacks it.
2. Replace executor export's first-10,000-events truncation with bounded pagination and an export watermark. Distinguish replay from new work so retries do not double-count usage or metrics. Retain provenance for reconstructed spans.
3. Do not infer tool duration from completion events: the current reconstructed tool span has identical start/end timestamps. Emit an instant event when start time is unknown; use matched start/end IDs when the harness supplies them.
4. Remove raw `miragen.tool.command` from default export. It currently copies command text with a 500-character cap. Audit exception/status strings too: truncation and `include_content=False` are not comprehensive redaction. Default to allowlisted tool identity/result codes plus restricted source references.
5. Add runtime metrics and selected structured diagnostic logs, rather than deriving all health from sampled traces. Add service/instance identity and cross-service trace propagation on Miragen → Loimi → worker/embedding calls; async jobs use explicit trace links where appropriate.
6. Expose last successful export time, failure/drop counters and backlog/coverage where actually measurable. Unknown SDK queue depth remains unknown. Validate shutdown behavior: `force_flush` has a timeout, but the subsequent provider `shutdown()` call in current code has no explicit timeout; do not assert the whole shutdown is bounded without testing it.

Source references: [telemetry implementation](https://github.com/ieepirzy/miragen/blob/1fa46a4f3ee2e05c8704411e4ee78edf4674666c/miragen/telemetry.py), [instrumentation factory](https://github.com/ieepirzy/miragen/blob/1fa46a4f3ee2e05c8704411e4ee78edf4674666c/miragen/factory.py), [app integration](https://github.com/ieepirzy/miragen/blob/1fa46a4f3ee2e05c8704411e4ee78edf4674666c/miragen/app.py), [existing configuration](https://github.com/ieepirzy/miragen/blob/1fa46a4f3ee2e05c8704411e4ee78edf4674666c/docs/telemetry.md).

### 18.3 Collector configuration and telemetry storage

Extend existing OTel support. Do not implement a custom telemetry transport or store high-volume spans/metrics in the memory vector index.

```yaml
# Proposed profile configuration; these fields are not shipped yet.
memory:
  backend: loimi
  endpoint_env: LOIMI_MEMORY_URL
  credential_env: LOIMI_MEMORY_TOKEN
  hooks:
    mode: native_required
  guidance:
    required: true

telemetry:
  enabled: true
  collector_url_env: OTEL_EXPORTER_OTLP_ENDPOINT
  headers_env: OTEL_EXPORTER_OTLP_HEADERS
  traces: true
  metrics: true
  diagnostic_logs: true
  include_content: false

memory_diagnostics:
  semantic_analysis: false  # enable separately for sentiment/pattern analysis
  interval_seconds: 86400
  max_records_per_scope: 100
```

Define precedence: explicit deployment override (`MIRAGEN_TELEMETRY_ENABLED`) → explicit profile `telemetry.enabled` → legacy endpoint-presence behavior. An effective explicit false wins over endpoint-presence detection even if endpoint variables exist. With enabled true, validate an endpoint at startup; an unreachable collector is a runtime export failure, not an execution blocker. No telemetry workers or export connections run when disabled. Essential memory revision/provenance journals remain enabled because correctness cannot depend on optional analytics.

Use standard OTLP base-URL semantics for the new collector setting, deriving `/v1/traces`, `/v1/metrics` and `/v1/logs`; standard per-signal URLs override that signal. Preserve `MIRAGEN_OTLP_ENDPOINT` as an exact legacy traces URL, never append another path to it. Legacy-only configuration continues to export traces only; metrics/logs need their own endpoints or a collector base URL. Test custom URL prefixes and conflicting settings explicitly. [OTLP exporter specification](https://opentelemetry.io/docs/specs/otel/protocol/exporter/)

The collector routes telemetry to the deployment's durable metrics/trace/log backends; Grafana can visualize them. The collector itself is not the long-term telemetry database. Give its export queue persistent storage when restart survival is required; there are still finite disk/retry limits and unexported in-process agent spans can be lost on abrupt death. [Collector resiliency](https://opentelemetry.io/docs/collector/resiliency/)

Keep nonblocking bounded queues, signal-specific sampling/limits and explicit drop reporting. Auth headers come from secrets and never enter logs. Export allowlisted metadata by default; any content diagnostics need separately configured access/retention. Correlation IDs belong in traces/logs, not unbounded metric labels. Telemetry loss never corrupts memory or delays tools; essential memory commits and their evidence are not best-effort telemetry.

### 18.4 Memory-system health, sentiment and behavioral drift

Implement two distinct products: **mechanical memory health**, enabled with metrics, and **semantic diagnostics**, separately enabled and budgeted. No single invented “memory health score.” No interpretation of agent prose as a measurement of subjective wellbeing or of the user's psychological state.

| Metric / diagnostic | Mechanism and interpretation |
| --- | --- |
| Write/search/validation duration | Histograms per bounded operation/result label; separate network, embedding and selection time. |
| Admission and rejection | Counts by type/reason; reviewed-fixture false-admission and false-rejection rates, with denominators. |
| Freshness and availability | Pending indexing count, oldest job age, revalidation backlog, active records stale under their configured policy, invalid source references. |
| Retrieval quality | Candidate/selected/token histograms, no-result and fallback rates, source coverage on labeled fixtures, rejected stale/unauthorized candidate counts. A high empty-result rate is not automatically bad. |
| Memory pollution | Duplicate-source rate, one-root concentration in selected packets, unsupported derivation count, orphaned lineage, conflict age, repeated corrections. |
| Temporal integrity | Superseded/retracted revisions actually emitted as current; target zero. Separate successful stale-candidate rejection from bad emission. |
| Workaround debt | Open/overdue remediation count and age, recurrence after mitigation, workaround use after a verified fix exists. |
| Usefulness | Reviewed with/without-memory outcomes and source-level recall; retrieval frequency is not utility or causation. |
| Semantic patterns | Sampled negative/neutral/positive valence toward a **named target**, distrust/avoidance/generalization language, blame attribution and repeated procedural pessimism, each linked to actual source spans. |

For sentiment/pattern analysis, record `target`, label(s), rationale/source spans, annotator model+revision, rubric version, time window, sampled population size, eligible population size and unclassifiable count. Analyze human-authored text, agent-authored text and quoted external material separately. Count distinct roots rather than copied summaries; stratify sampling so one noisy agent cannot dominate. Negative incident reports can be accurate and useful. Avoid optimizing for positivity or automatically deleting negative memories.

Compute label rates over classified eligible samples and publish coverage alongside them; uncertain cases remain unclassified. Compare like scopes/task mixes and track rubric/model changes. A rise in “verification cannot be trusted” warrants inspection of CI failures and evidence, not automatic personality edits. Diagnostics do not feed factual recall or alter authority automatically. If useful, a reviewed diagnostic finding becomes a source-qualified artifact through normal admission.

Persist detailed diagnostic records in a restricted relational `memory_analysis` area (outside the recall projection); export aggregate metrics and restricted reference-bearing logs. Analysts require access to all sampled roots. A fleet dashboard cannot expose private-scope examples to everyone. Worker-wide counts/gauges need a single designated aggregation owner or per-worker attribution; multiple replicas must not each report the same global count as independent events.

Initial dashboard views: **ingestion/freshness**, **retrieval/evidence quality**, **pollution/conflicts/remediation**, **semantic patterns with source drill-down**. This specifies the dashboard contract; no deployment-specific Grafana dashboard is created in this document pass.

### 18.5 Shared scopes: individual, profile, role, group and fleet

Use explicit **scope memberships and links**, not a rigid single-parent inheritance tree. Useful sharing and authorization are separate. One agent can belong to several roles/projects/groups; connected groups share a registered scope only through explicit membership grants. Similar names or overlapping topics never create access rights.

| Scope | Typical contents | Default write rule |
| --- | --- | --- |
| Instance-private | One conversation's private state | That instance's authorized writer. |
| Profile | Durable context useful to future instances of the same profile | Profile-scoped proposals. |
| Role/class | Methods applicable to researcher, operator or tutor roles | Role members propose; configured admission/promotion grants determine acceptance. |
| Group/project | Shared facts, decisions and progress | Explicit group contributors; qualified source authority governs current facts. |
| Shared domain across groups | Common platform/tool/process knowledge | Explicit registered shared scope; group linkage does not grant transitive access. |
| Fleet-wide within a trust domain | Broad conventions and genuinely general procedures | Broad-scope promotion capability, never any ordinary writer by default. |

“All” means all authorized agents within an explicit trust domain, not all users/tenants. Fleet memory is not automatically globally public. Server-authenticated memberships define the maximum readable set; a profile selects a subset, and the current task selects relevant context links within that set.

```yaml
# Illustrative bindings; deployment supplies the actual registered IDs.
memory:
  scopes:
    read: [instance:self, profile:self, role:researcher, group:project_a,
           shared:platform, fleet:default]
    propose: [instance:self, profile:self, group:project_a]
    default_write: profile:self
```

Preserve instance-private attribution and source restrictions; `profile:self` is not permission to expose a private conversation to every profile instance. Promotion creates a separately admitted, source-linked view at the target scope, with an applicability statement and redaction where needed. It never widens the original source ACL. A derived view cannot disclose private roots without explicit permitted declassification/redaction. Source revocation still invalidates affected promoted views.

Retrieval grants each relevant scope lane a bounded opportunity, then deduplicates and applies the same overall token budget. Access does not imply automatic injection. Specific applicable procedure overrides may replace general defaults when explicitly declared. A narrower-scope factual claim does not become truer just because it is local; unresolved evidence conflicts are surfaced. Agents cannot shadow core runtime permissions with profile memory.

### 18.6 Temporary mitigation must lead to a proper fix

**Prefer a verified proper fix even when it costs more or requires work beyond the initiating task.** When practical, do it in the current authorized work. If continuity requires temporary mitigation, label it `temporary_mitigation` and create a durable `RemediationObligation` before treating the incident as handed off. Task completion and root-cause resolution are separate statuses.

Required obligation fields: stable problem key, affected component/scope, supporting evidence, suspected/confirmed cause, mitigation reference and limitations, owner/queue, proper-fix goal, verification criteria, review deadline/trigger and lifecycle. Persist it using the prospective-memory/intention model with a `remediation` payload; no new universal task ontology is necessary. Deduplicate repeat failures against the same open problem.

The obligation survives agent/container/task termination and must be linked to the owner's normal work queue. A periodic bounded sweep resurfaces overdue/unassigned obligations; a failed external queue write stays pending/retryable in the durable outbox. Broader work follows normal permissions and is planned/dispatched through the owning context, not performed secretly under the old task's credentials.

Closing the initiating task does not close remediation. A deadline expiring does not delete the obligation. It is resolved only by verified proper-fix evidence or verified supersession that removes the underlying defect or requirement. Temporary risk acceptance may defer scheduling but leaves remediation open; convenience, retrieval disuse and a successful workaround cannot close it. Once a fix is verified, default recall prefers that fix and marks the mitigation obsolete for applicable versions. Memory must not reward repeated workaround use as a “successful best practice.”

### 18.7 Native hooks and a future Hermes adapter

**Claude Code and Codex native hooks are required integration work in this pass**, alongside the shared runtime boundary. Grok Build and Kimi are next capability-tested targets. A wrapper that merely prepends a startup prompt is not completion of a native-hook requirement.

Current primary-source check (2026-09-13):

| Harness | Evidence and implementation target |
| --- | --- |
| Claude Code | Official reference documents session/prompt/tool/compaction hooks and context output. Map session start/resume, prompt submission, tool outcomes, compaction and stop into the shared lifecycle. Verify the installed SDK/CLI version, especially context-replacement behavior. [Hooks reference](https://code.claude.com/docs/en/hooks) |
| Codex | Official hooks documentation now describes session/prompt/tool/compaction/subagent events and `additionalContext`. It notes context placement in developer context and a non-stable transcript format. Build native registration and test the actual App Server/CLI launch path used by Miragen; do not rely on historical assumptions that Codex lacks hooks. [Hooks reference](https://developers.openai.com/codex/hooks) |
| Kimi | Official docs describe beta lifecycle hooks, stdin JSON and context-bearing output, with fail-open behavior on hook failure. Version-pin and test rather than assuming permission enforcement or Claude parity. [Hooks docs](https://moonshotai.github.io/kimi-cli/en/customization/hooks.html) |
| Grok Build | Miragen already has an executor adapter; this pass did not verify a precise official native-hook contract. Inspect the installed harness/official docs, event and context APIs, then implement against verified capability. Keep status `unverified`, not “unsupported” or complete. |
| Hermes | **Research task, not implementation in this pass:** inspect the official harness for lifecycle hooks, tool/event streams, context assembly/compaction, session persistence, native memory/skills, permissions and licensing. Produce a compatibility matrix and a minimal adapter spike proposal, including how to avoid duplicate memory systems. [Official repository](https://github.com/NousResearch/hermes-agent) |

Implement one bounded local hook bridge (`miragen memory hook`, proposed command) with per-harness serializers. Normalize events as `context.started`, `input.received`, `tool.finished`, `context.compacting`, `context.restored`, `turn.finished`, `context.closed`; preserve the original event name and IDs. Use idempotency keys, bounded synchronous context preparation, asynchronous durable capture and explicit timeouts. Hooks receive credentials from the trusted host environment; payload text cannot specify arbitrary collector/store destinations or principal identity.

Install/version the bridge at launch, merge only its owned hook entries, preserve user hooks and record the capability result. Avoid recursion when memory tool calls themselves trigger capture. Never rely exclusively on a clean session-end hook to save state. Test resume, streaming, compaction, subagent starts, crashes and source invalidation in each required harness.

Separate **trusted usage guidance** from **retrieved evidence**. Where hook output enters a privileged instruction role, do not interpolate arbitrary recalled prose as instructions. Prefer native data/tool-result insertion for memory content; if only additional context is available, send a fixed trusted wrapper plus strictly serialized source-labeled data, and retain independent permission enforcement. Delimiters alone do not establish a security boundary. Track actual inserted packet IDs and context placement. An unsupported required hook/version is an explicit integration failure; do not silently downgrade it to a wrapper.

### 18.8 Every agent gets guidance, not the entire architecture manual

Ship a versioned **memory-use skill** with the runtime and native-harness integration. This is a future implementation artifact, not a newly installed personal skill in this document pass. Supply a compact core guide automatically at every new/resumed/restored context, including child agents; keep detailed examples/tool schemas available via the skill. Do not rely on the model discovering or choosing to load essential rules.

The always-present block states: current work context and effective scopes; permitted memory tools; distinction between observations, proposals, accepted claims and authoritative evidence; current/history modes; correction/retraction route; persistence-degraded status; temporary-mitigation/remediation rule; and the reminder that memory grants no permission. It explains that automatic recall is selective and absence of a packet does not mean the store is empty. Use explicit search when missing prior context matters.

Expose only a small role-appropriate surface: **recall**, **read**, **propose/remember**, **correct**, and **checkpoint**. Runtime hooks handle ordinary event capture automatically. Administrative erasure, cross-scope promotion, diagnostics and librarian maintenance are separate capabilities, not tools shown to every agent. Tool results distinguish `accepted`, `pending`, `conflict`, `rejected` and `persistence_unavailable`; agents must not say “saved” for an unacknowledged write.

The detailed skill includes examples of a changed preference, uncertain scientific claim, superseded procedure, useful personal episode, cross-group shared fact and temporary mitigation with open remediation. Keep examples domain-diverse. Required usage guidance is controlled by runtime release/configuration, never generated from remembered procedures. Include `guidance_version`, schema/API compatibility and effective capability fingerprint in the injection manifest; refresh on configuration change. Test actual model-request presence, not merely that a skill file exists on disk.

### 18.9 Added delivery gates

- Destroy/recreate an agent container and resume via the same stable identity; shared memories persist and instance-private data stays private.
- Telemetry explicit-off produces no exports despite inherited endpoint variables; legacy exact-trace URLs still work; base URLs route all enabled signals correctly.
- Collector outage, backpressure and shutdown leave execution/memory commits intact; export coverage/loss is observable. Commands/errors containing sentinel secrets do not leak into spans/logs.
- Streaming trace identity matches ordinary runs; executor export handles more than 10,000 events without silent truncation or replay double-counting; unknown durations remain unknown.
- Mechanical metrics survive trace sampling; semantic diagnostics show rubric/version, source coverage and unclassified cases. A corpus of legitimate negative incident reports is not automatically treated as defective memory.
- Cross-role/group/fleet recall works with bounded context; unauthorized sharing, transitive grants and private-source promotion fail. Facts do not silently override each other by scope rank.
- Claude Code and Codex pass native lifecycle integration tests; Kimi/Grok report verified capabilities honestly; Hermes research remains tracked.
- Core guidance is present after start/resume/compaction and for child agents; unavailable capabilities are not advertised.
- A workaround leaves a durable owned remediation obligation after task completion; a verified fix supersedes workaround preference and closes the obligation with evidence.
