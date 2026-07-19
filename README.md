# miragen

[![PyPI version](https://img.shields.io/pypi/v/miragen)](https://pypi.org/project/miragen/)
[![Python versions](https://img.shields.io/pypi/pyversions/miragen)](https://pypi.org/project/miragen/)
[![License](https://img.shields.io/github/license/ieepirzy/miragen)](https://github.com/ieepirzy/miragen/blob/main/LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/ieepirzy/miragen/test.yml?label=tests)](https://github.com/ieepirzy/miragen/actions)

**YAML-defined agent orchestration framework powered by [PydanticAI](https://ai.pydantic.dev).**

Define agents as YAML profiles. Run them as isolated Docker containers. Wire them together into a swarm.

```yaml
name: morning-briefing
mode: autonomous
triggers:
  - type: cron
    schedule: "0 9 * * 1-5"
    default_prompt: |
      Produce the morning briefing.
      Include: weather, calendar events, top headlines.
spec:
  model: anthropic:claude-sonnet-4-6
  instructions: |
    You are a concise morning briefing agent.
    Always end with a one-line summary of the day ahead.
  capabilities:
    - WebSearch
    - Thinking:
        effort: low
```

```bash
miragen run
```

That's it.

---

## Installation

```bash
pip install miragen
```

Requires Python 3.12+.

---

## Concepts

### Agent profiles

Each agent is defined by a YAML profile with two layers:

- **Swarm layer** — miragen-owned fields: name, mode, triggers, tools, approval flow, on-complete behaviour
- **Spec layer** — PydanticAI-owned fields nested under `spec:`: model, instructions, capabilities, model settings

### Agent modes

| Mode | Behaviour |
|---|---|
| `autonomous` | Cron-triggered, fire-and-forget, return-type void |
| `interactive` | Request-response via `POST /run`, supports streaming |
| `hybrid` | Long autonomous run with a live HTTP query endpoint mid-run |

### One container, one agent

Each agent runs in its own Docker container. Containers communicate over a Docker internal network — no host port exposure required for inter-agent calls.

```http
http://researcher-agent:8000/run
```

---

## Quick start

**1. Install miragen**

```bash
pip install miragen
```

**2. Write your tools** (optional)

```python
# tools.py
from miragen import register

@register
async def get_weather(ctx, city: str) -> str:
    ...

@register("speak")
async def tts(ctx, text: str) -> None:
    ...
```

**3. Write an agent profile**

```yaml
# agents/assistant.yaml
name: assistant
mode: interactive
triggers:
  - type: http
spec:
  model: anthropic:claude-sonnet-4-6
  instructions: |
    You are a helpful assistant.
  capabilities:
    - WebSearch
  max_steps: 20
```

**4. Run**

```bash
AGENT_PROFILE=agents/assistant.yaml miragen run
```

Or with Docker:

```bash
docker build -t my-swarm .
docker run -e AGENT_PROFILE=agents/assistant.yaml -p 8000:8000 my-swarm
```

---

## Example project structure

**Per-agent workspace** (recommended — each agent has its own directory):

```fs
my-swarm/
├── agents/
│   ├── morning-briefing/
│   │   ├── agent.yaml
│   │   └── tools.py
│   └── assistant/
│       ├── agent.yaml
│       └── tools.py
├── compose.yml
└── secrets/
    └── anthropic_key.txt
```

**Monorepo** (simpler for small swarms with shared tooling):

```fs
my-swarm/
├── agents/
│   ├── morning-briefing.yaml
│   └── assistant.yaml
├── tools.py              # shared @register decorated functions
├── Dockerfile
├── compose.yml
└── secrets/
    └── anthropic_key.txt
```

## Security

>[!WARNING]
>miragen imposes no security requirements as mandatory, however some good practices are built into this guide.

miragen recommends running agents inside _docker containers_ or similar containerized environments.
miragen is built with docker-based usage in mind, and recommends _docker secrets_, as well as _non-priviledged users_ within the container to run the application/agent.

Why? [So that this won't be you for real lmao](https://x.com/gilpinskyy/status/2054254470595330363)

>[!CAUTION]
>take care when running agents with shell capabilities or coding tools; agents are vulnerable to [prompt injection](https://en.wikipedia.org/wiki/Prompt_injection) attacks.

miragen uses `Pydantic AI`'s built in hooks to suspend execution for tools defined in the yaml schema as `approval_required` and sends the request notification to the user-defined endpoint.

---

## Docker & Compose

### Pre-built base image (recommended)

A pre-built image is published at `ghcr.io/ieepirzy/miragen:latest`. Use it with volume mounts — no Dockerfile per agent needed.

Each agent workspace is mounted to `WORKDIR /agent` inside the container. `AGENT_PROFILE` defaults to `agent.yaml`, which is the profile filename at the root of that workspace.

```yaml
# compose.yml
secrets:
  anthropic_key:
    file: ./secrets/anthropic_key.txt

x-agent-base: &agent-base
  image: ghcr.io/ieepirzy/miragen:latest
  restart: unless-stopped

services:
  morning-briefing:
    <<: *agent-base
    container_name: morning-briefing
    secrets: [anthropic_key]
    environment:
      ANTHROPIC_API_KEY_FILE: /run/secrets/anthropic_key
      AGENT_PROFILE: agent.yaml
    volumes:
      - ./agents/morning-briefing:/agent

  assistant:
    <<: *agent-base
    container_name: assistant
    secrets: [anthropic_key]
    environment:
      ANTHROPIC_API_KEY_FILE: /run/secrets/anthropic_key
      AGENT_PROFILE: agent.yaml
    volumes:
      - ./agents/assistant:/agent

networks:
  default:
    internal: true
```

### Custom image (monorepo / extra dependencies)

Build your own image when you need shared tooling baked in or extra pip packages:

```dockerfile
FROM ghcr.io/ieepirzy/miragen:latest
COPY tools.py ./
COPY agents/ ./agents/
```

Or from scratch:

```dockerfile
FROM python:3.12-slim
WORKDIR /agent
RUN pip install --no-cache-dir miragen your-extra-dep \
    && adduser --disabled-password --gecos "" agentuser \
    && chown agentuser /agent
USER agentuser
EXPOSE 8000
CMD ["miragen", "run"]
```

---

## Agent profile reference

```yaml
# ── Swarm layer ───────────────────────────────────────────────
name: my-agent                    # unique ID, used as container name
                                  # (lowercase letters, digits, - and _; max 63 chars)
mode: autonomous                  # autonomous | interactive | hybrid

triggers:
  - type: cron
    schedule: "0 9 * * 1-5"      # standard cron expression
    default_prompt: |             # optional — injected if no prompt at runtime
      Run the morning briefing.

  - type: http                    # exposes POST /run on the container
    header_prompt: |              # optional — prepended to every /run request
      You are operating in strict mode.

  - type: interval                # fires every N seconds
    every_s: 900                  # >= 10, guards against accidental hot loops
    default_prompt: |              # optional — injected if no prompt at runtime
      Poll the feed.

  - type: startup                 # fires once when the container boots
    delay_s: 5                    # optional, default 0 — wait after boot before firing
    default_prompt: |
      Announce you are online.

approval_required:                # optional — glob patterns for human-in-the-loop
  - "delete_*"
  - "execute_*"
  - "register_*"                  # recommended if agent has code-execution tools

tools:                            # optional — whitelisted @register tool names
  - get_weather
  - speak

on_complete:                      # optional — autonomous run side effects
  log_to: miradb
  notify: telegram
  post_to: https://my-service.com/webhook

limits:                           # optional — spend guardrails, see "Budgets" below
  tokens_per_run: 200000
  tokens_per_day: 2000000
  on_exceeded: skip                # skip | notify

history_max_messages: 200         # optional — cap on /agent/history.json;
                                  # only the newest N messages are kept on load.
                                  # unset = unbounded (today's behaviour).

# ── PydanticAI spec layer ─────────────────────────────────────
spec:
  model: anthropic:claude-sonnet-4-6

  instructions: |
    You are my agent. Be concise.

  model_settings:
    max_tokens: 4096
    temperature: 0.3

  capabilities:
    - WebSearch
    - WebFetch
    - Thinking:
        effort: low               # low | medium | high
    - MCP:
        url: https://my-mcp-server.com/mcp
        name: my-server

  max_steps: 30
```

Profiles are validated **strictly**: unknown keys are rejected with an error naming the offending field, so typos like `aproval_required:` fail at `miragen validate` time instead of silently disabling the feature.

### Environment interpolation

Any string value in the profile can reference an environment variable, so the same YAML works unchanged across deployments instead of hardcoding webhook URLs, MCP server URLs, or channel IDs:

```yaml
on_complete:
  post_to: ${WEBHOOK_URL}                       # fails miragen validate if unset

spec:
  capabilities:
    - MCP:
        url: ${MCP_URL:-http://mcp:9000}        # falls back if unset
```

- `${VAR}` — substituted with the environment variable's value; validation fails with the variable name and its YAML path (e.g. `spec.capabilities[0].MCP.url`) if it's unset.
- `${VAR:-default}` — falls back to `default` when `VAR` is unset. An empty-string env value still counts as set.
- `$${VAR}` — escape hatch, renders the literal text `${VAR}`.
- Interpolation runs on every string value, including inside multi-line `instructions` blocks, but never on dict keys or non-string values (ints, bools stay untouched).

Agent containers already get `*_API_KEY` env vars injected by miragen-mcp's compose management, and file-based `*_FILE` secrets are resolved into the environment before the profile loads — so secrets are interpolatable out of the box. Be careful interpolating secrets into `instructions`, though: that puts them in the model's context window.

### Budgets

Autonomous + cron + LLM = unbounded spend. `max_steps` caps round-trips per run, but nothing caps tokens on its own — `limits` does:

```yaml
limits:
  tokens_per_run: 200000     # optional, >= 1 — per-run cap, enforced by PydanticAI
  tokens_per_day: 2000000    # optional, >= 1 — rolling UTC-day cap, enforced by miragen
  on_exceeded: skip          # skip (default) | notify — what a blocked scheduled run does
```

At least one of `tokens_per_run` / `tokens_per_day` must be set — an empty `limits: {}` block fails `miragen validate` as dead config.

- **`tokens_per_run`** wires straight into PydanticAI's `UsageLimits(total_tokens_limit=...)`. Exceeding it ends the run with PydanticAI's own usage-limit error; the [run record](#run-records) is written with `status: failed` and that error as `error`.
- **`tokens_per_day`** is a rolling UTC-day sum of `input_tokens + output_tokens` across this agent's run records (a run with no recorded usage — e.g. one that failed before the model responded — counts as 0, not unbounded). Checked before every run starts:
  - A cron/interval/startup run over budget is **skipped**, with a log line. If `on_exceeded: notify`, the `on_complete.notify` handler fires with a budget-exceeded message (not `log_to` or `post_to` — there's no run output to log).
  - `POST /run` and `POST /run/async` return `429` with the used/limit counts and the UTC reset time in the message.
  - The budget resets at UTC midnight — yesterday's records never count against today.

Cost-in-dollars conversion, per-model pricing, and cross-agent (swarm-wide) budgets are out of scope for now — this is a token-count circuit breaker, not a billing system.

---

## Capabilities

Built-in capabilities map directly to PydanticAI:

| Name | Config | Notes |
|---|---|---|
| `WebSearch` | — | Uses native model search where available |
| `WebFetch` | `local: bool` | Fetches URLs |
| `Thinking` | `effort: low\|medium\|high` | Extended reasoning |
| `ImageGeneration` | `fallback_model: str` | Image generation |
| `MCP` | `url: str`, `name: str` | Attach an MCP server |
| `Peer` | `agents: list[str]`, `timeout_s: int` (default 120) | Injects `call_agent(agent, prompt)` — swarm-to-swarm calls, restricted to an explicit allowlist |

### Swarm calls

`Peer` injects one tool, `call_agent`, that POSTs to another agent's `/run` over the Docker internal network:

```yaml
spec:
  capabilities:
    - Peer:
        agents: [researcher, writer]
        timeout_s: 120
```

Calls to agents outside the allowlist are rejected before any network request is made — `call_agent` returns an `ERROR: ...` string (never raises) naming the allowlist, so the model can self-correct. Connection failures and timeouts also come back as explanatory strings rather than exceptions.

Since the injected tool is named `call_agent`, gate it like any other tool with `approval_required: ["call_agent"]` if you want a human in the loop before an agent calls a peer.

Register custom capabilities from user code:

```python
from miragen import register_capability
from pydantic_ai.capabilities import AbstractCapability

class MyMemoryCapability(AbstractCapability):
    ...

@register_capability("Memory")
def _(cfg):
    return MyMemoryCapability(size=cfg.get("size", 1000))
```

Then use in YAML:

```yaml
capabilities:
  - Memory:
      size: 5000
```

> **Note:** MCP tools are injected automatically via the `MCP` capability. They do not need to appear in the `tools` whitelist — that is only for locally registered Python functions.

---

## HTTP API

Every agent container exposes:

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness check; includes `last_run` and `pending_approvals` |
| `/run` | POST | Trigger a run and wait for the result (all modes) |
| `/run/async` | POST | Trigger a run, return immediately with a `run_id` |
| `/run/stream` | POST | Streaming run (interactive / hybrid); the response carries an `X-Miragen-Run-Id` header for correlating with `GET /runs/{id}` |
| `/runs` | GET | List recent run records, newest first |
| `/runs/{run_id}` | GET | Full record for one run (accepts a unique id prefix) |
| `/history` | GET | Read-only slice of persisted conversation history (`?limit=` or `?run_id=`) |
| `/approvals` | GET | List pending `approval_mode: queue` requests |
| `/approvals/{request_id}` | POST | Resolve a pending approval request |
| `/profiles/resolve` | POST | Validate + resolve an EDF (canonical doc, SHA-256, executable profile) without starting a run |
| `/executor-runs` | POST | Idempotent, provenance-carrying executor launch (durable before ack) |
| `/runs/{run_id}/snapshot` | GET | Immutable resolved-EDF snapshot persisted at launch |
| `/runs/{run_id}/events` | GET | Executor event stream: tail read, or cursor replay with `?after=&limit=` |
| `/runs/{run_id}/diff` | GET | Harvested diff (executor tier, 404 until success) |
| `/runs/{run_id}/resume` | POST | Give a suspended/failed executor run another turn |
| `/runs/{run_id}/abandon` | POST | Human-terminal state; `?discard_workspace=true` to drop the workspace |
| `/schedules` | GET | List managed schedule bindings with versions and next fire times |
| `/schedules/{name}` | PUT | Create/update a managed binding (compare-and-swap via `expected_version`) |
| `/schedules/{name}` | DELETE | Remove a binding and unregister its job (optional `?expected_version=`) |

**Request**
```json
{ "prompt": "What is the weather in Helsinki?" }
```

**Response**
```json
{ "output": "Currently 14°C and overcast in Helsinki.", "run_id": "3f2a1b9c..." }
```

### Run records

Every run — cron, `/run`, or `/run/async` — is persisted as a JSON file under `/agent/runs/` with its status, timing, token usage, and a tool-call trace. This is what `docker logs` could never give you: "what did this agent do last night, and what did it cost?"

```
GET /runs?limit=20&status=succeeded
→ {"count": 3, "runs": [{"run_id": "...", "status": "succeeded", "trigger": "cron",
                          "prompt_preview": "...", "duration_s": 4.2, "usage": {...}}, ...]}

GET /runs/3f2a1b9c
→ {"run_id": "3f2a1b9c...", "status": "succeeded", "output": "...", "tool_calls": [...], ...}
```

`/run/async` is the non-blocking counterpart to `/run` — useful when a caller (notably miragen-mcp, with its own timeout) can't hold a connection open for a long-running agent:

```
POST /run/async {"prompt": "..."} → 202 {"run_id": "...", "status": "running"}
```

If the container is killed mid-run, the interrupted record is marked `interrupted` (not left `running` forever) the next time it starts. Retention defaults to the newest 200 runs; override with `MIRAGEN_RUN_RETENTION`.

### History

`GET /history` is a read-only view of the conversation history persisted at `/agent/history.json` (see [Interactive conversation history](#roadmap) below). Messages are flattened to plain `{"role", "content"}` pairs.

```
GET /history?limit=20
→ {"message_count": 12, "messages": [{"role": "user", "content": "..."}, ...], "run_id": null}

GET /history?run_id=3f2a1b9c
→ {"message_count": 8, "messages": [...], "run_id": "3f2a1b9c..."}
```

Without `run_id`, returns the newest `limit` messages (default 20, max 200). With `run_id`, returns the message slice that existed right after that run saved history, correlated via `history.runs.jsonl`; an unknown or non-history-saving `run_id` returns `404`. If `history.json` doesn't exist yet, returns an empty list rather than erroring.

---

## CLI

```bash
miragen run                              # start the agent server
miragen run --tools my_tools             # custom tools module
miragen run --port 9000                  # custom port
miragen validate agents/morning.yaml    # validate a profile without starting
```

Environment variables: `AGENT_PROFILE`, `TOOLS`, `HOST`, `PORT`.

---

## Approval flow

When an agent tries to call a tool whose name matches a glob in `approval_required`, execution suspends until a human approves or denies it. miragen does not hardcode any notification channel — you wire in your own.

### How globs work

```yaml
approval_required:
  - "delete_*"     # matches delete_file, delete_user, …
  - "execute_*"    # matches execute_shell, execute_code, …
  - "fs_write"     # exact match
```

Standard `fnmatch` glob syntax. Patterns are checked against the tool name at call time.

### register_approval_handler

```python
# tools.py
from miragen import register_approval_handler, ApprovalRequest, ApprovalResponse

@register_approval_handler
async def _(request: ApprovalRequest) -> ApprovalResponse:
    # request.agent_name, .tool_name, .tool_args, .request_id
    msg = f"Approve {request.tool_name}({request.tool_args})?"
    approved = await send_telegram_and_wait(msg)
    return ApprovalResponse(approved=approved)
```

The handler must be `async`. It is a single slot — only one handler per container. If no handler is registered and no webhook is configured, what happens next is governed by `approval_mode` (below) — by default, miragen logs a warning and **auto-approves** (fail open). This is intentional: unconfigured approval gates should not silently break agents during development.

### approval_webhook

For a no-code alternative, set `approval_webhook` in the agent profile. miragen will `POST` an `ApprovalRequest` JSON body to that URL and expect an `ApprovalResponse` back:

```yaml
approval_webhook: https://my-approval-service.com/review
```

```json
// POST body (ApprovalRequest)
{
  "agent_name": "researcher",
  "tool_name": "delete_file",
  "tool_args": {"path": "/data/report.csv"},
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}

// Expected response (ApprovalResponse)
{ "approved": true }
{ "approved": false, "prompt": "That file is read-only, try another path." }
```

If `prompt` is set in the response, it is folded back into the agent's context before execution resumes (approved) or is included in the denial message (denied). A registered handler always takes precedence over `approval_webhook`.

### approval_mode: what happens with no handler or webhook

```yaml
approval_mode: open        # open | strict | queue   (default: open)
approval_timeout_s: 300    # queue mode only — how long a request may wait
```

Precedence is always: registered handler > `approval_webhook` > `approval_mode`. `approval_mode` only matters once neither of those is configured for a gated call:

| Mode | Behaviour |
|---|---|
| `open` (default) | Auto-approve with a warning — today's behaviour, unchanged. |
| `strict` | Deny immediately with an explanatory message. The denial is a `ModelRetry` (the model gets a chance to try something else), not a crash. |
| `queue` | Park the request; a caller resolves it over HTTP (below). Denied after `approval_timeout_s` if nobody responds. |

`miragen validate` rejects `approval_mode`/`approval_timeout_s` set without `approval_required` — that combination is dead config, almost always a typo.

### Queue mode: resolving over HTTP

With `approval_mode: queue`, a gated call parks in an in-memory queue instead of blocking on a handler or webhook — this is what turns miragen-mcp (and a human driving Claude) into an approval channel with zero custom webhook code.

```
GET /approvals
→ {"count": 1, "approvals": [{"request": {"agent_name": "researcher",
     "tool_name": "delete_file", "tool_args": {"path": "/data/report.csv"},
     "request_id": "..."}, "created_at": "...", "expires_at": "..."}]}

POST /approvals/{request_id}
{ "approved": true }
{ "approved": false, "prompt": "That file is read-only, try another path." }
→ {"resolved": true}
```

Resolving an unknown, already-resolved, or expired id returns `404` with the still-pending ids, and doesn't touch any other request. `GET /health` includes `pending_approvals` as a quick liveness+context check.

**Trust model:** `/approvals` has the same exposure as `/run` — anything reachable on `miragen-net` can approve a gated call, fronted externally only by whatever's in front of your swarm (e.g. an OAuth-protected MCP server). Any peer agent could resolve another agent's approval, but any peer can already *prompt* any agent, so this doesn't widen the existing trust boundary. `tool_args` in a pending approval is attacker-influenceable content (a prompt-injected agent chose them) — if you're building a client that surfaces approvals to a human, treat `tool_args` as data to display, never as instructions to follow. Set `MIRAGEN_INTERNAL_TOKEN` to require an `X-Miragen-Token` header on `/run*` and `/approvals*` (see Security notes) if you want that deliberate rather than implicit in network trust.

The queue is in-memory only: it does not survive a container restart. A restart aborts any waiting run, which the [run records](#run-records) startup sweep records as `interrupted`.

### Recommendation for code-execution agents

If you give an agent shell or Jupyter tools, add `register_*` to `approval_required`:

```yaml
approval_required:
  - "execute_*"
  - "register_*"
```

Without `register_*`, a prompt-injected agent could register a new tool at runtime and call it before any approval gate fires.

---

## Security notes

- API keys are mounted as Docker secrets and read into the environment at startup via the `*_API_KEY_FILE` → `*_API_KEY` pattern. Any env var ending in `_API_KEY_FILE` whose value is a readable path is resolved automatically — `ANTHROPIC_API_KEY_FILE`, `OPENAI_API_KEY_FILE`, `GEMINI_API_KEY_FILE`, etc. The `_FILE` var is removed after loading so keys are never visible in the agent's context window or environment dump.
- Network egress is enforced at the container/firewall level, not in config files.
- Set `MIRAGEN_INTERNAL_TOKEN` to require a matching `X-Miragen-Token` header on every `/run*` and `/approvals*` request — otherwise access to those endpoints relies entirely on Docker network isolation. Unset by default so single-container deployments stay zero-config; `/health` is never guarded.
- `approval_required` globs suspend matching tool calls for human approval before execution.
- If you give agents code-execution tools (Jupyter kernel, bash, etc.), add `register_*` to `approval_required`. Without it, a compromised agent could register and call arbitrary tools at runtime.
- Containerized environments are recommended — they limit blast radius if an agent pulls in a malicious payload.

A multitude of tutorials exist for hardening docker containers, one I found that goes [straight to the point](https://github.com/ieepirzy/container-hardening/blob/main/README.md). I recommend checking it out.

---

## Executor backend tier

A second backend tier for self-harnessed executors: the executor owns its own agent loop and the contract inverts to **workspace-in / diff-and-events-out**. A profile declares exactly one of `spec` (model tier) or `executor` (executor tier); executor runs land in the same run store with resumable `suspended`/`failed` states, a persistent per-run workspace, a wall-clock `turn_timeout_s` enforced by miragen itself, and a diff harvested exactly once on success.

Three backends implement the contract (`executor.executor`):

- **`codex`** — via openai-codex-sdk. Install with `pip install miragen[codex]`.
- **`claude-code`** — via claude-agent-sdk. Install with `pip install miragen[claude-code]`.
- **`spawn`** — no-SDK fallback: an argv template (`executor.command`) is spawned in the workspace, stdout becomes the event stream, exit 0 harvests. No resume, no usage reporting.

Auth is per backend and always spawn-time state, never profile content. Codex: `codex_home` must contain `auth.json` — an ephemeral container with an empty `codex_home` fails auth on spawn. Claude Code: set `ANTHROPIC_API_KEY` in the container environment (or mount Claude Code OAuth credentials at `~/.claude`). Both cases plus `workspace_root` (keeps suspended/failed runs resumable across container restarts) mean mounting persistent volumes; see the `codex-executor` service in [compose.example.yml](compose.example.yml) for a working example.

Optionally, `executor.artifact_sink` names a place to *also* put the harvested diff after a successful run (Loimi `store_document` with `kind="executor_diff"` is the first sink). It is advisory by construction: sink failures mark `artifact_stored: false` on the run record but never change run status — the diff on disk stays the source of truth. Full reference: [docs/executor-tier.md](docs/executor-tier.md).

### Example profiles

A Claude Code worker with a Loimi artifact sink:

```yaml
name: claude-worker
mode: interactive
triggers:
  - type: http
limits:
  tokens_per_run: 500000       # exceeded -> suspended (budget), resumable
executor:
  executor: claude-code
  instructions: |
    You operate on the repository mounted in your workspace.
    Make the change, run the tests, leave the tree clean.
  approval_policy: never       # -> bypassPermissions; unattended runs must not stall
  turn_timeout_s: 1800         # wall-clock cap per turn -> suspended (timeout), resumable
  workspace_root: /agent/workspaces
  mcp_servers:                 # injected into the executor's own session
    - name: loimi
      url: https://loimi.mesh/mcp/
      bearer_token_env: LOIMI_TOKEN
  artifact_sink:               # optional: ALSO push harvested diffs here
    kind: loimi
    url: https://loimi.mesh/mcp/
    bearer_token_env: LOIMI_TOKEN
```

A Codex worker looks the same with `executor: codex` plus `codex_home: /agent/codex-home` (see the auth note above). A spawn fallback needs only an argv template — `{workspace}` and `{prompt}` are substituted per element; with no `{prompt}` placeholder the prompt arrives on stdin:

```yaml
executor:
  executor: spawn
  instructions: "One-shot batch job."
  command: ["my-agent-cli", "--repo", "{workspace}", "--task", "{prompt}"]
  turn_timeout_s: 600
```

Driving it is the same HTTP surface as the model tier, plus the executor endpoints:

```bash
curl -X POST localhost:8000/run -d '{"prompt": "fix the failing test in tests/test_api.py"}'
# -> {"run_id": "abc123...", "output": "..."}

curl localhost:8000/runs/abc123/diff        # the harvested diff (404 until success)
curl localhost:8000/runs/abc123/events      # the executor's event stream
curl -X POST localhost:8000/runs/abc123/resume -d '{"prompt": "keep going"}'   # suspended/failed only
curl -X POST "localhost:8000/runs/abc123/abandon?discard_workspace=true"       # human-terminal
```

### Control-plane contracts (EDF, idempotent launch, event replay)

For an external control plane (MiraRun), the executor tier also speaks three
substrate contracts (design record:
[docs/design/mirarun-substrate-contracts.md](docs/design/mirarun-substrate-contracts.md)):

- **`POST /profiles/resolve`** — strictly validates a `mirarun.io/v1alpha1`
  Environment Definition File, expands defaults/presets deterministically, and
  returns the canonical document, its SHA-256, the executable profile, and
  repository/secret binding plans — without starting a run.
- **`POST /executor-runs`** — launches one executor run with a caller
  `idempotency_key` and product `provenance`, persisted durably *before* the
  launch is acknowledged. A repeated key returns the original run instead of
  launching twice; if the process dies mid-dispatch, the swept `interrupted`
  record is what the retried key surfaces. When an EDF is supplied, the
  immutable resolved snapshot is persisted per run (`GET /runs/{id}/snapshot`).
- **`GET /runs/{id}/events?after=N`** — cursor-based replay over the same
  durable event stream: every event carries a per-run monotonic `seq` and a
  schema version, so a projector can page, deduplicate by `(run_id, seq)`, and
  rebuild from `after=0` at any time. Plain tail reads work unchanged.

`GET /health` advertises the installed version and the served contract
capabilities for client feature detection.

---

## Roadmap

- **Interactive conversation history** _(in progress)_ — `use_history: bool` on `/run`. Persists conversation turns to `/agent/history.json` using PydanticAI's `ModelMessagesTypeAdapter`. Stateless by default, opt-in continuity. `history_max_messages` caps unbounded growth (newest N kept); `GET /history` exposes it read-only. Semantic retrieval (RAG) is still open.
- **Autonomous vs interactive history split** — The agent's autonomous working memory (tool calls, cron run observations) and the `/run` interactive conversation history are kept as separate stores. Interactive history is not injected into autonomous context by default — the agent stays focused. It can access interactive history explicitly via a tool call when needed.
- **Hybrid mode interrupt handler** — When `/run` hits a hybrid agent mid-autonomous-run, the interrupt handler selectively decides what context from the interactive history to inject before resuming.
- **RAG over history** — Instead of injecting full conversation history into context, the agent retrieves only relevant parts via semantic search. Keeps token usage low for long-running agents.
- **Session key isolation** — Per-caller conversation history for multi-user deployments. Currently deferred — one global history per agent.
- **WebFetch capability** — Add `pydantic-ai-slim[web-fetch]` to base image deps so the `WebFetch` capability works out of the box.
- **Default model updated to `deepseek:deepseek-v4-flash`** — `deepseek-chat` is deprecated July 24 2026, scaffold default should reflect this.

---

## License

MIT
