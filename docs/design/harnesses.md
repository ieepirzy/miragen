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

A profile can swap harnesses (`spec.model`), and each keeps its own
conversation on disk, so DELETE also discards what the harnesses *not*
running now hold for the instance (mapping, session files, working
directory; `forget_inactive_harnesses`) and its entry in the served-by
ledger. Otherwise swapping back would resume the deleted conversation.

A busy instance gets a 409. Clients that rotate conversations (Mira's episodes)
use this to prune retired instances. Run records are telemetry and have their
own retention (`MIRAGEN_RUN_RETENTION`).

### Session lifecycle (compaction, rotation, handoff)

A conversation instance never ends, but the Grok session behind it does:
every model call re-sends the whole context. After each turn, the harness asks
a policy what to do (`miragen/harness/grok_lifecycle.py`):

| Action | What runs | Default trigger |
|---|---|---|
| none | — | below the compaction threshold |
| compact | a memory-save turn, then grok's `/compact` | context ≥ 150k tokens |
| rotate | one turn that saves memories and writes a handoff note, then a fresh session | the 3rd compaction point, or context ≥ 400k (grok-4.7's window is 500k) |

- **Maintenance runs after the user's turn returns.** It holds the agent lock,
  so the user's reply is never delayed. The next turn waits for maintenance to
  finish and then lands in the session it leaves behind.
- **The instance name never changes**, so a client's conversation never sees
  sessions. `GET /instances/{name}/session` reports `fresh` (the next turn
  opens a rotated session) from the moment the policy decides. A client can
  use it to add its own recent transcript. `POST /instances/{name}/rotate`
  rotates on request.
- **The handoff** is best-effort. The model is told the size limit
  (`MIRAGEN_GROK_HANDOFF_MAX_CHARS`, default 4000); a longer note is cut, not
  retried. The next session's first turn gets it inside `<handoff>`, together
  with facts about the previous session: its turns, its compactions, its
  context size at the end, why it rotated, and how many memory writes the
  plane accepted and when.
- **Grok's own auto-compaction** stays on as a safety net at 90% of the window
  (`MIRAGEN_GROK_NATIVE_COMPACT_PERCENT`). It emits the same
  `auto_compact_completed` notification, so it is counted and logged, but no
  memory save runs before it.
- **The memory plane** sees each rotated session as its own session
  (`<agent>-<instance>-s<n>`). Compactions and rotations are sent as
  `context.compacting` and `context.closed`, so the plane writes an episode
  and a checkpoint for each.
- **Retired sessions'** files are deleted after
  `MIRAGEN_GROK_SESSION_RETENTION_DAYS` (default 30).
- **The policy is a first guess.** The thresholds and "every 3rd" are
  env-tunable (`MIRAGEN_GROK_COMPACT_AT_TOKENS`,
  `MIRAGEN_GROK_ROTATE_AT_TOKENS`, `MIRAGEN_GROK_ROTATE_AFTER_COMPACTIONS`), or
  the whole function can be replaced (`MIRAGEN_GROK_LIFECYCLE_POLICY=module:attr`).
  `<grok_home>/lifecycle/<instance>.jsonl` records the context size per turn,
  every compaction's before and after sizes (and whether miragen or grok
  triggered it), memory writes, and each rotation. That is the data a better
  policy gets derived from.
- **Wire facts** (grok 1.0.41, observed live): usage (`response_completed`)
  and compaction (`auto_compact_completed {tokens_before, tokens_after}`)
  arrive as `_x.ai/session_notification`, not `session/update`. The context
  size is the last model call's input plus cache tokens. `/compact <hint>` is a
  command sent as a prompt, and a session with only a few turns doesn't shrink:
  recent turns are kept whole.
- `MIRAGEN_GROK_LIFECYCLE=off` disables all of this.

### Turns in a conversation instance

`POST /instances/{name}/turns {prompt, idempotency_key, provenance?}` → 202
`{turn_id}` starts a turn with the instance's history. It is the base tier's
name for an instance launch through `/executor-runs`, which stays for
executor jobs, and it has the same durable, idempotent acceptance: a retried
key returns the same turn (200, `duplicate: true`).

A turn is asynchronous because it can take minutes (tools, approvals). The id
is how a caller waits for it (`GET /instances/{name}/turns/{turn_id}`, the
same record as `/runs/{turn_id}`), resumes waiting after its own restart
without sending the turn twice, and attributes the turn's tool calls,
approvals and usage.

## The Claude Code harness

`spec.model: claude-code:<model>` (e.g. `claude-code:sonnet`; `claude-code:`
alone means Claude Code's default) runs the same base-tier turns on the Claude
subscription. It exists so a persistent agent can swap subscriptions (Grok
rate limited → Claude, and back) by changing `spec.model` and redeploying,
without any change on the client side.

**Runtime shape** (`miragen/harness/claude_code.py`, through claude-agent-sdk):
- **Processes.** There is one long-lived `claude` process per instance, run as the SDK's CLI binary (bundled with claude-agent-sdk; `CLAUDE_BIN` overrides it). Eviction and timeouts work as for Grok (`MIRAGEN_CLAUDE_MAX_PROCESSES`, `MIRAGEN_CLAUDE_IDLE_S`, `MIRAGEN_CLAUDE_TURN_TIMEOUT_S`); `MIRAGEN_CLAUDE_EFFORT` sets the effort level.
- **Hermetic.**
  - `CLAUDE_CONFIG_DIR=<MIRAGEN_CLAUDE_HOME>` (default `/agent/claude-home`) with no settings sources, no skills, no plugins and no CLAUDE.md.
  - The built-in tools are removed (`tools=[]`); the profile's `WebSearch`/`WebFetch` capabilities map to Claude Code's own.
  - `--strict-mcp-config` makes the gateway (server name `gw`) the only MCP server. Its bearer is `${MIRAGEN_GATEWAY_TOKEN}`, expanded from the child's environment.
  - `can_use_tool` allows only `mcp__gw__*` and the enabled web tools; permission mode is `default` (never `bypassPermissions`, which would skip the callback).
  - Tool search is off (`ENABLE_TOOL_SEARCH=false`): it would defer gateway tools behind a built-in the harness removes.
  - Prompts are delivered verbatim (no `@path` expansion, no slash commands).
- **Subscription only.** The SDK merges the parent environment into the child's, so the CLI is launched through an exec wrapper (`<claude_home>/miragen-claude`) that keeps an allowlist. The child sees `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or a login in `CLAUDE_CONFIG_DIR`, and never `ANTHROPIC_API_KEY`, a base URL or the deployment's upstream MCP secrets.
- **Sessions.** `<claude_home>/miragen-instances.json` maps each instance to its Claude Code session. It is separate from Grok's, so swapping back resumes Grok's own sessions. A (re)started process resumes with `--resume`. A missing session file starts a new session and reports `fresh`.
- **Per-turn context** (the memory packet) rides the prompt. The system prompt is `spec.instructions` (plus voice guidance) and stays byte-stable, so the prompt cache holds. An instructions change takes effect when the process next starts: Claude Code takes the system prompt per launch, not per session.
- **Lifecycle.** Claude Code compacts on its own. By default that happens near the model's full window (sonnet: 967k, observed), so a persistent agent sets `MIRAGEN_CLAUDE_AUTO_COMPACT_WINDOW` (→ `CLAUDE_CODE_AUTO_COMPACT_WINDOW`; 100000 is Claude Code's floor and compacts at 67k, observed via `get_context_usage`). Its PreCompact hook reaches the memory plane as `context.compacting`. There is no miragen compaction/rotation policy and no handoff note here. `POST /instances/{name}/rotate` starts a new session (the plane sees `context.closed`), and `DELETE /instances/{name}` removes the process, mapping, session files and working directory.
- **Clock order** as for Grok: with gated tools, `MCP_TOOL_TIMEOUT` is `approval_timeout_s + 120` s and the turn timeout is raised above it.

### Swapping harnesses

Each harness keeps its own conversation state, so after a swap the next turn
continues a conversation that missed whatever the other harness served. Both
harnesses record which of them served each instance's last turn
(`<runs>/harness/served.json`, `miragen/harness/served.py`) and report
`fresh: true` from `GET /instances/{name}/session` when that was the other
one. A client adds its own recent transcript on `fresh`, exactly as after a
rotation. The Claude Code harness also continues the memory plane's session
numbering after the other harness (`seq`). Grok, on a swap back, keeps its
own `seq`.

**Verified live** (claude 2.1.284, claude-agent-sdk 0.2.161, subscription token, real gateway): a gateway tool call attributed to its instance through the env-expanded bearer; no built-ins reachable; a bogus `ANTHROPIC_API_KEY` in the parent did not reach the child; resume after a restart; streaming; ephemeral runs; rotate and forget; WebSearch + WebFetch; `MCP_TOOL_TIMEOUT` honoured through the wrapper (a 4 s limit timed out a 12 s tool); ~260 MB RSS per claude process.

## The Codex harness

`spec.model: codex:<model>` (e.g. `codex:gpt-6-sol`) runs base-tier turns on
the ChatGPT subscription through `codex app-server` (JSON-RPC over stdio,
`miragen/harness/codex.py`). There is one long-lived app-server per instance;
a thread per instance is the conversation, and it is resumed after restarts.
The binary is the one bundled with openai-codex (`CODEX_BIN` overrides it).

**The tool boundary is three layers**, because Codex has no switch that turns
off all built-in tools. Each layer was verified live against codex 0.159:

1. **Feature flags** (`FEATURES_OFF`) remove the shell (`shell_tool`,
   `unified_exec`), sub-agents, plugins, apps, memories, the browser and
   computer use, image tools and hooks. The model still sees some tool names,
   but none of them can reach a file or a process:
   - `exec` (code mode) stays on: gpt-6 models call every tool, including
     the dynamic ones, as JS inside it. With its host off, every tool call
     failed (observed live on `gpt-6-sol`).
   - `collaboration.*`: a spawned sub-agent gets the same restricted tools.
   - `apply_patch`: routed to approvals.
   - `web.run`.

   Code mode is a bare ECMAScript isolate: no `process`, `require`,
   `import`, `fetch` or sockets. Its nested calls go through layer 2 like
   any other.
2. **Deny by default.** The thread runs with sandbox `read-only`,
   `approvalPolicy: untrusted` and the `user` reviewer. Every file change,
   command, permission escalation and elicitation arrives at miragen as a
   server request and is declined; so is any request miragen doesn't know.
   This covers requests from inside code mode and from sub-agents too.
3. **Nothing worth taking.** The process gets an allowlisted environment:
   no `OPENAI_API_KEY`/`CODEX_API_KEY`, no upstream MCP secrets and no gateway
   credential. `auth.json` sits in `CODEX_HOME`, which no remaining tool can
   read. A login with `auth_mode` other than `chatgpt` is refused.

**The gateway's tools are dynamic tools, not MCP.** Codex defers MCP tools
behind a discovery step that the model often skips (observed: it answered
without ever finding the tool). Dynamic tools are declared on the thread, use
the experimental app-server API, and are always in context. Each call arrives
as `item/tool/call` and runs through `ToolGateway.call_tool`, which handles
approvals, run binding and call records. The list is re-declared on resume.
A 90-second call did not time out, so approval waits are not cut short by
Codex. Verified in the image through `POST /instances/{name}/turns` with an
upstream MCP tool and `approval_mode: queue`. Once approved, the action ran
once; when denied, it never ran.
A 16-minute approval wait was tested with gpt-6-sol. Code mode does not block on
a pending tool call: `exec` yields, and the model polls `wait`. There were 20 polls
in about 9 minutes, each a full-context model call, but only one approval request.
The run ended when OpenAI reported the model at capacity, not on a Codex timeout.
So a long approval costs quota on Codex, which blocking MCP calls on Grok and
Claude do not.
`approval_delivery: async` (README, "Async delivery") avoids this: the gated
call answers at once and runs when approved.

- **Tool-set changes.** Codex fixes a thread's dynamic tools at `thread/start`; `thread/resume` and `thread/fork` ignore new ones (observed). The instance state records the declared set.
  - **Same or fewer tools** (an upstream is down right now): the thread resumes with the declared set, and a missing tool fails at call time.
  - **A new tool name:** a new thread declares the union, and the old thread's model-visible history since its last compaction is carried in with `thread/inject_items`. That history is the compaction's replacement history, including its encrypted summary, followed by the user and assistant messages. Verified live: the new thread used the new tool and still knew facts from the old one. If the carry fails, the instance reports `fresh`.
- **Sessions.** `<MIRAGEN_CODEX_HOME>/miragen-instances.json` maps each instance to its thread. A resume that fails (the rollout is gone) starts a new thread and reports `fresh`. `rotate` starts a new thread; `forget` deletes the rollouts and the working directory.
- **Output.** A turn's reply is its `final_answer` messages; `commentary` is used only when there is no final answer. Notifications are filtered by thread, so sub-agent text never leaks into the reply.
- **Lifecycle.** Codex compacts on its own at `MIRAGEN_CODEX_AUTO_COMPACT_TOKENS` (`model_auto_compact_token_limit`). The limit was verified live: with it set to 4000, compaction fired and the model still recalled earlier facts. Auto-compaction arrives as a `contextCompaction` item (`thread/compacted` is the manual one); either reaches the memory plane as `context.compacting`. `context_tokens` is the last model call's input.
- **Instructions.** `spec.instructions` (plus voice guidance and a short tool note) replaces Codex's own base instructions. Unlike tools, they are sent on every resume and take effect: a changed prompt applied to an existing thread (observed live).
- **Login.** Once per `CODEX_HOME`, on the subscription:
  `docker exec -it -e CODEX_HOME=/agent/codex-home <container> codex login --device-auth`.
- **Costs.** About 220 MB RSS per app-server. Models on the subscription (2026-09-29): `gpt-6-astra` (default), `gpt-6-sol`, `gpt-6-luna`, and older `gpt-5.x`.
