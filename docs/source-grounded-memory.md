# Source-grounded memory and resource retrieval

Loimi owns records, revisions, source events, evidence receipts, grounding
baselines, scope enforcement and consolidation transactions. Miragen owns Python
inspection, retrieval selection, rendering, maintenance orchestration and the
existing extraction worker. Source indexes are disposable observations, never a
second memory store. No Markdown memory store, Git synchronization, procedural
graph evolution or model confidence admission is introduced.

## Runtime and tool surface

`memory_for_resources(resources, project, query="", inspect=False)` is available
on the **miragend bridge MCP**. `project` must be an observed **local session key**
(e.g. `claude-code:...` from the session header). `resources` is a bounded list of
explicit `{ "path": "pricing.py", "symbol": "Price.total" }` locators; omitting
`symbol` selects the entire Python file. Symbol names must come from source
inspection. The tool never invents them from natural language.

Miragen resolves the local checkout, inspects its source and asks Loimi for exact
resource joins in the lifecycle's configured read scopes. Optional ordinary
recall for `query` uses the existing selector when configured. Both paths feed
one renderer, with resource matches first, deduplication by record, canonical
revision checks, complete payloads, assertion/validity/slot qualifications and
source-verification limitations. The resource view is refreshed after selection
and Loimi checks canonical state in a consistent database snapshot.

`inspect=True` returns explicitly labelled **INSPECTION ONLY** historical or
unverified evidence. It cannot be treated as current operational guidance. A
resource miss differs from an excluded record: `rendering.omitted` reports IDs
and machine reasons such as `not_current_applicable`, `canonical_ineligible`,
`duplicate_record`, and `budget_exceeded`. Database candidate/result truncation
is reported separately by the lookup API. Explicit `memory_read` and existing
revision reads retain complete records.

The same assembly path is available to hosted callers through
`MemoryLifecycle.prepare_context(resources=...)`, with explicit observations.
Ordinary text retrieval stays available when no resource identity is known.
Grounded records require source inspection; text similarity cannot establish
checkout applicability. A revised grounded record remains withheld until its
new revision has its own grounding, even if the revision request omits
`requires_grounding`.

## Rendering and delivery accounting

`render_optional_section()` returns `RenderResult`: exact text, emitted
record/revision pairs, omitted pairs with reasons, character budget, used and
remaining characters, and truncation. The text budget includes the header and
newlines. It is the optional **context text** budget; machine accounting and the
required guidance/working-state lane are separate. Entire qualified records are
omitted when they do not fit. There is no 500-character record slicing. The
selector sees an explicit unavailable-card marker for oversized payloads,
rather than a prefix with a potentially missing negation.

Boundary and prompt-time manifests are made exclusively from emitted entries.
Their policy has `stage: rendered`, `delivery_status: unconfirmed`, and full
rendering accounting. Preparation is not proof of delivery. A manifest can exist
when a subsequent timeout, transport failure or closed harness drops the
response. Existing `injections` counters retain their wire name but measure
prepared contexts; the status API explicitly labels this measurement. This pass
does not invent harness delivery acknowledgements. Old manifests are not
backfilled: their exact historical emission cannot be reconstructed from the
previous accounting alone.

## Grounding contract

An exact resource identity consists of kind (`python_symbol` or `python_file`),
canonical repository identity, repository-relative path and qualified symbol.
Line numbers and content digests are not identity components. Content evidence
is SHA-256 of exact source bytes, including decorators and comments **inside**
the symbol. Qualified nested declarations are indexed with standard-library
AST. Duplicate declarations remain ambiguous; moves and renames are misses.
There is no inferred call graph or approximate rebinding.

A baseline stores the source event, memory revision, original source revision,
branch, checkout identity and dirty-worktree digest, plus an existing Loimi
assertion-support receipt. Checkout identity includes host/root/Git directory.
Inspection brackets file reads with checkout snapshots and rereads the source;
symlink traversal, nonregular files, unavailable Git, parse failures and races
produce unverified results. Inspection is bounded to 20 resources, 512 kB per
file and 8 MB of Git output/dirty content per snapshot. It never executes project
code, Git external diff helpers or text converters.

Four facts stay distinct:

1. The original source observation and its historical memory revision.
2. The exact resource identity.
3. Source content currency (`unchanged`, `revalidation_required`, `missing`,
   `ambiguous`, `unverified`).
4. Assertion support (`supports`, `contradicts`, `inconclusive`) with verifier,
   property, observation time and limitations.

