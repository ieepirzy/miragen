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
| `/health` | GET | Liveness check |
| `/run` | POST | Trigger a run (all modes) |
| `/run/stream` | POST | Streaming run (interactive / hybrid) |

**Request**
```json
{ "prompt": "What is the weather in Helsinki?" }
```

**Response**
```json
{ "output": "Currently 14°C and overcast in Helsinki." }
```

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

The handler must be `async`. It is a single slot — only one handler per container. If no handler is registered and no webhook is configured, miragen logs a warning and **auto-approves** (fail open). This is intentional: unconfigured approval gates should not silently break agents during development.

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
- `approval_required` globs suspend matching tool calls for human approval before execution.
- If you give agents code-execution tools (Jupyter kernel, bash, etc.), add `register_*` to `approval_required`. Without it, a compromised agent could register and call arbitrary tools at runtime.
- Containerized environments are recommended — they limit blast radius if an agent pulls in a malicious payload.

A multitude of tutorials exist for hardening docker containers, one I found that goes [straight to the point](https://github.com/ieepirzy/container-hardening/blob/main/README.md). I recommend checking it out.

---

## Roadmap

- **Interactive conversation history** _(in progress)_ — `use_history: bool` on `/run`. Persists conversation turns to `/agent/history.json` using PydanticAI's `ModelMessagesTypeAdapter`. Stateless by default, opt-in continuity.
- **Autonomous vs interactive history split** — The agent's autonomous working memory (tool calls, cron run observations) and the `/run` interactive conversation history are kept as separate stores. Interactive history is not injected into autonomous context by default — the agent stays focused. It can access interactive history explicitly via a tool call when needed.
- **Hybrid mode interrupt handler** — When `/run` hits a hybrid agent mid-autonomous-run, the interrupt handler selectively decides what context from the interactive history to inject before resuming.
- **RAG over history** — Instead of injecting full conversation history into context, the agent retrieves only relevant parts via semantic search. Keeps token usage low for long-running agents.
- **Session key isolation** — Per-caller conversation history for multi-user deployments. Currently deferred — one global history per agent.
- **WebFetch capability** — Add `pydantic-ai-slim[web-fetch]` to base image deps so the `WebFetch` capability works out of the box.
- **Default model updated to `deepseek:deepseek-v4-flash`** — `deepseek-chat` is deprecated July 24 2026, scaffold default should reflect this.

---

## License

MIT
