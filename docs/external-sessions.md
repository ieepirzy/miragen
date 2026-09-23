# External sessions: Claude Code, Codex and Grok Build on the memory substrate

`miragend` can run on a developer machine as the **local memory-participation
daemon**: every ordinary `claude`, `codex` or `grok` process joins the same memory
substrate MiraGen-owned agents use, without a wrapper command and without
MiraGen spawning it. Design record: [docs/design/external-sessions.md](design/external-sessions.md).

```text
claude / codex ──hooks──▶ miragen-hook ──HTTP──▶ miragend ──/memory/v1──▶ Loimi
                          (stdlib, ≤10 s,        session registry,          (network,
                           fails open)           project → scope,           principal token)
                                                 inject / capture /
                                                 compact / finalize
```

What a session gets, automatically:

| When | What miragend does |
|---|---|
| `SessionStart` (startup, resume, **after compaction**, fork) | registers the session, resolves the repository → its Loimi scope, restores the project's **working state** by identity and injects it with the versioned memory guide as `additionalContext` |
| `UserPromptSubmit` | captures the prompt (idempotent), and — when a selector model is configured — runs the zero-or-more recall lane and injects only what was selected |
| `Stop`, `PostToolUseFailure`, `SubagentStop` | captures the turn outcome / failure / child result as operational trail |
| `PreCompact` | captures, then writes a **session episode** (prompts, last outcome, counters — extraction-eligible) and checkpoints `last_session` into the project's working state, *before* the harness compacts |
| `SessionEnd` | same finalization; the session is marked ended |
| process killed, no `SessionEnd` | the sweeper notices the pid is gone and finalizes anyway |
| daemon restarted mid-session | the next hook re-registers the session; journaled captures that never reached Loimi are replayed (idempotency keys make this safe) |

Failure behaviour: the harness never waits more than the adapter's bound
(10 s for context, 2 s for captures, 1 s at session end) and never sees a
non-zero exit. A missing daemon costs one connection refusal per hook. A
missing Loimi degrades **explicitly**: the injected guide says `MEMORY
DEGRADED`, `/health` counts it, and captures stay journaled for replay.

## Hosted: one daemon for your machine, cloud sessions and claude.ai

The same daemon runs next to a production Loimi and serves every harness
over HTTPS (design record: [design/hosted-bridge.md](design/hosted-bridge.md)).
It adds three things to the local picture above:

- **Identity from anywhere.** The adapter reports the repository's origin
  URL, its hostname and whether the harness declared itself remote; the
  daemon maps a cloud checkout of `github.com/org/repo` to the same project
  scope as your laptop's clone. A raw path (HTTP hooks) is identified by
  directory name and adopts a project the daemon already knows by that name.
- **The Loimi artifact store.** Each session gets a run; compaction and
  session-end episodes are filed as `session_episode` artifacts under it;
  the run closes at session end. The injected header carries `store_run=…`.
- **The bridge MCP (`/mcp`).** `memory_*` and `store_*` tools for the model,
  guarded by the daemon bearer and, optionally, origo OAuth for claude.ai.

### Deploy the daemon

Image `ghcr.io/ieepirzy/miragend` with `MIRAGEND_LIFECYCLE=off` needs no
Docker socket. Environment:

| Variable | Meaning |
|---|---|
| `MIRAGEND_SESSIONS_CONFIG` | path to `sessions.yaml` (see `scripts/miragend-local/sessions.example.yaml`) |
| `MIRAGEND_TOKEN` (`_FILE`) | the bridge bearer every client sends |
| `MIRAGEND_HOST` / `MIRAGEND_PORT` / `MIRAGEND_STATE_DIR` | bind + state (a volume: sessions, journal, the minted principal token, OAuth state) |
| `LOIMI_MEMORY_URL` | Loimi base URL (`http://loimi:8400` on the shared network) |
| `LOIMI_OPERATOR_TOKEN` (`_FILE`) | Loimi's store bearer: provisions the principal and scopes, and is the artifact store credential unless `LOIMI_STORE_TOKEN` is set |
| `LOIMI_MEMORY_TOKEN` | optional — an operator-minted principal token; unset lets the daemon mint its own |
| `MCP_BASE_URL`, `MCP_CLIENT_ID`, `MCP_CLIENT_SECRET`, `MCP_AUTO_APPROVE` | all together or none: origo OAuth on `/mcp` for claude.ai custom connectors |