Lookup applicability is relative to the supplied source observation; its timestamp
is included. Loimi cannot independently inspect a caller's filesystem. Miragen
performs a fresh local inspection for the supported tool/CLI workflow.

A content digest match proves only unchanged source. The support receipt must
check `source_supports_assertion` for the particular memory revision, resource
key and digest. Receipts are attributed claims by an authorized verifier, not an
omniscient semantic oracle. Miragen does not derive a support verdict from a
hash or model confidence. Grounding writes require a corresponding source event
already in the record's lineage; the baseline must equal that event's observation.

Baselines are immutable. `check` explicitly appends a source-currency receipt;
`inspect`, `recall`, and rebuilding source observations never write or renew a
baseline. A changed branch, commit, checkout or dirty digest requires revalidation
even if the symbol bytes match. Thus a line shift preserves identity, but a dirty
checkout change still conservatively changes applicability. To establish support
for a new checkout state, capture new evidence, use the existing record revision
API with `expected_seq`, and attach a baseline/support receipt to that new
revision. Old evidence remains readable. A renamed resource needs an explicit
new grounding; it cannot become verified by a rename heuristic.

## Explicit maintenance workflow

The CLI uses `LOIMI_MEMORY_URL` and a registered principal's
`LOIMI_MEMORY_TOKEN`. It calls the same APIs as the lifecycle; it needs no database
credentials. Run it in the checkout that actually holds the source:

```sh
python -m miragen.memory.source_cli inspect --scope group:demo --checkout /path/to/repo --path pricing.py --symbol price
python -m miragen.memory.source_cli ground --scope group:demo --checkout /path/to/repo --path pricing.py --symbol price --input grounding.json
python -m miragen.memory.source_cli recall --scope group:demo --checkout /path/to/repo --path pricing.py --symbol price
python -m miragen.memory.source_cli check --scope group:demo --checkout /path/to/repo --path pricing.py --symbol price
python -m miragen.memory.source_cli history --scope group:demo --checkout /path/to/repo --path pricing.py --symbol price
```

`grounding.json` contains a complete `payload`, optional `type` (observation,
procedure or intention), and `support: {result, method, limitations}`. The support
verdict describes an actual performed check; use `inconclusive` otherwise. The
workflow preserves source text in a source event and uses `requires_grounding`
to withhold the new record until attachment succeeds. These are separate API
transactions: a failed later step leaves reviewable evidence and a withheld
record, not a falsely applicable record. Structured claims use Loimi's existing
predicate/record API.

`check` checks at most 20 exact groundings per invocation, records each outcome,
and reports omitted historical revisions or limits. `maintain` is required for
check writes. Reading an absent or ambiguous resource is not verification.

## Bounded overlapping-memory consolidation

`POST /memory/v1/consolidations/queue` accepts up to 20 explicit candidate
record/revision pairs, one scope and an idempotency key. The existing
`miragen memory-worker` worker consumes `consolidate_overlap` jobs alongside
extraction jobs. Its default policy proposes only exact complete duplicates of
the same source roots, with matching types, assertion and temporal metadata.
It can return no changes. It does not infer equivalence from similarity.

`POST /memory/v1/consolidations` (also CLI `apply --input proposal.json`) accepts
`keep_separate`, `merge`, or `supersede` proposals over only the named candidates.
Loimi locks records in stable order, checks expected revision IDs, requires
`propose`, `maintain` and `resolve`, validates referenced evidence and commits the entire
batch or nothing. Merges require equal complete payloads, observation times,
assertion/validity metadata and resource/verification qualifications. Supersession
requires a `consolidation_supersession` support receipt naming both exact
revision IDs. Structured claim slots continue through the existing temporal
resolution API. Different validity intervals or grounding qualifications stay
separate. No added human approval gate or confidence threshold exists.

An immutable representation link excludes the duplicate/outdated representation
from current recall while retaining its original revisions, observations,
conflicts, source roots and evidence. Explicit reads expose the relation and
source/target record IDs. Repeated representations of one event never create a
new evidence root. A merged representative still depends on every original's
roots and expected revision. Correction/root invalidation withholds it; erasure
reaches duplicate representations and their retrieval projections. Chained or
already-consolidated candidates are refused in this bounded first pass. A stale
worker job fails without an endless retry; queue a new candidate set after
rereading canonical revisions.

## Reproducible example and verification

Against a **disposable Loimi service** with migration 0011 and an operator token:

```sh
LOIMI_DEMO_OPERATOR_TOKEN=... python examples/grounded_memory_demo.py --url http://127.0.0.1:18400
```

