# Plan: Executor tier refinement — closing the #26 gaps

**Status:** proposed — plan for review, no code yet
**Scope:** miragen executor tier (`miragen/executor.py`, `miragen/app.py`, `miragen/models.py`)
**Baseline:** the gap audit on issue #26 (2026-07-18) against `main` after PR #29/#31

Issue #26 specified the executor tier; PR #29 shipped a narrower, in places
deliberately different, design. The audit on the issue leaves five genuinely
open items. This plan closes them — reconciled against what actually shipped
rather than the original issue sketch, because the shipped semantics
(resume/abandon state machine, baseline-tag diff harvest) are better than the
issue's (`cancel()`, `git diff HEAD`) and are already load-bearing.

## Scope decisions carried in from review

- **Lomitus stays excluded.** Confirmed on the issue as a deliberate 07-12
  call, not a regression. Coordination of parallel executor runs remains a
  layer above this tier. Recorded here as a non-goal.
- **Loimi comes in, but not as a primary defined channel.** The issue's
  acceptance criterion ("Loimi `store_document` called on success") hard-wires
  one destination into the success path. Instead, miragen grows a generic
  **artifact sink** capability — "put things that were produced somewhere" —
  and Loimi is the first sink implementation. The sink is optional profile
  config, never a required step, and its failure never changes a run's status.
- **`cancel()` is superseded, not missing.** The shipped resume/abandon state
  machine (`POST /runs/{id}/resume`, `/abandon`) covers the issue's `cancel()`
  intent with better semantics (abandon is the only human-terminal state).
  The ABC below codifies the shipped contract; it does not add `cancel()`.
- **Baseline-tag diff harvest stays.** `git diff --cached --binary
  miragen-baseline` is correct where the issue's `git diff HEAD` would drop
  the executor's own mid-turn commits. The harvest moves into shared base
  code unchanged.

## The five gaps, and how each closes

### 1. Abstract `ExecutorBackend` interface

`miragen/executor.py` becomes a package:

```
miragen/executor/__init__.py   # re-exports CodexExecutor + base types (import-stable)
miragen/executor/base.py       # ExecutorBackend ABC, ExecutorResult, shared workspace/diff/events code
miragen/executor/codex.py      # CodexExecutor (moved, unchanged semantics)
miragen/executor/claude_code.py  # gap 3
miragen/executor/spawn.py        # gap 4
```

The ABC codifies the **shipped** contract:

```python
class ExecutorBackend(ABC):
    def prepare(self) -> None: ...                      # startup config, idempotent
    @abstractmethod
    async def run_job(self, prompt, run_id, *, thread_id, workspace,
                      first_turn, prior_usage) -> ExecutorResult: ...
    def read_events(self, run_id, limit=200) -> list[dict]: ...
```

Backend-agnostic pieces move to `base.py`: `_prepare_workspace` (git init +
baseline tag), `_harvest_diff`, the events.jsonl writer/reader, and the
budget check. Adapters own only: how to start/resume their agent, how to map
their native event stream into event payloads, and how to report usage.

`ExecutorSpec.executor` widens to `Literal["codex", "claude-code", "spawn"]`
and app startup dispatches through a small factory instead of constructing
`CodexExecutor` directly. Codex-specific spec fields that don't apply to a
backend are rejected at profile validation (same loud-rejection philosophy as
the existing model-tier/executor-tier field split).

### 2. Independent wall-clock timeout

Today the only guard is the token budget — a spend limit, not a clock. Add:

- `ExecutorSpec.turn_timeout_s: int | None` (default e.g. 1800; `None` = no limit).
- `_run_executor_turn` wraps `backend.run_job()` in `asyncio.wait_for`, so the
  timeout is enforced by miragen independent of any adapter's cooperation.
- On timeout the run lands in the shipped state machine as **`suspended`,
  `exit_reason="timeout"`** — resumable, workspace + thread survive, no
  harvest — rather than the issue's terminal `timeout` status. A wedged
  executor is exactly the case where resume-or-abandon is the right human
  decision.
- Adapters must tolerate cancellation: on `CancelledError`, terminate the
  underlying CLI/SDK subprocess and flush the events file before re-raising.
  This is part of the ABC's documented contract and gets a test per adapter.

