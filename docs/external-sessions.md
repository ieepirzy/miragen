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

## Enable it on the development machine

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