The example creates a synthetic scope/principal and a temporary Git repository.
It checks a fixture's `return 10` AST node, stores a qualified observation, and
recalls it. It changes the body to `return 20`, then asserts that current recall
emits zero items, an explicit check says `revalidation_required`, and historical
inspection still shows the original observation and baseline. It prints no
authentication tokens. Its synthetic data remains in the disposable store; the
source checkout is temporary.

Focused tests are `tests/test_memory_sources.py` and existing recall/session
suites in Miragen, plus Loimi's `test_memory_grounding.py`,
`test_memory_consolidation.py`, and `test_memory_miragen_integration.py` (the last
runs when Miragen is installed alongside Loimi). The integration test uses real
Postgres, Loimi's authenticated API and Miragen's source/recall/check/worker code.

## Rollout and limitations

Loimi automatically applies **0011_memory_grounding** during normal startup,
before accepting requests. No manual migration step is needed. When rolling
out these changes, update Loimi before Miragen. There are no new dependencies
or daemon configuration keys. Existing principals keep
their capabilities: grounding creation needs `read`/`propose`, checks need
`read`/`propose`/`maintain`, consolidation needs
`read`/`propose`/`maintain`/`resolve`. A backend
without these extensions, including the disposable ephemeral test backend,
reports an unsupported/refused resource operation; it is not a second durable
implementation.

External hooks currently provide SessionStart/context-restored and prompt-time
context opportunities. They do not provide a verified path/symbol stream for
resource recall, and successful tool calls are not generally captured. This
pass therefore uses the explicit bridge tool for observed local sessions and
the CLI/API where the source actually lives. It does not inject guessed source
identities, claim a hosted daemon can inspect a remote workspace, or claim
external Claude/Codex delivery has been live-tested.

Python syntax inspection does not prove behavior involving imports, globals,
configuration, dynamic dispatch or dependencies outside the selected unit.
Checkout matching is intentionally conservative. There is no general graph,
implicit revalidation, automatic rename matching, or autonomous procedure
learning. Character accounting is exact for the rendered optional text; it is
not a provider-specific token estimate.

## Engineering references

Inspected MEX at `66c3047e7b94b71d0f9b270a040808af53cba488`:
[grounding resolver](https://github.com/mex-memory/mex/blob/66c3047e7b94b71d0f9b270a040808af53cba488/src/wiki/grounding/resolve.ts),
[symbol identity](https://github.com/mex-memory/mex/blob/66c3047e7b94b71d0f9b270a040808af53cba488/src/graph/extraction/node-id.ts),
[resource joins](https://github.com/mex-memory/mex/blob/66c3047e7b94b71d0f9b270a040808af53cba488/src/wiki/query/for-code.ts),
[read consistency](https://github.com/mex-memory/mex/blob/66c3047e7b94b71d0f9b270a040808af53cba488/src/graph/read-session.ts),
[budget accounting](https://github.com/mex-memory/mex/blob/66c3047e7b94b71d0f9b270a040808af53cba488/src/wiki/query/budget.ts),
[constrained consolidation](https://github.com/mex-memory/mex/blob/66c3047e7b94b71d0f9b270a040808af53cba488/src/wiki/synthesis/global-pass.ts).
The useful mechanisms are immutable reviewed baselines, line-independent
identity, exact reverse joins, consistent observations, explicit truncation and
bounded proposals. No MEX source was copied; its Markdown authority, approximate
rebinding and confidence-based promotion were not adopted. No performance claim
from MEX is asserted for these systems.


## Verification record for this implementation

The implementation was verified locally in isolated worktrees, without merging,
pushing or deploying. Loimi's full suite passed 218 tests against a disposable
Postgres/pgvector 16 container. Miragen's affected memory, protocol, hook, session
and bridge suites passed 224 tests. The combined test installs both repositories
in one fresh Python 3.12 environment. Source inspection was also run against
`MemoryLifecycle._optional_lane` in Miragen and `_validated_card` in Loimi.

The HTTP example ran through a separate loopback Uvicorn Loimi service with a
read/propose/maintain principal, without retraction permission. It emitted one
memory before the edit, zero after, recorded `revalidation_required`, and retained
the historical baseline. The partial-manifest regression was proved to fail when
the original over-accounting bug was deliberately reintroduced, then the fixed
code was restored. Selected lint checks and `git diff --check` passed.

The Miragen suite reports an existing Starlette deprecation and unawaited
`MemoryClient.admin_grant` warnings on session-provisioning failure tests. No live
external Claude/Codex invocation, production data mutation, merge, remote CI run
or deployment was performed. The disposable verification services were removed.
