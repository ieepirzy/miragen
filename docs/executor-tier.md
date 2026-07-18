# Executor backend tier

miragen's original backend tier assumes miragen owns the agent loop: a
profile's `spec` names a pydantic-ai model, miragen sends messages and
receives completions. Self-harnessed executors invert this — they own their
own loop, and the contract becomes **workspace-in / diff-and-events-out**.
This is a second backend tier, not another model backend: a profile declares
exactly one of `spec` (model tier) or `executor` (executor tier).

The contract is an ABC (`miragen.executor.base.ExecutorBackend`): the base
class owns everything backend-agnostic — workspace baseline, events.jsonl
persistence, payload parsing, budget check, baseline-tag diff harvest —
and adapters implement `_stream_turn()`, an async generator of normalized
event payloads. Three adapters ship:

| `executor.executor` | Backed by | Resume | Usage reporting |
|---|---|---|---|
| `codex` | openai-codex-sdk (`miragen[codex]`) | thread id | yes |
| `claude-code` | claude-agent-sdk (`miragen[claude-code]`) | session id | yes |
| `spawn` | argv template, no SDK | none | none |

Design record: miradb #293 (2026-07-12); refinement plan:
[design/executor-tier-refinement.md](design/executor-tier-refinement.md).

## Profile

```yaml
name: codex-worker
mode: interactive
triggers:
  - type: http
limits:
  tokens_per_run: 500000     # exceeded -> run suspends (resumable), no harvest
executor:
  executor: codex
  instructions: |
    You operate on the repository mounted in your workspace.
  model: null                # executor default when omitted
  sandbox_mode: workspace-write
  approval_policy: never     # unattended default — see gotchas
  network_access: false
  web_search: false
  reasoning_effort: null
  workspace_root: /agent/workspaces
  codex_home: /agent/codex-home
  turn_timeout_s: 1800         # wall-clock cap per turn, enforced by miragen
                               # itself; expiry -> suspended (timeout), resumable.
                               # null disables. The token budget is a spend
                               # limit, not a clock; this is the clock.
  mcp_servers:
    - name: loimi
      url: https://loimi.mesh/mcp/
      bearer_token_env: LOIMI_TOKEN   # env var NAME; value injected at spawn
  artifact_sink:               # optional: ALSO push harvested diffs somewhere.
    kind: loimi                # store_document with kind=executor_diff.
    url: https://loimi.mesh/mcp/
    bearer_token_env: LOIMI_TOKEN
```

A `claude-code` profile is identical minus `codex_home` (auth is
`ANTHROPIC_API_KEY` or mounted `~/.claude` credentials instead) — the same
`mcp_servers` list is injected per-session, and `approval_policy` maps onto
permission modes:

```yaml
executor:
  executor: claude-code
  instructions: |
    You operate on the repository mounted in your workspace.
  approval_policy: never       # -> bypassPermissions
  turn_timeout_s: 1800
  mcp_servers:
    - name: loimi
      url: https://loimi.mesh/mcp/
      bearer_token_env: LOIMI_TOKEN
```

The spawn backend swaps `mcp_servers` (rejected — a spawned CLI reads its own
config) for a `command` argv template: `{workspace}` and `{prompt}` are
substituted per element, and when no element contains `{prompt}` the prompt
arrives on stdin:

```yaml
executor:
  executor: spawn
  instructions: "One-shot batch job."
  command: ["my-agent-cli", "--repo", "{workspace}", "--task", "{prompt}"]
  turn_timeout_s: 600
```

The artifact sink is **not a primary channel**: it never gates success, and a
sink failure only marks `artifact_stored: false` on the run record while the
run stays `succeeded` — the diff on disk is the source of truth.

Model-tier-only fields (`tools`, `approval_required`, `approval_webhook`,
`history_max_messages`) are rejected on executor profiles: the executor's
tools are its own, approvals are `executor.approval_policy`, and the
executor thread is the history.

## Job semantics

One agent-run = one executor thread. The `thread_id` and `workspace` live on
the **run record** — job DAG/lineage is a concern of the layer above this
tier. Resume re-opens the thread bound to the run and reuses its workspace.

State machine:

