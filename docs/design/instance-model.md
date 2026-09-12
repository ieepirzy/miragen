# ADR: Instance model — one container per profile, N instances within

Status: accepted (2026-09-12)

## Context

"One agent, one container" conflated two things. The container is the right
unit for **environment, permissions and definitions**: `agent.yaml`,
`tools.py`, credential env scoped by executor kind, labels, resource limits,
the pinned runtime image — all provisioned per-container by `LifecycleCore`.
But it is the wrong unit for **an agent that exists more than once**: running
50 copies of one profile today means 50 containers, and `MIRAGEND_MAX_AGENTS`
(default 10) caps exactly the wrong thing.

The conflation lives almost entirely in the app tier, not the daemon:

- `app.py` keys conversation state to the container: `HISTORY_FILE =
  /agent/history.json` is a singleton, so the model tier has exactly one
  implicit instance — and two concurrent `use_history` runs race on it.
- There is no in-container admission control: nothing bounds concurrent
  `/run/async` calls except the daily token budget.

Meanwhile the executor tier already behaves instance-ish per run: each run
gets its own workspace (`workspace_root/<run_id>`) and resumable thread.

## Decision

A container hosts one **profile**. Within it, an **instance** is a named
state scope an arbitrary number of which may exist, bounded by resources —
not by container count.

1. **Identity.** `instance` is a string matching the agent-name grammar
   (`[a-z0-9][a-z0-9_-]{0,62}`). Every run belongs to exactly one instance;
   the default is `default`. `RunRecord` gains an `instance` field.

2. **API.** `instance` is an optional body field on `POST /run` and
   `POST /run/async` (bare calls keep today's behaviour on the `default`
   instance — fully backward compatible). `GET /instances` lists known
   instances (union of history files and run records) with last-run info.
   `DELETE /instances/{id}` discards an instance's conversation state.
   New contract capability string: `instances/v1`.

3. **Model-tier state.** Per-instance history at
   `/agent/histories/<instance>.json` (+ per-instance sidecar). Migration:
   an existing `/agent/history.json` is adopted as the `default` instance's
   history on first boot.

4. **Executor-tier state.** `instance` is recorded on the run and used for
   serialization/listing. Conversation continuity remains thread-based
   (run → resume); an instance groups those runs. No new thread semantics.

5. **Concurrency & admission.**
   - Per-instance mutex: at most one running turn per instance. This is
     what makes per-instance history writes safe by construction.
   - Per-container semaphore: `limits.max_concurrent_runs` (profile field,
     env override `MIRAGEN_MAX_CONCURRENT`, default 4). Overflow → HTTP 429
     with `Retry-After`; queueing is deliberately out of scope for v1.
   - `limits.tokens_per_day` stays container-wide: all instances of a
     profile share the profile's budget.

6. **Triggers.** Scheduled runs (profile triggers and managed schedules)
   are stateless today and stay that way by default — a fire runs as an
   ephemeral anonymous instance. A trigger (and a managed binding) may opt
   into persistent state by naming an `instance:`.

7. **Daemon.** Nearly untouched. `MIRAGEND_MAX_AGENTS` keeps its
   implementation and becomes honest: it caps *profiles*, not copies of an
   agent. Per-profile container resource sizing (a profile hosting 30
   instances wants more than `1.0 cpu / 512m`) is a swarm-layer
   `resources:` field — deferred to a follow-up; the env-global knobs
   suffice until then.

## Consequences

- N copies of one agent = 1 container, N instances. Container count scales
  with the number of *kinds* of agents, as intended.
- The `use_history` concurrent-write race disappears (per-instance mutex).
- Clients that never send `instance` see no change.
- The memory system (separate design doc, forthcoming) gets its natural
  boundary: per-instance conversation history vs per-profile durable
  memory shared across instances. This ADR deliberately decides only the
  instance boundary, not memory.
