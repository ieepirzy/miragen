# External sessions: miragend as the local memory-participation daemon

**Status:** implemented (`miragen/daemon/sessions/`, `miragen_hook/`, `docs/external-sessions.md`)
**Depends on:** the memory lifecycle (§17.3, `miragen/memory/lifecycle.py`), the
native hook bridge (§18.7, memory pass PR 2b), Loimi `/memory/v1`.

## 1. The invariant

**Loimi stores memory. `miragend` manages participation in the memory
system. MiraGen agents, Claude Code, Codex and future harnesses are clients
of that capability.**

Before this record, a harness session could only participate in memory in
one of two ways: as a MiraGen-owned agent (the app process owns the
lifecycle around each run) or through the PR 2b hook bridge, where every
hook invocation is a fresh `miragen memory-hook` process that loads an
agent profile and talks to Loimi directly. The second path works, but it
has no session: nothing remembers that a Claude Code process exists between
two hook invocations, nothing can finalize a session whose `SessionEnd`
never fires, and every hook pays a ~1 s `pydantic-ai` import.

## 2. What was found in the code (and what changed the design)

- **Loimi over the network already exists and is the only transport.**
  `MemoryClient` speaks `/memory/v1` over HTTP with a minted principal
  token (`LOIMI_MEMORY_URL` / `LOIMI_MEMORY_TOKEN`); the deployed stack
  publishes it on `127.0.0.1:8400` and on `miragen-net`. Nothing new is
  introduced — the daemon holds one principal token and reuses the client.
- **The memory pipeline begins at `MemoryLifecycle.prepare_context` and
  ends at Loimi's admission.** Working state by identity (required lane),
  the zero-or-more recall selector (optional lane), manifests, idempotent
  capture, checkpoint/remember/correct, and the extraction worker are all
  harness-agnostic already. The daemon composes them; it does not fork
  them. Two small additions were needed: prompt-time recall without
  re-injecting guidance (`recall_section`) and an extraction-eligible
  episode capture (`capture_episode`, source kind `session_episode` —
  the worker deliberately skips `harness:*` operational trail).
- **miragend's registry invariant stays intact.** The lifecycle plane's
  rule is "agents cannot announce themselves into the registry" because
  a compromised container must not forge membership. External sessions
  are a *separate* registry with a different trust model: they are
  self-announced by definition, admitted only from the daemon's own
  bearer-guarded loopback API, and they can never choose a scope, a
  principal or a credential — identity and scopes are resolved by the
  daemon from its own configuration and from the session's working
  directory. A session payload is data about a session, never authority.
- **The hook serializers were right in shape but drifted in field names.**
  Live-probed against Claude Code 2.1.270 on 2026-09-15: `SessionStart`
  carries `source` (not `reason`), `UserPromptSubmit` carries `prompt` +
  `prompt_id`, `SessionEnd` carries `reason`, `PostToolUseFailure` carries
  `error`. `additionalContext` is honored on both `SessionStart` and
  `UserPromptSubmit`. The normalizer now accepts every documented
  spelling; the daemon never sees a raw payload.
- **`miragend` cannot require Docker for this.** The lifecycle plane is
  Docker-bound by design; the session plane is not. `create_app` now
  accepts `core=None` and `main()` honors `MIRAGEND_LIFECYCLE=off`, so
  the same daemon runs as a plain user service on a developer machine.

## 3. Shape

```text
claude (hooks) ──miragen-hook claude-code──┐
codex  (hooks) ──miragen-hook codex────────┤  stdlib-only adapters:
future harness ──miragen-hook <kind>───────┤  serialize in, shape out,
                                           │  fail open, ≤10 s
                                           ▼
                                 miragend  POST /sessions/v1/events
                                   │ SessionRegistry   (persisted, pid-checked)
                                   │ ProjectResolver   (cwd → repo identity → scopes)
                                   │ MemoryLifecycle   (one per project scope set)
                                   │ EventJournal      (replayed on restart)
                                   ▼
                                 Loimi /memory/v1 (network, principal token)
```

- **Adapter (`miragen_hook`)** — harness-specific, stdlib only, no
  dependency on the `miragen` package. Reads one hook payload on stdin,
  normalizes it to the design-doc vocabulary (`context.started`,
  `context.restored`, `input.received`, `tool.finished`,
  `context.compacting`, `context.compacted`, `turn.finished`,
  `context.child_started`, `context.child_finished`, `context.closed`),
  posts one envelope, and if the daemon answers with context, wraps it in
  the harness's output shape. It never blocks the harness: bounded
  timeouts (context events 10 s, captures 2 s, session end 1 s), exit 0
  always, nothing on stdout unless there is context to inject.
- **Session plane (`miragen/daemon/sessions`)** — harness-agnostic.
  `ExternalSession` is the generic record (key, harness, session id, pid,
  cwd, project identity, parent session, agent, children, lifecycle
  state, counters, timestamps). The daemon upserts it on *every* event,
  so a restarted daemon relearns live sessions from the next hook.
- **Scope policy** — one Loimi `group` scope per project, derived from
  the repository's remote (or its path when there is none), plus
  configured shared read scopes; explicit per-project bindings override
  the template. Auto-provisioning of a new project scope uses the
  operator credential *if* the daemon is given one; otherwise the
  project scope is assumed provisioned and Loimi's refusal degrades the
  packet explicitly. A session in repository A never reads repository
  B's scope unless a binding says so.
- **Out-of-band writes** — prompts, turn outcomes, tool failures, child
  completions and compactions are captured idempotently as harness
  events (operational trail). At compaction and at session end the
  daemon writes a deterministic **session episode** (prompts, last
  outcome, counters — an extraction-eligible source event) and
  checkpoints `last_session` into the project's working state. No
  model call is made for these; extraction into claims is the existing
  worker's job.
- **Resilience** — every capture envelope is journaled before it is
  processed; the journal is replayed on daemon start and Loimi's
  idempotency keys make the replay harmless. A sweeper finalizes
  sessions whose process is gone or that went silent, so a killed
  harness still gets its episode.

## 4. Deliberately not done

- No transcript scraping: the episode is built from what the hooks
  carry. Reading `transcript_path` at `PreCompact` is a documented
  follow-up, gated on the harnesses declaring the format stable.
- No model-authored summaries in the daemon. The one model call in the
  path is the existing recall selector, and only when configured.
- No generalized recursion policy. Parent/child is recorded where the
  harness exposes it (`agent_id`/`agent_type` on subagent hooks;
  `MIRAGEN_PARENT_SESSION` in the environment for spawned harnesses).
- No plugin framework: a new harness is one serializer in
  `miragen_hook/normalize.py` and one install function.