### 3. `ClaudeCodeAdapter`

`miragen/executor/claude_code.py`, built on the Claude Agent SDK (Python),
as an optional dependency extra (`miragen[claude-code]`), mirroring how the
Codex SDK is only needed for codex profiles.

Mapping onto the shipped contract:

| Contract piece | Claude Code realization |
|---|---|
| workspace | SDK `cwd` option pointed at the run's workspace |
| thread_id (resume) | SDK session id; stored on the run record like a Codex thread id |
| MCP injection | `spec.mcp_servers` passed as SDK MCP server config at session init |
| approval_policy | permission mode (`bypassPermissions` for the unattended `never` default) |
| events.jsonl | SDK message stream mapped to the same payload shapes (`thread.started`, `turn.completed`, `item.completed`, `turn.failed`) |
| usage | taken from the SDK result message's usage block |
| auth | `ANTHROPIC_API_KEY` / mounted OAuth credentials — documented in README next to the Codex `auth.json` section |

Jobs stay atomic: nothing persists between distinct runs; the per-run session
is the resume state, exactly as the Codex thread is. Tests use an injectable
client factory, the same seam `thread_factory` already provides for Codex.

### 4. `SpawnAdapter`

`miragen/executor/spawn.py` — the no-SDK fallback: spawn a configured argv
template (`spec.command`, with `{workspace}`/`{prompt}` placeholders) as a
subprocess in the workspace, stream stdout/stderr lines into events.jsonl as
raw `item` events, and reuse the shared baseline-tag harvest on exit 0.

Deliberately minimal: no resume (`thread_id` stays `None`; resume returns the
existing "no executor thread handle" 400), no usage reporting, timeout via
gap 2's wrapper killing the process group. This is the cheapest gap and the
least urgent — it ships last and is cuttable if priorities shift.

### 5. Artifact sink (generic), Loimi as first sink

New optional profile config:

```yaml
executor:
  artifact_sink:
    kind: loimi
    url: https://loimi.mesh/mcp/
    bearer_token_env: LOIMI_TOKEN   # env var NAME, injected at spawn
```

Behavior, wired into `_run_executor_turn` after a **succeeded** finish:

- A small `ArtifactSink` protocol: `async def store(run, diff_path) -> None`.
- `LoimiSink` calls Loimi's `store_document` tool over its MCP endpoint with
  `kind="executor_diff"`, the diff content, and run metadata (run_id,
  agent name, thread_id). Provenance rides on Loimi's existing `open_run`
  auto-mint — no extra plumbing in miragen.
- **Non-fatal by construction:** sink errors are logged and recorded on the
  run record (`artifact_stored: bool`), but never change run status and never
  raise into the caller. The diff on disk remains the source of truth.
- No sink configured (the default) = exactly today's behavior. This keeps
  Loimi a destination, not a defined channel; other sinks (filesystem copy,
  webhook POST) can implement the same protocol later if ever wanted.

## Non-goals

- Lomitus locks / any cross-run coordination (deliberately out, see above).
- `cancel()` on the ABC (superseded by resume/abandon).
- Job queue changes — runs keep arriving through the existing run/record flow.
- Making the artifact sink a required or blocking step anywhere.

## Sequencing

1. **Refactor + timeout** — package split, ABC extraction, factory dispatch,
   `turn_timeout_s`. Pure restructure plus one feature; no behavior change
   for existing codex profiles beyond the new timeout default. Existing
   `test_executor.py` must pass with only import-path updates.
2. **ClaudeCodeAdapter + artifact sink** — the two items with new user-facing
   capability. Includes README auth docs and a `compose.example.yml` note for
   Claude credentials, mirroring #31's Codex treatment.
3. **SpawnAdapter** — last, cuttable.
4. **Issue #26 hygiene** — on completion, comment mapping each original
   acceptance criterion to done / superseded-by-design (cancel(),
   `git diff HEAD`, Lomitus, Loimi-as-required-step) and close.

Each phase is a separate PR against `main`; phase 1 is mechanical enough to
review quickly and unblocks the other two.
