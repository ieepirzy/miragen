# Design: Run Records & Approval Bridge

**Status:** draft for review
**Scope:** miragen (HTTP API + runtime) and miragen-mcp (MCP tools)
**Depends on:** the strict-schema work on `claude/friendly-wozniak-vuchr4`

Two features with one theme: give external callers — above all miragen-mcp and the
LLM clients behind it — visibility and control over agent execution. Today the only
window into a running agent is `docker logs`, and the only control point after a run
starts is killing the container.

- **Part 1 — Run records:** every agent run becomes a persistent, queryable record
  with status, usage, and a tool-call trace, plus a non-blocking way to start runs.
- **Part 2 — Approval bridge:** approval requests can be parked in a queue and
  resolved over HTTP, so miragen-mcp (and therefore Claude, or a human driving
  Claude) becomes an approval channel with zero custom webhook code.

They ship together because the approval queue's UX depends on run visibility: a
paused run is indistinguishable from a hung one unless run records exist.

---

## Part 1 — Run records & async runs

### Goals

- Persist a record per run: prompt, output/error, timing, token usage, tool calls.
- Let callers start a run without holding an HTTP connection open (`/run/async`).
- Let callers list and inspect runs (`GET /runs`, `GET /runs/{id}`).
- Zero behaviour change for existing callers of `POST /run`.

### Non-goals

- Conversation history / RAG (already on the roadmap; run records are execution
  telemetry, not conversational memory — though they share the workspace-dir
  storage pattern).
- Cross-agent aggregation (miragen-mcp composes that by fanning out).
- A database. Files in the agent workspace are enough at this scale and keep the
  one-container-one-agent model self-contained.

### Data model

New Pydantic models in `miragen/models.py` (wire models — lenient, like the
approval models):

```python
class ToolCallRecord(BaseModel):
    tool_name: str
    args: str            # JSON-encoded, truncated to 2_000 chars
    ok: bool             # False if the call raised / was denied

class RunUsage(BaseModel):
    requests: int
    input_tokens: int | None = None
    output_tokens: int | None = None

class RunRecord(BaseModel):
    run_id: str                       # uuid4 hex
    agent_name: str
    trigger: Literal["cron", "http", "http_async"]
    status: Literal["running", "succeeded", "failed", "interrupted"]
    prompt: str                       # truncated to 20_000 chars
    output: str | None = None         # truncated to 100_000 chars
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_s: float | None = None
    usage: RunUsage | None = None
    tool_calls: list[ToolCallRecord] = []
    use_history: bool = False

class RunSummary(BaseModel):
    # Everything in RunRecord except prompt/output/tool_calls, plus:
    prompt_preview: str               # first 200 chars
    output_preview: str | None        # first 200 chars
```

`usage` comes from `result.usage()` on the PydanticAI `AgentRunResult`;
`tool_calls` is extracted by walking `result.all_messages()` for
`ToolCallPart`s and pairing them with their return/retry parts.

### Storage: `RunStore` (new module `miragen/runs.py`)

- Directory: `/agent/runs/` (workspace-relative, so it survives container
  restarts and is readable through `miragen_read_agent_file` even before the
  new MCP tools exist).
- One JSON file per run: `<started_at:%Y%m%dT%H%M%S>Z-<run_id[:8]>.json`.
  Lexicographic filename order == chronological order; listing needs no index.
- Writes are atomic: write to `<name>.tmp`, then `os.replace`.
- A `running` record is written when the run starts and replaced on completion,
  so a crash mid-run leaves evidence.
- **Startup sweep:** in the app lifespan, any record still `running` is
  rewritten as `interrupted` (the container restarted mid-run).
- **Retention:** after each write, prune to the newest `MIRAGEN_RUN_RETENTION`
  records (env var, default 200). Env rather than profile field: it's an
  operational knob, not agent behaviour.

API of the class (all sync file I/O — records are small, calls are rare):

```python
class RunStore:
    def __init__(self, root: Path = Path("/agent/runs"), retention: int = 200): ...
    def start(self, *, agent_name, trigger, prompt, use_history) -> RunRecord
    def finish(self, record, *, status, output=None, error=None, usage=None, tool_calls=()) -> RunRecord
    def get(self, run_id: str) -> RunRecord | None      # accepts full id or unique prefix
    def list(self, *, limit=20, status=None) -> list[RunSummary]   # newest first
    def sweep_interrupted(self) -> int
```