`GET /health` → `sessions.principal_source` (`environment` / `state_dir` /
`created` / `minted` / `missing`), `sessions.loimi.shared_scopes`,
`sessions.store`, `sessions.mcp`.

### Join from Claude Code — the plugin

```shell
/plugin marketplace add ieepirzy/miragen
/plugin install miragen-memory@miragen
```

Enter the daemon URL and the bearer when asked. Hooks and the
`miragen-bridge` MCP server are configured by the plugin; nothing is
installed on the machine (the adapter is stdlib Python 3). For cloud
sessions, enable the plugin for your claude.ai account or declare it in the
repository's `.claude/settings.json` (`extraKnownMarketplaces` +
`enabledPlugins`), allow the daemon's host in the environment's network
access, and set `MIRAGEND_URL` / `MIRAGEND_TOKEN` there — see
[plugins/miragen-memory/README.md](../plugins/miragen-memory/README.md).

### Join from an environment you do not control — one bootstrap

```bash
curl -fsSL https://raw.githubusercontent.com/ieepirzy/miragen/main/scripts/cloud-bootstrap.sh | bash
```

`scripts/cloud-bootstrap.sh` installs the plugin (marketplace + plugin, no
token option — the hooks read `MIRAGEND_TOKEN` from the environment) AND
writes `type: http` hooks into that user's `~/.claude/settings.json`,
merging with what is there. Either path alone is enough; the daemon drops
raw hooks for a session it already knows through the plugin adapter, so
both together never inject twice. It never fails the caller. Put that one
line in a Claude Code cloud environment's **setup script** and every
session in that environment joins the bridge without any repository
carrying configuration. The environment must set `MIRAGEND_TOKEN` (and
optionally `MIRAGEND_URL`) and allow the daemon's host.

### Join from Claude Code — HTTP hooks in a repository

```bash
MIRAGEND_URL=https://memory.example miragen-hook install claude-code --http \
    --settings .claude/settings.json
```

The harness POSTs raw payloads to `/sessions/v1/hooks/claude-code` with
`Authorization: Bearer $MIRAGEND_TOKEN` from its own environment.

### Codex and Grok Build: the daemon sets them up

Harnesses may be started by mirarun with nobody present, so nothing here
needs a per-machine step or a trust prompt. Every machine that runs an AI
harness runs a local `miragend`, and **the daemon writes the harness setup**
(`miragen_hook/harness_setup.py`, run by `miragen/daemon/harness_setup.py`)
at startup and every `interval_s` (default 10 min):

```yaml
# sessions.yaml of the LOCAL daemon
harness_setup:
  url: https://memory.muutto365.fi        # where harness sessions report (the hosted bridge)
  token_file: ~/.config/miragend/bridge.token   # 0600, that bridge's bearer
  # enabled: true | false   (default: on when url is set and not in a container)
  # interval_s: 600
```

(or `MIRAGEND_HARNESS_SETUP_URL` / `_TOKEN_PATH` / `_INTERVAL_S`, and
`MIRAGEND_HARNESS_SETUP=off`). There is no default URL — the feature stays
off, and `/health` says why, rather than pointing sessions at a daemon
nobody chose. The hosted daemon (a container) never runs it unless
`enabled: true`. Each harness is skipped while its home does not exist: the
homes come from the daemon's own environment (`GROK_HOME`, `CODEX_HOME`,
default `~/.grok`, `~/.codex`), so **a systemd unit must carry a relocated
home** (`Environment=CODEX_HOME=…`). What it writes, and only that:

