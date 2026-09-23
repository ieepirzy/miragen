# Memory effectiveness: from plumbing to agents that remember

Status: **Scoping proposal**, 2026-09-23. **Shipped:** P0 (#111, deployed; VPS
capture failures went from 395/405 to 2/251, and those 2 were dedupe, fixed in
#115) and P3 (#112, deployed). Everything else is unbuilt. P1/P2 revised after
the §9 design round (decisions 3–7); **nothing gets built until Ilari
approves.**
Revised after an agent fact-check against main: P1b already exists (undeployed),
and P2 follows §17.7's mandatory selector.
Owner: Mira (leads testing). Decided by Ilari 2026-09-23: **the VPS bridge
(`10.8.0.4:8420` / `memory.muutto365.fi`) is the single memory store.** Local
miragend is not the driver.

## 0. Implementation status (2026-09-23)

Built, reviewed and green in CI, **not merged or deployed** (merge and deploy wait for Ilari):

| Piece | PR | Notes |
| --- | --- | --- |
| MiraDesign Stop continuation off `stop_hook_active` | miradesign#24 | Prerequisite for P1a |
| P1.0 runner `claude-code:<model>` + worker lease/backoff/backlog cutoff/self-provisioned principal | #117 | |
| P2 sticky project re-resolution | #118 | |
| P2 asynchronous recall + retrieval judgment log | #126 | Stacked on #118 |
| P1a pushy end-of-work nudge | #128 | Stacked on #126 |
| Deploy prerequisites (Claude Code in the image, worker grants) | #129 | |
| Compose: bridge token, worker service | Muutto365/movingfirm-agents#43 | Deploy **only after** the miragen image contains all of the above |

Deferred, with issues: #116 redaction, #119 pinning, #120 async recall for HTTP hooks, #121 learned adapter, #122 `MEMORY.md` import, #123 judgment log into Loimi, #124 Codex/Grok parity, #125 run namespace after a switch, #130 poison-job retry cap, #133 other agents' messages recorded as the user's.

**P1.1 eval done** (full report in the PR #110 comments). The selector is `claude-code:haiku` with the v2 instructions: recall 0.83, false injections 10%, both bars met. Extraction is `claude-code:sonnet`: recall 0.70, precision 0.89. Haiku's extraction recall of 0.26–0.35 fails the bar.

## 1. Why this exists

Ilari suspected memory "isn't doing much, and agents aren't acking it". A
read-only audit on 2026-09-22 confirmed it. The memory an agent actually feels
in a session is Claude Code's own `MEMORY.md`. Miragen contributes a stub of
the previous session's first and last prompt.

| Link | Measured 2026-09-22 |
| --- | --- |
| Capture: hooks → bridge → Loimi events | VPS `/health`: 405 captures, **395 failures** (`409 idempotency_key was already used for different content`) |
| Admission: events → records | Recall on `dir:ilari` and on `miradesign` returns 0 items. The VPS holds 2 records in total, both hand-made on 09-16 |
| Retrieval: records → context | `selector_configured: false`, `prompt_recalls: 0`. The start-of-session lane needs a working-state `goal`, and nothing sets one |
| Use: agent reads and cites | 36 Claude Code sessions in 7 days: 1 made memory calls (2× remember, 0 recall/checkpoint). The memory-bridge skill was loaded 0×. `MEMORY.md` got 40 writes in 11 sessions |

Every link is broken, and each break hides the next. Codex's
`feat/source-grounded-memory` (miragen + Loimi) makes retrieval *precise*: exact
resource grounding and rendering manifests. It assumes records exist and
extraction runs, and on the VPS neither is true today. This doc covers the
path that branch sits on. The two are complementary, and this doc does not
change the grounding contract.

## 2. The four broken links

### 2.1 Capture fails silently
Hook events that fire more than once per prompt (Stop, Subagent*, compaction)
reuse one idempotency key with different content, and Loimi rejects them with a
409. Root cause and fix: §6. Either way,
a 97% failure rate must never again be visible only on `/health`. See §2.4.

### 2.2 The admission path exists; its worker was never deployed
`miragen/memory/extraction.py` skips per-turn `harness:*` events
(`SKIP_SOURCE_PREFIXES`) by design. The session **episode** (`capture_episode`,
`lifecycle.py:355`, filed at session end or compaction from `plane.py` `_finalize`)
*is* eligible (`docs/external-sessions.md`: "`session_episode` events are
eligible, `harness:*` trail is not"). What's missing is the runner:
`miragen memory-worker` and its model are "deployment choices"
(`docs/design/hosted-bridge.md` §5), and no deployment runs them. So today
the only way in that actually runs is the agent voluntarily calling
`memory_remember` / `memory_checkpoint`, and agents don't.

### 2.3 Retrieval is off by construction
- Per-prompt recall needs `recall.model` in `MIRAGEND_SESSIONS_CONFIG`
  (`BRIDGE_RECALL_MODEL` in the agent-stack). It's unset on both daemons, so
  `SessionPlane(selector=None)`. That's by design: §17.7 of the architecture
  pass makes the relevance selector mandatory ("No rank threshold or top-k
  list alone means 'relevant enough'").
- The start lane derives its query from working state `goal`. Nothing writes a
  goal, so it returns `no_query`.
- `memory_recall` works (lexical), but only when an agent thinks to call it.

### 2.4 Silence is ambiguous, so the agent ignores the system
The guide says "an absent memory packet does not mean the store is empty". The
agent therefore cannot tell *nothing matched* from *nothing exists* from
*capture is failing*. There is no count, no health signal and no request to use
or cite anything. `injections` counts prepared contexts, not delivered or used
ones (the grounded branch labels this honestly, but doesn't close it).

## 3. Target: the loop an agent should experience

1. **SessionStart:** a short status line, always, even when empty. Then
   whatever the start lane found for this project.
2. **Each prompt:** recall runs against the prompt. A hit is injected with
   record ids. A miss is stated in one line.
3. **During work:** the agent cites ids it relied on (`[mem:ab12]`) and calls
   `memory_correct` when a memory is wrong.
4. **Stop / PreCompact / SessionEnd:** the session's durable learnings get
   proposed. This happens mechanically, not only when the agent remembers to.
5. **Next session**, same project: step 1 surfaces what step 4 admitted.

The acceptance test is exactly this loop (§5).

## 4. Proposed changes, in delivery order

Each item is one PR with a live check against the VPS, not only a suite.

**P0: capture works.** Fix the idempotency-key defect (§6). Add
`capture_failure_rate` to the status line in P3 so it can never hide again.

**P1.0: model runner. Headless Claude Code on Ilari's subscription** (decision
3, §7). The selector (P2), extractor and checker (P1b) all run through one
`claude -p` invocation. No API key, no per-token bill. The code already has
the seam: `ExtractFn`, `CheckFn` and `SelectFn` are plain async callables, so
the runner is a fourth implementation next to the PydanticAI ones, not a
rewrite. Verified 2026-09-23 on Dakiaim (Claude Code 2.1.280):
```
claude -p <input> --model haiku --output-format json --json-schema <schema> \
  --system-prompt <instructions> --tools "" --setting-sources "" \
  --strict-mcp-config --mcp-config <empty> --no-session-persistence
```
run from an empty working directory with `MIRAGEN_WORKER=1`.
- **Isolation, measured with a control:** the isolated call's debug log shows
  `Registered 0 hooks from 2 plugins`. The same command without
  `--setting-sources ""` shows `Registered 15 hooks from 8 plugins`, with the
  same rollout flag off in both. So the isolation comes from the flag, not
  the rollout state. The isolated call also used only 1,180 input tokens, so
  no CLAUDE.md, auto-memory or tool schemas were loaded, and the model did
  not know it was "Mira".
- **Structured output, measured:** schema-valid in 6/6 toy-schema calls, and
  in one call with the real `ExtractionResult` schema (`$defs`, enums,
  nullable fields) on a realistic episode. That call proposed 3 memories, all
  with exact-substring quotes, and skipped both the joke and the
  hypothetical. It did file a stated preference (squash-merge) as an
  `observation`, not a `claim`. That kind of quality question is what the
  P1.1 eval is for.
- **Latency, measured:** 2.5–2.8 s wall for a trivial call (1.3–1.6 s API),
  3.4–4.0 s with a longer prompt, and **17.9 s** for the real extraction call.
  Extraction is background work, so that's fine there. The selector sits in
  `UserPromptSubmit`, so its latency has to be measured on real selector
  input (P2, open question 7D).
- **Errors never look like "nothing":** the subscription shares its rate
  limit with Ilari's interactive use, so calls will fail sometimes. A
  non-zero exit, `is_error`, or a missing `structured_output` raises. It is
  never read as an empty selection or as "nothing durable". For the
  selector, that means §17.7's degraded path, and the status line says so.
  For extraction, the episode stays unprocessed and is retried through the
  job lease. Otherwise a rate-limited hour would lose its episodes for good.
- **`--bare` is unusable:** it never reads OAuth ("Anthropic auth is strictly
  `ANTHROPIC_API_KEY`"), so it can't use the subscription. Isolation comes
  from the flags above instead.
- **Recursion guard, required:** a worker call must never be captured as a
  harness session, or every memory call spawns a session that spawns an
  episode that spawns extraction. The flags already keep plugin hooks out.
  `miragen_hook` also exits immediately when `MIRAGEN_WORKER=1`, with a test
  for both. That env guard is the **primary** defence, because the flags'
  behaviour belongs to Claude Code and can change between releases. The flag
  test above stays in the runner's live check so a regression shows up.
- **Credential boundary:** the runner shells out to the `claude` binary, and
  only that. The subscription OAuth token is never lifted into PydanticAI or
  raw API calls; that would stop being Claude Code usage. On a headless host
  the token comes from `claude setup-token` (`CLAUDE_CODE_OAUTH_TOKEN`).
- **Where it runs:** open question 7C (§7).
- **§17.2 amendment:** §17.2 says to start on the main model, "rather than an
  unvalidated cheaper substitute". Decision 3 overrides that for Haiku, on
  one condition: the eval in P1.1 passes first. Until it does, the runner can
  be pointed at `--model sonnet` with the same flags.
- **Usage share:** at the measured ~36 sessions/week, that is about 50–150
  selector calls and 5–10 extraction runs a day, each around 2–4k tokens of
  Haiku. P4 reports calls per day, so the share of the Max 5x allowance is
  watched, not guessed.

**P1.1: eval gate for the model.** Before P1b/P2 deploy: 30 real
`session_episode` events from the VPS, labelled for durable items (Mira
labels, Ilari spot-checks 10); 40 seeded records and 40 real prompts labelled
with the ids that should apply. Run Haiku and Sonnet (as the ceiling) 3× each
through the runner. Proposed pass bars: extraction precision ≥ 0.9 and recall
≥ 0.6; selector recall ≥ 0.7 with ≤ 10% false injections. On the
subscription this costs no money, only about half a day.

**P1: admission for harness sessions.** Two paths with different jobs. Both
go through Loimi admission, so nothing mints authority.
- *(a) Pushy agent-side save (the main write path).* Today agents barely use
  miragen, and Claude Code's `MEMORY.md` gets the writes (§1). Ilari wants
  agents nudged hard (decision 2). What agents discover (gotchas,
  environment facts, procedures) exists only in their turns, and the episode
  doesn't carry those (see b). So this path is where that knowledge comes in.
  - *When it fires:* Ilari usually ends sessions with `/clear`, which no hook
    can block. So the nudge can't wait for the end. It fires at the first
    Stop once the session has ≥ 5 prompts, then again after every 15 more
    prompts or after a compaction, at most 3 times per session. It never
    fires in child sessions, and never when `memory_remember` or
    `memory_checkpoint` was already called since the last nudge.
  - *Wording:* pushy and concrete. It names the categories (decisions,
    gotchas, environment facts, Ilari's preferences, open intentions) and
    demands exactly one of two outcomes: `memory_remember` calls, or the
    literal reply `nothing durable`.
  - *Engagement check:* the bridge sees the `memory_*` call for this session,
    or the reply matches. If it's ignored, one firmer re-ask, then log
    `nudge_ignored` (P4).
  - *Guidance, every session:* the SessionStart guide says durable project
    learnings go to `memory_remember`. How that relates to `MEMORY.md` is
    open question 7A.
  - *Coexisting with MiraDesign's Stop hook:* MiraDesign only continues when
    `stop_hook_active` is false (`harness/ingest.py`). A memory block sets
    that flag on the next Stop, which would silence MiraDesign's "someone is
    waiting" continuation. So neither hook decides from the shared flag. Each
    keeps its own per-session "last blocked" state. **Prerequisite:** a
    small MiraDesign PR, split out of miradesign#23, that stops keying its
    continuation on `stop_hook_active` (miradesign#24). Until that lands,
    §5 item 8 fails by construction, so P1a ships after it. **Verified
    live** (Claude Code 2.1.280, 2026-09-23): two blocking Stop hooks run in
    parallel, the model sees **both** reasons in one continuation, and
    `stop_hook_active` is true for both hooks on the next Stop, whichever
    one blocked. §5 still tests both hooks together.
  - *Build:* three pieces that don't exist yet: Stop-block output in
    `miragen_hook` (today it only emits `additionalContext`), per-session
    nudge state in miragend, and bridge-side counting of `memory_*` calls per
    session (`SessionCounters` has none). One PR, no model.
- *(b) Server-side extraction (built, never deployed).* `miragen
  memory-worker` consumes `session_episode` events and already runs the span
  check, the checker and Loimi admission. It gets the P1.0 runner and a
  compose service or unit, plus a principal holding `maintain`.
  - *What it can find:* the episode holds up to 20 prompts cut to 300 chars
    and only the **last** assistant message (`sessions/models.py`,
    `render_episode`). So extraction mostly captures what **Ilari said**
    (decisions, preferences, intentions), which are exactly the records that
    carry authority. Raising the caps is cheap and optional.
  - *Priority:* user-stated beats agent-observed beats inferred.
    "Durable" means a future session in this project would act differently
    knowing it, with the concrete future-use reason §17.5 already requires.
  - *Near-duplicates:* at admission, look up similar records in the scope and
    let the model choose new, same-as-X (add the source) or supersedes-X.
    This reuses the grounded branch's consolidation instead of building a
    second one.
  - *Situation field:* each proposal also carries a short `situation` ("while
    redeploying agent-stack on the VPS"). This is the store side of task
    vectors (P2). It changes `ProposedMemory` (`extra="forbid"`) and the
    record payload. Before building, check it doesn't touch the grounding
    contract this doc promises to leave alone (§1).

**P2: retrieval on, the accepted way.** The §17.7 selector, run through the
P1.0 runner. Lexical top-k alone is still rejected as a relevance signal, so
there's no model-free "interim" recall.
- *Project resolution, tiered (port MiraDesign's rule).* Today miragen binds
  a session's project **once**, at its first event (`plane.py`
  `_attach_project`, only when `session.project is None`). Ilari launches
  most sessions from `~`, so they bind to `dir:ilari` for good, and their
  memories are written there even when the work was in miragen. MiraDesign
  already solved this (`application/presence.py` `heartbeat`): it
  re-resolves on every event from the reported cwd/remote, unless an
  explicit attach pinned the session. Tiers, highest first:
  1. pinned: an explicit attach, or the project MiraDesign bound this
     session to;
  2. the repository of the **current** cwd, re-resolved on every event
     (Claude Code's hook `cwd` follows the agent's `cd`);
  3. no project: profile scope only. That covers `$HOME` itself, detected
     explicitly (it is a git repo, and today it resolves locally to
     `dir:ilari`), plus local cwds that match the existing workspace-root
     heuristic, which today only applies to remote sessions.
  - *Sticky, like MiraDesign:* a binding is only replaced by a *new*
    resolution. `cd ~` after working in miragen keeps miragen; it does not
    fall back to tier 3.
  - *Writes:* the **write** scope follows the resolution at write time, so a
    memory learned inside miragen lands in miragen's scope.
  - *Multi-repo sessions:* a session that touched miragen and miradesign
    still files one episode. It goes to the scope bound at filing time, and
    lists every project the session was bound to in its attributes. The
    extractor then assigns each proposal to one of those scopes, or to
    profile scope when a proposal spans them.
- *Session start, per tier:* tier 1/2 query from project, repo, branch and
  working-state goal, plus the previous session's last prompt. Tier 3 has no
  meaningful query at start: inject only the required lane (profile-scope
  open intentions, pinned records) and let the first prompt drive recall.
- *Each prompt: facets* (≤ 4, §17.7): the prompt itself; the **task
  descriptor**: repo, branch, the claimed MiraDesign work item and the last
  two prompts; the working-state goal when set. Lexical and (later) dense
  channels are fused by RRF, as §17.7 specifies.
- *Small scopes:* while a scope has ≤ 20 eligible records, all of them go to
  the selector and search is skipped. They pass the **same eligibility
  filter** as search: current heads only, nothing superseded, quarantined or
  tentative. Otherwise this would reopen the §17.1 finding. It's consistent
  with §17.7 because the selector still decides relevance; only candidate
  generation is trivial.
- *Asynchronous recall (decision 11).* The prompt hook never waits for the
  selector. `UserPromptSubmit` answers at once with one line in the status
  line: recall for this prompt is running in the background, results arrive
  after a later tool result, don't poll and don't re-run `memory_recall` for
  the same thing. The line is skipped when the scope has zero eligible
  records. The daemon runs search and selection as a background task, keyed
  to that prompt. Delivery:
  - a new `PostToolUse` hook, gated on a local per-session "recall pending"
    marker, so it exits without touching the network when nothing is
    outstanding. When the result is ready, it goes out as `additionalContext`
    on that tool result. **Verified live:** PostToolUse `additionalContext`
    reaches the model, and a subagent's tool calls carry `agent_id` and
    `agent_type` (null on the main thread), so delivery skips subagents. Fetching it is the atomic delivery claim, so
    injections are counted when **delivered**, not when prepared;
  - if the turn ends first, the miragen Stop handler waits a short, bounded
    time and blocks only for a **non-empty** selection. This is the same
    handler, and the same per-session state, as the P1a nudge, so there is
    one miragen Stop block at a time;
  - a result is dropped when a newer prompt arrived, the context compacted,
    or the session ended;
  - HTTP-hook (cloud) sessions have no local marker, so they get no async
    delivery in v1 (#120).
  Selector latency (2.5–18 s through the runner) stops mattering to the
  prompt path. Runner calls share a small concurrency cap, since each one is
  a Node process on a memory-constrained VPS. Trivial prompts ("yes", "merge it", under ~20 chars with no
  new facet) skip the selector call. That decides *whether* recall runs, not
  what's relevant, so it doesn't bend §17.7's rule.
- *Budget:* tighter than §17.7's 2,000-token ceiling: about 800 tokens or
  4 cards per prompt, and never an id already injected in this session.
- *Dense retrieval: deferred.* §17.2 already fixes bge-m3 at 1024 dims.
  Deploying it is movingfirm-agents#29 and waits until scopes outgrow the
  whole-scope path.
- *Task vectors* (Ilari's term; origin miradb #600/#231/#225): a multi-facet
  **situation** representation (domain, activity, entities, outcome, a
  running average of recent turns), so recall finds structurally similar
  situations even when the wording differs. The cosine mismatch between a
  task and a stated fact is handled three ways:
  1. the selector sits after retrieval, so dense search only needs the right
     memory in the top 20 (recall@20), not ranked first;
  2. like-with-like: query situations are compared with stored `situation`
     fields (P1b), not with statements;
  3. later, a small learned **linear adapter** on frozen embeddings, trained
     on the judgment log below.
  Until an embedder exists, the task vector is the structured task
  descriptor above, fed to lexical search and to the selector.
- *Retrieval judgment log (autonomous training data).* Every selector call
  writes one row per candidate, with no human in the loop:
  - the scope, session and a hash of the facets, plus the facet and card
    **text**, so vectors can be computed later, after the embedder ships;
  - each candidate's id and its rank per channel;
  - selected or not, with the selector's reason (teacher labels: a
    non-selected candidate is a hard negative);
  - the model and the embedding-space identity (null for now).
  How much of this is autonomous:
  - The selector's teacher labels are fully autonomous. That is the main
    dataset.
  - The use signal is only partly observable. Hooks carry only the **last**
    assistant message, so a `[mem:…]` citation mid-turn is invisible without
    reading the transcript, which is out of scope (§8). What *can* be seen:
    `memory_read` and `memory_correct` calls, and citations in the last
    message. Anything else counts as **absent**, never as a negative label.
  Requirements:
  - a test that a selector call writes its rows;
  - a `judgments` count on `/health`, so an empty log is visible;
  - §17.8 erasure covers these rows, since they hold prompt text.
  Per §17.6.4, the adapter only reshapes candidate **generation**. It never
  changes authority or truth, and use frequency never feeds back into
  ranking by itself. This gives the dataset the small net needs; "later"
  means once a few hundred positive rows exist.

**P3: announce itself.** Every injection gets one status line, always:
`miragen: 14 memories in group:project.miradesign · 2 match this prompt (below) · capture ok`.
When empty: `0 memories in this project yet`. When failing:
`capture failing (395/405): memories from this session may be lost`.
The guidance lane asks the agent to cite ids it used.

**P4: measure use, not preparation.** Count per session: injected ids, ids the
agent cited, `memory_*` tool calls, corrections. A session-level "memory used"
ratio on `/health` and in telemetry. This is the number that answers "is it
doing anything". Also count: nudges fired, answered and ignored; runner calls
per day and their failures or timeouts; judgment rows written. For the
`MEMORY.md` comparison (decision 4), count per session which system got the
writes and which one the agent actually used.

## 5. Acceptance test (what I will run)

A harness E2E with real `claude -p` children against the VPS bridge, the same
approach as the MiraDesign live QA on 2026-09-18:
1. Session A in a disposable repo learns a non-obvious fact (e.g. "tests need
   `FOO_DB_URL` pointed at port 55433") and ends.
2. Session B in the same repo gets a task that needs the fact. It gets no hint
   in the prompt.
3. Pass = B's context shows the memory with its id, B uses it without
   rediscovering it, and the status line was present in both sessions.
4. Negative controls: an unrelated repo sees no leak, a wrong memory corrected
   in B is not injected to C, and a joke or hypothetical in A is not admitted.

Added with the P1/P2 revision:
5. **Nudge, not instruction:** A is never told to remember anything. The fact
   has to arrive through the P1a nudge. Separately, one of A's prompts states
   a preference, and P1b extraction has to admit it.
6. **Recall without asking:** B's context shows the memory without B calling
   `memory_recall`.
7. **Launched from `~`:** A and B both start in `~` and `cd` into the repo.
   The memory is written to the repo's scope, not `dir:ilari`, and B's recall
   finds it after its `cd`.
8. **Two Stop hooks:** with MiraDesign's plugin active and a directed message
   waiting, a session that also gets the memory nudge still receives the
   MiraDesign continuation, in both orders (nudge first, message first).
9. **Recursion guard:** a runner call creates no bridge session, no episode
   and no capture.
10. **Judgment log:** every selector call in B wrote rows, and `/health`
    `judgments` went up.

Run it after each of P0–P3, so each PR shows what it moved.

## 6. Capture defect (P0)

**Root cause: one call site. The idempotency key treats Claude Code's
`prompt_id` as if it identified one event, but it identifies one prompt.**

- The key is built at `miragen_hook/normalize.py:168-182`:
  `hook:{harness}:{session_id}:{original_event}:{discriminator}`. The
  discriminator is the first present of `tool_use_id`, `prompt_id`, `turn_id`,
  `agent_id`, then a content hash.
- It is used only by `capture_harness_event` (`miragen/memory/lifecycle.py:427-437`).
- Loimi raises the 409 when the same producer and key arrive with a different
  `content_digest` (`src/loimi/memory/service.py:236-242`).
- Claude Code attaches `prompt_id` to **every** hook fired during a prompt. This
  was live-probed and is pinned in `tests/test_hook_adapter.py:82,95`.
- Any event that fires more than once per prompt therefore reuses its key with
  new content:
  - **Stop** fires again whenever the model resumes without a new user prompt:
    blocking Stop hooks (**including MiraDesign's continuation**),
    background-task and agent notifications, and `/loop` / ScheduleWakeup
    wakeups. The result is 409, and the capture is lost.
  - **SubagentStart/SubagentStop**: `prompt_id` outranks `agent_id`, so every
    subagent in a prompt shares one key. Different content gives a 409.
    Identical content is deduped silently, and the event is lost with no error.
  - **PreCompact/PostCompact** repeated within one prompt behave the same way.
- UserPromptSubmit (once per prompt) and PostToolUseFailure (`tool_use_id`) are
  safe.
- VPS evidence: `agent-stack-miragend-bridge-1` (`ghcr.io/ieepirzy/miragend:latest`)
  logs `POST /memory/v1/events 409` → `memory degraded: hook capture: memory API 409`
  roughly every 32 s during long agentic sessions. Only hook events fail.
  Episodes and artifacts don't.

**Minimal fix (`normalize.py` only):**
- Keep the bare id for events that are unique per id: UserPromptSubmit →
  `prompt_id`, tool failures → `tool_use_id`.
- For every other event, key on `{id}:{sha256(captured_content + attributes)[:16]}`,
  with `agent_id` leading for Subagent* events.
- A redelivered hook still dedupes (same content, same key), and distinct Stops
  stop colliding.
- Test: two Stops that share a `prompt_id` with different messages must both
  be captured, with no 409.
- It doesn't overlap `feat/source-grounded-memory`, which leaves `normalize.py`
  alone.

**Consequence for P1a:** a Stop nudge adds exactly the repeated-Stop traffic
that triggers this bug, so P1a must land after P0.

## 7. Decisions

Decided by Ilari, 2026-09-23:
1. **Model: the cheapest one that can do the job.** One structured-output model
   serves the selector (P2) and the memory worker (P1b). Which model it is goes
   to the design session (§9), chosen by measured quality and cost, not
   reputation.
2. **The end-of-session save prompt (P1a) is pushy.** The goal is to get the
   agent to actually engage, not to be polite. Design and wording are in §9.
   Order constraint from §6: it adds repeated Stop events, so it lands after
   P0, which is now live.

Decided by Ilari, 2026-09-23 (design round, answering §9):
3. **Runner: headless Claude Code on the Max 5x subscription, Haiku.** No API
   costs. Codex's subscription is the fallback, but it's usually spent on
   side projects. Mechanics and measurements: P1.0. This overrides §17.2's
   "main model first", once the P1.1 eval passes. It also removes the
   data-residency question a third-party API (DeepSeek) would have raised:
   prompts go only to Anthropic, where these sessions already run.
4. **miragen, not `MEMORY.md`, is the intended memory.** Ilari prefers
   miragen's record store over Claude Code's wiki-like file memory, so agents
   get nudged hard to write to it (P1a). If P4 shows miragen performing worse,
   switching gets reconsidered.
5. **"Task vectors" = the situation representation** from miradb #600: facets
   plus like-with-like situation matching, and later a small adapter. The
   dataset for that adapter must build itself (P2 judgment log).
6. **Session-start resolution is tiered**, using the rule MiraDesign already
   has (P2).
7. **Stop-hook coexistence with MiraDesign is proven by a test** (§5 item 8),
   not argued.

Decided by Ilari, 2026-09-23 (answers to A–D below):
8. **A:** Claude Code auto-memory stays **on** for now (the baseline).
9. **B:** "no redaction" still holds; deferred to #116.
10. **C:** the runner lives on the **VPS**. Ilari accepts that blast radius:
    the credential only spends his subscription's usage limits. This holds on
    the condition that the `setup-token` credential gives no billing or
    account control; see P1.0.
11. **D:** recall is **asynchronous**: the agent is told it's running and
    gets the result later, like an async API call (P2).

Kept for the record:
A. **`MEMORY.md` coexistence during the trial.** Claude Code's system prompt
   tells agents to write file memory, and that competes with the nudge.
   Options: (a) leave auto-memory on as the baseline and let P4 compare;
   (b) turn off Claude Code auto-memory while the §5 trial runs, and route
   everything to miragen; (c) a one-time import of the ~60 `MEMORY.md` entries
   into `profile:mira` and project scopes. Recommendation: (a) for the first
   week to get a baseline, then (b) with (c).
B. **Prompt capture + secrets.** Distilling session episodes (P1b) mines
   verbatim prompts. The 2026-09-16 decision was "no redaction". Confirm it
   still holds once content gets *promoted*, not just stored.
C. **Where the runner lives.** Either on the VPS next to the bridge (it needs
   the `claude` CLI in the image and a `setup-token` credential there; that
   puts a personal subscription credential on the company VPS), or on
   Dakiaim, where Claude Code is already logged in. Dakiaim is off at times:
   extraction can queue, but per-prompt selection from cloud sessions would
   degrade while it's off. Recommendation: the VPS, if Ilari accepts the
   credential there.
D. **Selector latency.** Is +2.5–4 s per non-trivial prompt acceptable?
   Answered by decision 11: recall doesn't block the prompt at all.

## 9. Handoff: design questions for a fresh session

**Status 2026-09-23: answered.** The answers are in P1.0–P2 and decisions 3–7
(§7). The model question was decided by the runner choice (decision 3), and
quality is gated by the P1.1 eval. The questions stay below for the record.

Ilari wants these answered in a dedicated design session. They decide what
P1 and P2 actually become. **Read first:** this doc, then
`docs/miragen-memory-agent-architecture-pass.md` §17 (accepted design; §17.5
extraction, §17.7 retrieval) and §18. That's where earlier answers live, and
proposals must not quietly contradict them (this doc's first draft did, see
PR #110 comments).

**Facts established 2026-09-22/23 (verified, not assumed):**
- Loimi already supports **hybrid search**: lexical OR-query plus
  `query_embedding` in a locked 1024-dim projection space (pgvector,
  migrations 0004/0005/0010; `service.py` search). **No embedding server is
  deployed**, so production recall is lexical-only. Tracked in
  Muutto365/movingfirm-agents#29 (embed server + Loimi worker) and
  miragen#109 (memory worker: extraction, projection embeddings, predicate
  registry).
- The write path exists but never runs: `miragen memory-worker` extracts from
  `session_episode` events (span check + checker + Loimi admission; may
  propose zero). It skips per-turn `harness:*` events by design. It isn't
  deployed and has no model.
- The selector (§17.7) is mandatory for automatic recall. Nothing injects
  until `BRIDGE_RECALL_MODEL` is set (agent-stack env, declared in compose).
  movingfirm-agents#28 tracks it.
- Episodes are built from what hooks carry (prompts, last assistant messages,
  failures, children). There's no transcript reading.
- The status line (P3) now tells agents the recall mode on every open, so the
  effect of any change is visible per session.

**Questions to answer, each with a recommendation and its cost:**
1. **Cheapest adequate model** for (a) the selector and (b) extraction. Both
   need structured output. Candidates must be measured on a small fixed eval:
   real `session_episode` events from the VPS store as extraction input, and
   real prompts against seeded records for the selector. Report
   precision/recall and cost per 1k sessions. Is one model enough for both?
2. **Write: based on what?** Is the session episode the right unit? Should
   agent-authored `memory_remember` calls outweigh extraction? What makes
   something "durable" (decisions, gotchas, environment facts, user
   preferences) versus noise? How is the stream of near-duplicates
   deduplicated or consolidated (the grounded branch has consolidation)?
3. **Recall: based on what?** The query source at session open (repo, branch,
   first prompt, working-state goal) and per prompt. How lexical and dense
   combine (§17.7 already specifies RRF over facets). What the selector sees.
   The injection budget.
4. **Semantic/vector similarity for automatic recall.** It's designed and the
   store supports it; what's missing is an embedding model and a deployment.
   Which embedding model fits 1024 dims cheaply (or local on the VPS)? Is it
   worth it before the selector exists? Per §17.7, similarity alone never
   decides relevance.
5. **"Task vectors".** Ilari's term. Clarify with him first: (a) embedding the
   *current task* (prompt + goal + repo) as the recall query, which is §17.7
   facets; or (b) task vectors in the model-editing sense, which don't fit a
   retrieval store; or (c) clustering memories by task type. Recommend one.
6. **Pushy end-of-session save (P1a, decided pushy).** Mechanism: a Stop hook
   that blocks once after real work, like the proven MiraDesign Stop
   continuation. Needs: a threshold for "real work", wording that makes the
   agent actually call `memory_remember`/`memory_checkpoint` (or say
   "nothing durable"), how to verify it engaged (tool call observed before the
   next Stop), and what happens when it ignores the prompt (block once more?).
   It must not fight MiraDesign's Stop hook. Two blocking Stop hooks in one
   harness need an order.

**Deliverable:** a revision of this doc's P1/P2 sections with the answers, a
model choice Ilari can approve with a cost estimate, and the §5 acceptance
test adjusted to prove the loop end to end.

## 8. Out of scope

Grounding precision (Codex's branch), multi-tenant bridges, and transcript
reading beyond what hooks carry.