```
running ──> succeeded            diff harvested, exactly once
        ──> suspended (budget)   RESUMABLE — partial diff is resume state
        ──> suspended (timeout)  RESUMABLE — turn_timeout_s expired; the turn
                                 was cancelled, workspace + thread survive
        ──> failed    (crash)    RESUMABLE — thread + workspace survive
        ──> abandoned            human-terminal; the only place where
                                 keep-for-forensics vs discard is decided
```

The timeout is enforced in the app tier (`asyncio` cancellation around the
whole turn), independent of any backend's cooperation; adapters must let the
cancellation propagate and release their subprocess/session on the way out.
The resume handle is recovered from the persisted event stream, since the
cancelled turn never returned one.

Tool-level failures inside the executor's loop are the agent's problem and
are not modeled by miragen. Budget exhaustion (`limits.tokens_per_run`,
checked after each turn) suspends; crashes and API errors fail; both keep
the thread handle and workspace so `POST /runs/{id}/resume` can continue.

**Workspace lifetime ≠ container lifetime.** Mount a persistent volume at
`workspace_root`; the container is disposable (suspend idle, resume on
activity), the workspace is keyed to the agent-run.

## HTTP surface (additions)

| Endpoint | Purpose |
|---|---|
| `POST /run`, `POST /run/async` | Same as model tier; dispatches to the executor. |
| `POST /runs/{id}/resume` `{prompt}` | Re-open the thread of a suspended/failed run. |
| `POST /runs/{id}/abandon?discard_workspace=` | Human-terminal; optionally discard the workspace. |
| `GET /runs/{id}/diff` | The harvested diff (404 until terminal success). |
| `GET /runs/{id}/events` | Executor event stream: turn events, item/tool events, errors. |

`POST /run/stream` returns 400 for executor profiles — poll `/events`.

## SDK integration

**Codex** — install the extra: `pip install miragen[codex]`. The official
SDK is **`openai-codex-sdk`** (Author: OpenAI). Note the trap: `codex-sdk`
on PyPI is an unrelated Cleanlab package. Verified against 0.1.11: the SDK
wraps the bundled `codex` CLI (`codex exec --experimental-json`, `... resume
<thread_id>`) — it is spawn-and-parse under the hood, but with typed
Pydantic events and thread resume as an object handle, which is what this
tier needs from it.

**Claude Code** — install the extra: `pip install miragen[claude-code]`
(`claude-agent-sdk`, the official Anthropic package). The SDK session id
plays the thread_id role; `approval_policy` maps onto permission modes
(`never` → `bypassPermissions` — same unattended-stall gotcha as Codex);
`mcp_servers` are injected per-session rather than written to a config file.
Jobs are atomic: nothing persists between distinct runs.

**Spawn** — no extra needed; the fallback for CLIs with no SDK. stdout
lines become raw `item.completed` events and the whole stdout doubles as the
run output. Exit 0 harvests, non-zero is a resumable `failed` — but with no
thread handle, resume in practice means abandon-and-rerun.

## Operational gotchas (unattended runs)

1. **`approval_policy` must be effective** — an executor waiting on
   interactive approval stalls the job forever. The profile default is
   `never`; the container plus `sandbox_mode` are the guard.
2. **`auth.json` must be volume-mounted into `codex_home`** — ephemeral
   containers fail auth on spawn otherwise. miragen warns at startup when
   neither auth.json nor `CODEX_API_KEY` is present.
3. **The pip wheel does NOT bundle the codex binary.** `Codex()` raises at
   construction if the CLI is absent. Install the codex CLI in the container
   image (or run `openai_codex_sdk.install.install_codex()` at build time).
4. **Claude Code auth is env or mount, never image-baked** — set
   `ANTHROPIC_API_KEY` in the container environment (the `*_FILE` secrets
   loader works here too) or mount Claude Code OAuth credentials at
   `~/.claude`. miragen warns at startup when neither is visible.
5. **A cancelled turn must not leak its process** — `turn_timeout_s` kills
   the turn via asyncio cancellation; the spawn adapter kills its subprocess
   on the way out, the SDK adapters rely on their SDK's cleanup.

## Identity & provenance

Per-agent Origo confidential-client identity (decided 2026-07-12): each
executor-backed agent gets its own credentials, minted through origo's
existing registration flow, supplied to the container at spawn as env/secret
and referenced from the profile **by env var name only**. miragen writes the
MCP server list into `CODEX_HOME/config.toml` at startup; token values never
enter profile or config. Loimi provenance then comes free: the executor's
store writes run under its own agent identity via auto-mint.