| Harness | Files | Notes |
|---|---|---|
| both | `<home>/miragen-adapter/<digest>/miragen_hook/` | a stable copy of the stdlib adapter; a superseded copy is deleted a day after it stopped being referenced (running sessions keep their command line) |
| both | `<home>/miragen-adapter/setup.json` | the URL + token file baked below; the plugin's MCP proxy reads it so tools reach the daemon the hooks reach |
| Grok Build | `$GROK_HOME/hooks/miragen.json` | every event, `python3 <copy>/miragen_hook/__main__.py grok-build --daemon URL --token-file PATH` (hook files load in every Grok mode with no trust step) |
| Codex | our groups in `$CODEX_HOME/hooks.json` | harness `codex` explicit; user groups untouched and kept in place |
| Codex | `[hooks.state."<key>"] trusted_hash` in `config.toml` | for exactly our entries, computed as Codex does (`hook_hash`); `codex exec` silently skips untrusted hooks |
| Codex | `[mcp_servers.miragen-bridge]` in `config.toml` | the stdio proxy with `--daemon`/`--token-file` in its args (Codex clears the MCP environment); `default_tools_approval_mode = "approve"` (`codex exec` refuses tools that need approval) |

TOML is edited table by table and re-parsed; an edit that would change
anything outside our tables (our key defined inline or dotted, say) is
refused and reported on `/health` instead of written. A second run with
nothing to change writes nothing. `GET /health` → `harness_setup`: enabled,
reason, url, and per harness installed / current / last_changed /
last_error. By hand (debug, or a machine without a daemon):
`miragen-hook setup {grok-build,codex} --daemon URL [--token-file F] [--home DIR] [--remove]`.