### HTTP API changes (`miragen/app.py`)

| Endpoint | Change |
|---|---|
| `POST /run` | Unchanged semantics. `RunResponse` gains `run_id: str \| None` (additive — existing clients that read only `output` are unaffected). |
| `POST /run/async` | **New.** Same body as `/run`. Returns `202 {"run_id": ..., "status": "running"}` immediately; the run continues in an `asyncio.create_task`. Task exceptions are caught and land in the record as `status="failed"`. |
| `GET /runs` | **New.** Query params `limit` (default 20, max 100) and `status`. Returns `{"count": N, "runs": [RunSummary, ...]}`, newest first. |
| `GET /runs/{run_id}` | **New.** Full `RunRecord`; 404 with the list of recent ids if unknown. Accepts unique id prefixes so LLM callers can use the short id shown in summaries. |
| `GET /health` | Gains `last_run` (RunSummary or null) and `pending_approvals` (int, Part 2) — cheap liveness+context in one probe. |

Wiring: `run_agent()` in `app.py` is refactored to take an optional
`RunRecord` and do start/finish bookkeeping; `run_agent_cron`, `/run`,
`/run/async`, and `/run/stream` all pass one. For `/run/stream`, the record is
finished when the stream completes (output = accumulated text).

Concurrency note: runs are already unserialized today (two `POST /run`s
overlap); records don't change that, they just make it observable. A
`max_concurrent_runs` profile field is out of scope but becomes trivial once
records exist.

### MCP tools (miragen-mcp `server.py`)

