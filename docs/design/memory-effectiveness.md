# Memory effectiveness: from plumbing to agents that remember

Status: **Scoping proposal**, 2026-09-23. Nothing here is built.
Owner: Mira (leads testing). Decided by Ilari 2026-09-23: **the VPS bridge
(`10.8.0.4:8420` / `memory.muutto365.fi`) is the single memory store.** Local
miragend is not the driver.

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

### 2.2 Nothing admits harness sessions into memory, by design
`miragen/memory/extraction.py` skips `SKIP_SOURCE_PREFIXES = ("harness:",)`.
Coding-session turns count as an "operational trail", not statements to mine.
The extraction worker (`miragen extract`) and its model are also "deployment
choices" that were never deployed (`docs/design/hosted-bridge.md` §5). So for
Claude Code and Codex, the **only** way in is the agent voluntarily calling
`memory_remember` / `memory_checkpoint`, and agents don't.

### 2.3 Retrieval is off by construction
- Per-prompt recall needs `recall.model` in `MIRAGEND_SESSIONS_CONFIG`. It's
  unset on both daemons, so `SessionPlane(selector=None)`.
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

**P1: admission for harness sessions.** Two independent paths. Both go through
Loimi admission, so nothing mints authority.
- *(a) Agent-side nudge.* A Stop hook blocks **once** per session, after a
  session that did real work (tool calls and file edits over a threshold). It
  asks: "Anything durable learned? `memory_remember` it, or reply 'nothing'."
  This is the proven pattern: miradesign's Stop continuation blocks exactly
  once and agents answer (verified live 2026-09-18). Cheap, and needs no model
  on the server.
- *(b) Server-side distillation.* Let the extraction worker process
  **`session_episode`** events only, one per session end or compaction, still
  skipping per-turn `harness:` events. The existing span check + checker +
  admission apply unchanged. Needs the worker deployed on the VPS with a model.

**P2: retrieval on.**
- Deterministic first: per-prompt lexical recall (Loimi's existing lexical
  search), top-k, with a score floor. No model and no cost, and it fails
  visibly.
- Selector as an optional reranker once there's enough data for it to matter.
- Start lane: query from project scope + repo + branch + first prompt when
  there is no `goal`, instead of `no_query`.

**P3: announce itself.** Every injection gets one status line, always:
`miragen: 14 memories in group:project.miradesign · 2 match this prompt (below) · capture ok`.
When empty: `0 memories in this project yet`. When failing:
`capture failing (395/405): memories from this session may be lost`.
The guidance lane asks the agent to cite ids it used.

**P4: measure use, not preparation.** Count per session: injected ids, ids the
agent cited, `memory_*` tool calls, corrections. A session-level "memory used"
ratio on `/health` and in telemetry. This is the number that answers "is it
doing anything".

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

## 7. Open decisions for Ilari

1. **Distillation model on the VPS (P1b, P2 rerank).** Which model and what
   budget. P1a + P2-lexical need none, so this can wait.
2. **Stop nudge intrusiveness (P1a).** Once per session above a work
   threshold, or opt-in per project?
3. **`MEMORY.md` coexistence.** Claude Code's file memory is where the real
   knowledge lives today (~60 entries). Options: (a) leave it alone and let
   miragen earn its place; (b) a one-time import into `profile:mira` / project
   scopes; (c) make it a generated view of miragen. Recommendation: (a) until
   §5 passes, then (b).
4. **Prompt capture + secrets.** Distilling session episodes (P1b) mines
   verbatim prompts. The 2026-09-16 decision was "no redaction". Confirm it
   still holds once content gets *promoted*, not just stored.

## 8. Out of scope

Grounding precision (Codex's branch), multi-tenant bridges, and transcript
reading beyond what hooks carry.
