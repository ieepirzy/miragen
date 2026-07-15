# Executor backend tier

miragen's original backend tier assumes miragen owns the agent loop: a
profile's `spec` names a pydantic-ai model, miragen sends messages and
receives completions. Self-harnessed executors (Codex first) invert this —
they own their own loop, and the contract becomes **workspace-in /
diff-and-events-out**. This is a second backend tier, not another model
backend: a profile declares exactly one of `spec` (model tier) or
`executor` (executor tier).

Design record: miradb #293 (2026-07-12).

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
  mcp_servers:
    - name: loimi
      url: https://loimi.mesh/mcp/
      bearer_token_env: LOIMI_TOKEN   # env var NAME; value injected at spawn
```

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
        ──> failed    (crash)    RESUMABLE — thread + workspace survive
        ──> abandoned            human-terminal; the only place where
                                 keep-for-forensics vs discard is decided
```

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

Install the extra: `pip install miragen[codex]`.

The official SDK is **`openai-codex-sdk`** (Author: OpenAI). Note the trap:
`codex-sdk` on PyPI is an unrelated Cleanlab package. Verified against
0.1.11: the SDK wraps the bundled `codex` CLI (`codex exec
--experimental-json`, `... resume <thread_id>`) — it is spawn-and-parse
under the hood, but with typed Pydantic events and thread resume as an
object handle, which is what this tier needs from it.

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

## Identity & provenance

Per-agent Origo confidential-client identity (decided 2026-07-12): each
executor-backed agent gets its own credentials, minted through origo's
existing registration flow, supplied to the container at spawn as env/secret
and referenced from the profile **by env var name only**. miragen writes the
MCP server list into `CODEX_HOME/config.toml` at startup; token values never
enter profile or config. Loimi provenance then comes free: the executor's
store writes run under its own agent identity via auto-mint.