**MCP tools** come from the plugin, as `miragen-hook mcp-proxy`: stdio
JSON-RPC ↔ Streamable HTTP `POST <url>/mcp`, resolving URL and token like
the hooks (`--daemon`/`--token-file` → the daemon's `setup.json` →
`MIRAGEND_URL`/`MIRAGEND_TOKEN` → the option saved in Claude Code → the
manifest default). Under Grok it names the session on the connection
(`X-Harness-Session: grok-build:$GROK_SESSION_ID`), and where no daemon
manages the Grok hooks it writes them itself (effective from the next
session; never over a daemon's). Codex gets no session header — the model
passes `session`.

### Join from Grok Build

Grok Build ≥ 1.0 loads the Claude Code plugin when it is enabled in
`~/.claude/settings.json`; `.grok-plugin/plugin.json` gives it the MCP proxy
and an empty hooks file. The lifecycle hooks (harness `grok-build`, session
key `grok-build:<sessionId>`) are the hook FILE the daemon writes, because
Grok 1.0.41 registers plugin hooks only after a plugin reload, never at
session start (source: `spawn.rs` builds the session registry with
`discover_hooks`, plugin hooks arrive only via `apply_plugin_registry_snapshot`;
confirmed live). `miragen-hook install grok-build` remains as a manual
fallback. Contract facts, read from the xai-org/grok-build source at 1.0.41
(2026-09-23), that shaped the adapter:

| Fact | Consequence |
|---|---|
| camelCase payload with a closed list of snake aliases (`hook_event_name`, `session_id` yes; `promptId`, `stopHookActive`, `lastAssistantMessage` no) | normalizer reads both spellings |
| SessionStart `source` is `new`/`load`; an extra observe-only Stop fires at session end (`reason` `channel_closed`/`shutdown`) | `load` restores; that Stop is dropped |
| SessionStart/UserPromptSubmit stdout is discarded; `additionalContext` is honoured on Pre/PostToolUse and Stop | context queued and delivered on the next tool result (`client.context_delivery = "deferred"`) |
| no `CLAUDE_PLUGIN_OPTION_*`, no `${user_config.*}`; the hook inherits grok's process env; `GROK_HOOK_EVENT` is set for every hook (and `CLAUDE_PROJECT_DIR` too) | URL: env → saved Claude option → manifest default; `GROK_HOOK_EVENT` marks a Grok session even under a `claude-code` entry |
| subagents run as their own sessions, marked `subagentType` | only the parent's Subagent* events are recorded |
| HTTP hooks have no `headers` | the `--http` install is Claude Code only |
| plugin hooks load only after `/reload-plugins` | hooks installed as a file; the plugin declares none |
| Claude Code hook entries (settings files, plugins) also run under Grok | a non-`grok-build` miragen entry does nothing when `GROK_HOOK_EVENT` is set; the repository's `type: http` hooks still fire and get 401 (Grok http hooks carry no headers) — noise only |

Live-verified 2026-09-23 against the real 1.0.41 binary, driven by a scripted
OpenAI-compatible stub model (a BYOK `[model.*]` entry; no xAI credentials)
and this daemon over in-memory Loimi fakes: every lifecycle event arrived as
`grok-build`, the start block reached the model as a system reminder after
the first tool result exactly once, `memory_checkpoint` without `project`
landed in the session's project scope, and the hooks found the daemon via
the saved Claude option with no `MIRAGEND_URL`.

### Join from Codex

Codex 0.156 (source-read and live-verified 2026-09-23): the daemon's native
hooks and MCP entry (above) are the path. The plugin (`codex plugin
marketplace add ieepirzy/miragen && codex plugin add miragen-memory@miragen`)
is optional: its `.codex-plugin/plugin.json` wins over `.claude-plugin` and
declares an **empty** hooks file (the Claude `hooks/hooks.json` fallback
labelled every Codex event `claude-code`, and untrusted plugin hooks never
run under `codex exec`) plus the MCP proxy (`cwd: "."`, `env_vars` for
`MIRAGEND_URL`/`MIRAGEND_TOKEN`/`CODEX_HOME`); the daemon's
`[mcp_servers.miragen-bridge]` shadows it where both exist.

| Fact | Consequence |
|---|---|
| non-managed hooks run only when `[hooks.state."<file>:<event>:<group>:<handler>"].trusted_hash` matches; `codex exec` skips others silently | the daemon writes the hash for our entries (pinned by a test against hashes Codex itself accepted; a flipped byte live-verified to drop exactly that hook) |
| SessionStart / UserPromptSubmit `additionalContext` reaches the model as a developer message; above ~2,500 tokens (bytes/4) it is spilled to a file and previewed | our entries set `additionalContextLimit: 6000`; the adapter caps Codex context below that in UTF-8 bytes, head kept |
| no PostToolUseFailure event; PostToolUse on a failed command carries only the output text (no exit code) | tool failures are **not captured** under Codex |
| no Notification event, no `CODEX_*` marker env | harness named explicitly in every entry |
| plugin MCP: no variable expansion, `headers` ignored, a cleared environment | the stdio proxy, told where to look by arguments or `env_vars` |
| `codex exec` runs with approval policy `never` | the bridge's MCP tools are `approve`d up front |

Live (2026-09-23, codex-cli 0.156.1, a scripted Responses-API stub, this
daemon on a scratch port over in-memory Loimi, network-isolated): with no
bypass flag, every event arrived as `codex`, the start block reached the
model, `bridge_status` ran through the proxy from the daemon-written entry,
from the plugin entry alone, and with both installed (one set of events,
the daemon's server winning).

### Join from claude.ai / any MCP client

Custom connector URL `https://<daemon>/mcp` (OAuth, the pre-registered
client). From a terminal: `claude mcp add --transport http miragen-bridge
https://<daemon>/mcp --header "Authorization: Bearer <token>"`.

## Enable it on the development machine (local daemon)

Prerequisites: the memory stack from `~/.agents/memory.compose.yml`
(Loimi on `127.0.0.1:8400`), a minted principal token (`assistant`) and,
for automatic per-project scopes, the operator credential.

1. **Install miragen with the daemon on the host** (a venv or `uv tool`):

   ```bash
   uv tool install --from /home/ilari/Software/Repositories/miragen miragen   # or pipx / a venv
   which miragend miragen-hook
   ```

2. **Configure the daemon** — copy the three files from
   `scripts/miragend-local/` and edit paths:

   ```bash
   mkdir -p ~/.config/miragend ~/.config/systemd/user
   cp scripts/miragend-local/sessions.example.yaml ~/.config/miragend/sessions.yaml
   cp scripts/miragend-local/miragend.env.example  ~/.config/miragend/miragend.env
   cp scripts/miragend-local/miragend.service      ~/.config/systemd/user/
   chmod 600 ~/.config/miragend/miragend.env
   ```

   `sessions.yaml` names the principal and the scope policy. The default
   policy gives every session read access to `profile:assistant` plus one
   `group:project.<slug>` scope per repository (derived from the remote
   URL), created on first sight when `LOIMI_OPERATOR_TOKEN` is available.
   Explicit `projects:` bindings override the template (two repositories
   sharing one project scope, a project that may also read a shared
   platform scope). Set `recall.model` to enable prompt-time recall.

3. **Start the daemon** as a user service (no Docker needed —
   `MIRAGEND_LIFECYCLE=off` in the unit):

   ```bash
   systemctl --user daemon-reload && systemctl --user enable --now miragend
   curl -s http://127.0.0.1:8420/health | jq .sessions
   ```

4. **Claude Code: install the plugin** (above) or the hooks once:

   ```bash
   miragen-hook install claude-code --daemon http://127.0.0.1:8420
   ```

   **Codex and Grok Build need nothing**: set `harness_setup.url` (and
   `token_file` when the bridge is guarded) in `sessions.yaml` and the
   daemon writes and keeps their hooks, trust and MCP entries current (see
   "Codex and Grok Build: the daemon sets them up"). If `~/.codex` or
   `~/.grok` live elsewhere, put `CODEX_HOME` / `GROK_HOME` into the unit's
   environment.

   The local daemon is bound to loopback and runs unauthenticated by
   default (the same "rely on network isolation" mode the containerised
   daemon uses on `miragen-net`). To guard it anyway, set
   `MIRAGEND_TOKEN_FILE` in `miragend.env` and name the same file as
   `harness_setup.token_file` / `--token-file`.

5. **Use it**: `cd` into any repository and run `claude` or `codex`. Check
   participation with:

   ```bash
   curl -s "http://127.0.0.1:8420/sessions/v1/sessions?active=true" | jq
   ```

## Observability

`GET /health` (unguarded) carries `sessions`: active sessions by harness,
Loimi reachability (last ok / last error), recall configuration,
provisioned and unprovisionable scopes, pending background tasks, and the
counters: events received/rejected, sessions registered, retrievals and
failures, injections, prompt recalls, captures and failures, episodes,
checkpoints, timeouts, provisioning, journal replays, sweep finalizations,
mean retrieval and write latency. `GET /sessions/v1/stats` is the same
block guarded; `GET /sessions/v1/sessions[/{key}]` lists sessions.

With `MIRAGEN_OTLP_ENDPOINT` set, every event is one `session.event` span
under `service.name=miragend` with `mira.run.id` (the session key),
`mira.run.trigger` (the normalized event), `mira.session.harness`,
`mira.event.original`, `mira.project.id` and `mira.event.outcome` —
mechanical facts only, never prompt or memory content
(see [telemetry.md](telemetry.md)).

## Scope and permissions

A session's memory is scoped by the daemon, never by the session:

- the daemon authenticates as **one** principal (`principal:` in
  `sessions.yaml`) whose token it holds; nothing in a hook payload can
  name a scope, a principal or a credential;
- `read` = `scopes.shared_read` + the project's scope (+ a binding's extra
  reads); `write` = the project's scope. A Claude in repository A never
  reads repository B's project memory unless a binding says so;
- Loimi still enforces every grant (row-level security under the
  principal), so the daemon's policy can only narrow what the principal
  is allowed, never widen it;
- the daemon's own API is bound to loopback; a bearer token is optional
  there (`MIRAGEND_TOKEN`), as it is for the containerised daemon.

## What is deliberately not there yet

- No transcript reading at `PreCompact`: the episode is built from what
  the hooks carry (prompts, last assistant messages, failures, children).
- No model-authored summaries in the daemon; extraction into claims is
  the existing `miragen memory-worker`'s job (`session_episode` events are
  eligible, `harness:*` trail is not).
- Codex tool failures: Codex has no PostToolUseFailure event and its
  PostToolUse carries no exit code, so failed commands are not captured.
- Memory MCP tools for external sessions (`memory_remember` etc.) are not
  wired; the guide tells the model so. The `/mcp/memory` mount of a
  MiraGen agent remains the explicit interface.
