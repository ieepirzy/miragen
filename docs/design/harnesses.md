# Base-tier harnesses

Status: accepted direction (Ilari, 2026-09-24). PR A (this seam) is
implemented; the tool gateway and the Grok Build harness follow.

## Why

The base tier (`spec:` profiles) was PydanticAI-only. PydanticAI was never
meant to be the only harness. A self-contained agent CLI such as Grok Build
(on its subscription) is a harness the same way Claude Code is, even when it
drives a single model family. Later candidates include Hermes.

The executor tier is a different thing. It runs one-off worker jobs: a
workspace, diff harvest, resumable threads, interventions. It is not the
home for a persistent conversational agent.

## Decisions

1. **Selection by model prefix.** `spec.model: grok-build:<model>` picks the
   Grok Build harness. This mirrors the `claude-code:<model>` selector
   convention. Any other string stays a pydantic-ai model string.
   Consequences:
   - A profile that relies on `spec.model` as a fallback PydanticAI model
     (the memory recall selector, the extraction worker) must name
     `memory.recall.model` / `memory.extraction.model` explicitly.
2. **The seam.** The app tier keeps these unchanged across harnesses:
   - instances, admission and run records;
   - the memory boundary;
   - telemetry run spans;
   - triggers and schedules.

   A harness only runs the turn: `run(turn)`, `stream(turn)` and `aclose()`,
   defined in `miragen/harness/base.py`. PydanticAI is the default harness,
   and its behaviour is unchanged.
3. **Tools through a miragen tool gateway.** A foreign harness can't call
   Python tools or use PydanticAI's approval hooks, so miragen serves the
   agent's whole tool set as one MCP endpoint on the agent container:
   - proxied `spec.capabilities` MCP servers, with per-run credentials
     injected by miragen;
   - runtime tools (memory, scheduling, speak, ask-human);
   - `@register` local Python tools.

   `approval_required` enforcement and tool-call records happen in the
   gateway, which fails closed. The host owns every tool call, as in the
   PydanticAI tier.
4. **Grok's session is the conversation's source of truth.** Each instance
   maps to a native Grok session, and Grok compacts it itself.
5. **Transport: ACP, one long-lived `grok agent` process per instance.** The
   agent container is persistent. Spawning the CLI per turn is out. The ACP
   client bugs (miragen#145) are fixed as part of this work. Idle processes
   are evicted and resumed with `session/load`, re-sending `mcpServers` and
   `cwd`.
6. **The memory packet rides in the prompt for Grok (option a).** Grok takes
   system rules only at `session/new`, so the per-turn packet is prepended to
   the turn's prompt and persists in Grok's transcript. Grok's compaction
   handles it. This is a deliberate exception to §17.3's "transient packet"
   rule, made because relying on the agent to pull memory itself is brittle.
   If a per-turn system channel turns up in probing, prefer it.

7. **System instructions are stable, and a change forks the session.**
   `spec.instructions` goes in once, at `session/new`. Nothing is expected
   to edit the system prompt routinely. Per-turn context (the memory
   packet, runtime frames) goes in at the user-prompt level.
   - miragen stores an instructions hash beside each instance's session.
   - When the hash no longer matches, the next turn forks the Grok session.
     History is kept, and the fork carries the new instructions.
   - Instructions are never re-sent every turn.

## Open

- **The tool boundary for Grok.** Built-ins are removed through the agent
  profile (`tools: search_tool, use_tool`), a host-side `pre_tool_use` client
  hook denies anything outside the gateway, and the gateway fails closed.
  Nothing is claimed until a live probe with a subscription login shows it.

## The Grok Build harness (PR C)

A profile opts in with `spec.model: grok-build:<model>` (`grok-build:` alone
means Grok's default model):

```yaml
name: mira
mode: interactive
triggers: [{type: http}]
spec:
  model: grok-build:grok-4.6
  instructions: |            # the session's rules; a change forks the session
    You are Mira…
  capabilities:              # MCP only on a gateway harness
    - MCP: {name: mira, url: http://mira:8441/mcp, bearer_token_env: MIRA_MCP_TOKEN}
approval_required: ["mira_perform_device_action"]   # enforced by the gateway
voice:
  provider: http
  url: http://mira-voice:8450/speak
  instructions_file: speak-instructions.md
```

**Runtime shape**
- There is one `grok agent … stdio` process per instance. Each process gets:
  - `--agent-profile <GROK_HOME>/miragen-agent-profile.md` (`tools: search_tool, use_tool`);
  - an allowlisted environment: `HOME=<GROK_HOME>/hermetic-home`, the instance's `MIRAGEN_GATEWAY_TOKEN`, and no `XAI_API_KEY` or `GROK_*`;
  - a working directory of `<MIRAGEN_GROK_WORKDIRS>/<instance>`.
- **Session state.** `<GROK_HOME>/miragen-instances.json` maps each instance to its Grok session id and an instructions hash.
  - A (re)started process resumes the session with `session/load`.
  - A changed hash forks the session (`x.ai/session/fork` with the new `rules`).
- **Ephemeral runs.** A run without `use_history`/instance gets a fresh session in a process that exits after the turn.
- **Eviction.** `MIRAGEN_GROK_MAX_PROCESSES` (default 3) caps idle processes (least recently used goes first), and `MIRAGEN_GROK_IDLE_S` (default 900) stops idle ones. Either way the conversation resumes on the next turn.
- **Timeouts.** `MIRAGEN_GROK_TURN_TIMEOUT_S` (default 600) sends `session/cancel` and drops the process.
- **Tool calls.** The gateway is served at `/mcp/gateway/` on the agent's own port, and is the only MCP server in the hermetic config.
  - Permission requests that don't name a `gateway__*` tool are rejected host-side.
  - Approvals and tool-call records happen in the gateway.
- **Login.** Log in once per `GROK_HOME`, on the subscription:
  `docker exec -it -e HOME=/agent/grok-home/hermetic-home -e GROK_HOME=/agent/grok-home <container> grok login --device-code`.
  A missing login fails the turn with that instruction (strict auth). The harness never falls back to an API key.

**Verified against a scripted `grok agent`** (`tests/fixtures/fake_grok_agent.py`, seeded with grok 1.0.41's real `initialize`), and still owed a live, logged-in probe:
- Does the agent profile actually remove the built-ins?
- Do permission requests name `gateway__<tool>`?
- Is `requirements.toml`'s MCP allowlist honoured in ACP mode?
- Does `x.ai/session/fork` accept `rules`?
- Is `session/load` enough to restore the config-declared MCP server?

### Discarding an instance

`DELETE /instances/{name}` asks the harness to forget the instance when the
harness owns its conversation. For Grok Build, that means:

- stop its process;
- drop its session mapping;
- delete `$GROK_HOME/sessions/<percent-encoded cwd>/`, which holds every
  session and fork for that working directory;
- delete the working directory itself.

A busy instance gets a 409. Clients that rotate conversations (Mira's episodes)
use this to prune retired instances. Run records are telemetry and have their
own retention (`MIRAGEN_RUN_RETENTION`).