| Tool | Behaviour |
|---|---|
| `miragen_run_agent_async(agent, prompt)` | POST `/run/async`, return the run_id with a hint to poll `miragen_get_run`. Annotations: open-world. |
| `miragen_get_run(agent, run_id)` | GET `/runs/{id}`, render the record (output truncated at the server's 50k cap). Read-only. |
| `miragen_list_runs(agent, limit=20, status=None)` | GET `/runs`, return `{count, runs}`. Read-only. |
| `miragen_run_agent` | Unchanged, but append `(run_id: …)` to the returned output when present. |

Graceful degradation: a 404/405 from an agent on the old image maps to
`ERROR: agent '<name>' is running a miragen version without run records — recreate it
on image >= X.Y (miragen_delete_agent + miragen_create_agent).`

---

## Part 2 — Approval bridge

### Current behaviour (for context)

`approval_required` globs route matching tool calls through
`_run_approval_gate` (`miragen/approval.py`), which blocks inside PydanticAI's
`tool_execute` hook awaiting a registered handler or `approval_webhook`. If
neither is configured it logs a warning and **auto-approves (fail open)**.

### Design: in-process approval broker + HTTP resolution

New module `miragen/broker.py`:

```python
class PendingApproval(BaseModel):
    request: ApprovalRequest
    created_at: datetime
    expires_at: datetime

class ApprovalBroker:
    def submit(self, request, timeout_s) -> asyncio.Future[ApprovalResponse]
    def resolve(self, request_id, response) -> bool     # False if unknown/expired
    def pending(self) -> list[PendingApproval]
```

In-memory only, by design: the waiting side is an `asyncio.Future` inside a
live agent run — neither can survive a container restart, so persisting the
queue would only create zombies. A restart aborts the run, which the startup
sweep from Part 1 records as `interrupted`.

### Profile schema additions (`miragen/models.py`)

```yaml
approval_mode: open        # open | strict | queue   (default: open)
approval_timeout_s: 300    # queue mode: how long a request may wait
```

`approval_mode` governs what happens when a gated tool call fires and **no
registered handler or webhook** is configured:

| mode | behaviour |
|---|---|
| `open` | auto-approve with a warning — **today's behaviour, stays the default** (no breaking change) |
| `strict` | deny with `ModelRetry("approval gate '<glob>' is unconfigured — denied (approval_mode: strict)")` |
| `queue` | park in the broker; wait up to `approval_timeout_s`; timeout ⇒ deny with an explanatory ModelRetry |

Precedence overall: registered handler > `approval_webhook` > `approval_mode`.
A validator rejects `approval_mode`/`approval_timeout_s` when
`approval_required` is unset (dead config = probable typo, consistent with the
strict-schema philosophy).

### HTTP endpoints (`miragen/app.py`)

| Endpoint | Behaviour |
|---|---|
| `GET /approvals` | `{"count": N, "approvals": [PendingApproval, ...]}` — `tool_args` included so the approver can actually judge the call. |
| `POST /approvals/{request_id}` | Body = `ApprovalResponse` (`{"approved": bool, "prompt": str \| null}`). Resolves the future; the paused run continues (approved, with the note folded in — existing gate behaviour) or receives a denial ModelRetry. `404 {"error": "unknown, already resolved, or expired", "pending": [ids]}` otherwise. |

The denial/approval note mechanics (`prompt` folded back into context) reuse
`_run_approval_gate`'s existing code paths unchanged — the broker is just a
third source of `ApprovalResponse`.

### MCP tools (miragen-mcp)

| Tool | Behaviour |
|---|---|
| `miragen_list_pending_approvals(agent)` | GET `/approvals`. Read-only. |
| `miragen_resolve_approval(agent, request_id, approved, note=None)` | POST `/approvals/{id}`. `note` maps to `ApprovalResponse.prompt`. **Not** read-only; destructive=false; description must tell the client to show `tool_args` to the human before approving. |

Phase 2 (optional): `miragen_list_all_pending_approvals()` fanning out over
every workspace agent, skipping unreachable containers.

### Security / trust model

`/approvals` has the same exposure as `/run` today: reachable by anything on
`miragen-net`, fronted externally only by the OAuth-protected MCP server. That
means **any peer agent could approve another agent's gated calls** — but any
peer can already *prompt* any agent, so this doesn't widen the existing trust
boundary. Hardening for both, worth doing but separable: an optional shared
secret (`MIRAGEN_INTERNAL_TOKEN` env var; when set, `/run*` and `/approvals*`
require an `X-Miragen-Token` header; miragen-mcp injects it from its own env).

Prompt-injection note for the MCP layer: `tool_args` in a pending approval is
attacker-influenceable content (the agent chose them, possibly under
injection). The `miragen_resolve_approval` description must instruct clients
to treat it as data to display, never as instructions — and auto-approval by
an unattended LLM client defeats the purpose of the gate; the intended flow is
Claude *surfacing* the request to the human.

---

## Sequencing & compatibility

1. **miragen first** (one minor release): `RunStore`, broker, endpoints, schema
   fields, startup sweep. All changes additive; `approval_mode` defaults to
   today's behaviour; `RunResponse.run_id` is additive.
2. **miragen-mcp second:** the five new tools + graceful-degradation errors for
   agents on older images. No ordering hazard: old server + new agent image is
   fine (extra endpoints unused), new server + old image degrades with a clear
   error.
3. Bump the base image and note in both READMEs that run records/approvals
   require image >= the miragen release.

## Testing

- `RunStore`: unit tests with `tmp_path` — atomic write, prefix lookup, retention
  pruning, `sweep_interrupted`.
- Broker: `asyncio` tests — submit/resolve, timeout ⇒ denial, resolve-after-expiry
  returns False, `pending()` ordering.
- Endpoints: extend `tests/test_app.py` with the existing stub-agent pattern
  (`TestClient`): `/run/async` returns 202 then record reaches terminal state;
  `/runs` pagination; `/approvals` resolve unblocks a gated run end-to-end
  (gate + broker + endpoint in one test).
- Modes: `strict` denies, `open` warns and approves, `queue` + timeout denies —
  extend `tests/test_approval.py`.
- miragen-mcp: mock `httpx` per existing conventions; verify degradation errors.

## Open questions

1. Should `/run/stream` also return the run_id (header `X-Miragen-Run-Id`) so
   streaming clients can correlate? (Proposed: yes, it's free.)
2. Is 200-record retention the right default for chatty cron agents (every 5
   min ⇒ ~17 h of history)? Env-tunable either way.
3. Does the roadmap's conversation-history work want to reference run_ids in
   `history.json` entries? Cheap to add now (`run_id` on the record is stable),
   painful to retrofit.
