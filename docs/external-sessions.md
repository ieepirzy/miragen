# External sessions: Claude Code and Codex on the memory substrate

`miragend` can run on a developer machine as the **local memory-participation
daemon**: every ordinary `claude` or `codex` process joins the same memory
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

4. **Install the hooks once, globally**:

   ```bash
   miragen-hook install claude-code --daemon http://127.0.0.1:8420
   miragen-hook install codex       --daemon http://127.0.0.1:8420
   ```

   The local daemon is bound to loopback and runs unauthenticated by
   default (the same "rely on network isolation" mode the containerised
   daemon uses on `miragen-net`). To guard it anyway, set
   `MIRAGEND_TOKEN_FILE` in `miragend.env` and pass the same file to
   `miragen-hook install … --token-file <path>`.

   This merges owned entries into `~/.claude/settings.json` and
   `~/.codex/hooks.json` (user entries untouched; re-running refreshes;
   `--uninstall` removes). Codex's `hooks` feature is stable in
   codex-cli 0.153 — no flag needed.

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
- Codex was not live-probed on 2026-09-15 (usage limit); its field
  spellings follow the reference verified for PR #88 and the normalizer
  accepts every documented variant.
- Memory MCP tools for external sessions (`memory_remember` etc.) are not
  wired; the guide tells the model so. The `/mcp/memory` mount of a
  MiraGen agent remains the explicit interface.

### Exact resource recall

The bridge exposes `memory_for_resources` for explicitly named Python files and
qualified symbols in an observed local session. Pass that session's key as
`project`. Remote/default/guessed checkouts return unverified. Existing hooks do
not supply a trustworthy symbol stream, so source identities are not inferred
from prompts or synthesized into hook events. See
[source-grounded memory](source-grounded-memory.md) for examples and limitations.
`injections` counters mean prepared contexts; delivery remains unconfirmed.
