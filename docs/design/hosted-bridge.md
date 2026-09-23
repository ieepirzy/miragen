# The hosted bridge: one miragend for every harness, everywhere

**Status:** implemented (`miragen/daemon/sessions/`, `miragen_hook/`,
`plugins/miragen-memory/`, `docs/external-sessions.md`).
**Extends:** [external-sessions.md](external-sessions.md) (the session plane),
memory pass §17.3 / §18.7 / §18.8.

## 1. The problem

The session plane made a `claude` or `codex` process on the developer machine
a memory participant through a **local** miragend. Two facts made that
insufficient the moment it worked:

- Most agentic work does not happen on that machine. Claude Code cloud
  sessions run in Anthropic-managed VMs that read nothing from
  `~/.claude/`, cannot run a daemon we control between sessions, and reach
  the network only through an allowlist.
- Loimi was designed as the organisation's artifact store — runs, immutable
  artifacts, one-hop provenance, lineage — and nothing wrote to it, because
  the sessions doing the work had no path to it.

## 2. The invariant, unchanged

**Loimi stores memory and artifacts. miragend manages participation. Every
harness is a client.** What changes is *where* miragend runs (next to the
production Loimi, reachable over HTTPS) and *how* a client gets there
(plugin, repository hooks, or an MCP connector) — not who decides scopes,
principals, runs or namespaces. A hook payload or a tool argument may
**name a project**; the daemon still resolves it to a scope and a run.

## 3. Shape

```text
 laptop: claude/codex ─plugin hooks (stdlib adapter)──┐
 cloud VM: claude ────plugin hooks | http hooks ───────┤   HTTPS, bearer
 claude.ai chat ──────MCP connector (origo OAuth) ─────┤
 claude CLI ──────────claude mcp add --header ─────────┤
                                                       ▼
                        miragend (session plane, hosted next to Loimi)
                          POST /sessions/v1/events   ← adapter envelopes
                          POST /sessions/v1/hooks/*  ← raw harness payloads
                          /mcp                       ← memory_* + store_* tools
                          │ principal token (self-minted)   │ store bearer
                          ▼                                  ▼
                        Loimi /memory/v1                  Loimi /v0
```

## 4. Decisions

**Hosted, not per-environment.** Spinning a miragend up inside each cloud VM
would cost a `pydantic-ai` install per session, put a Loimi principal
token *and* the operator token into every sandbox, and leave no registry
of sessions anywhere. One hosted daemon holds the credentials, keeps the
session registry, and every surface joins the same substrate. The hook
adapter stays stdlib-only so the client side is nothing to install.

**Identity travels with the event.** A hosted daemon cannot `git remote
get-url` in a cloud VM. The adapter therefore reports `client.host`,
`client.remote` (Claude Code's `CLAUDE_CODE_REMOTE`) and
`client.project_remote` (the origin URL, bounded 1 s, best-effort). The
daemon prefers that remote, inspects the directory itself only for its own
host, and otherwise falls back to the directory **name** (`dir:<basename>`)
— adopting a project it already knows by that repository name
(`scopes.adopt_by_name`, default on; a shared daemon with unrelated
same-named repositories turns it off). Raw HTTP hooks carry only a path, so
they always take that last route; the plugin path is preferred.

**Liveness is per host.** A pid from another machine is meaningless. The
sweep checks a pid only for sessions on the daemon's own host and not
declared remote; everything else ends by silence (`stale_after_minutes`).
A cloud VM reclaimed mid-session still gets its episode.

**One secret to deploy.** With `provision_principal: true` (default) and the
operator token in the environment, the daemon creates its principal (or
mints a token for an existing one) at startup and persists it in the state
dir; the shared read layers are created and granted the same way. A hosted
deployment configures the store bearer — which is the operator credential —
and nothing else memory-specific. An operator-minted token in the credential
env still wins.

**The artifact store is part of the bridge.** Every session gets a Loimi
run (`store.agent_id`, namespace from `store.namespaces` bindings or the
default) opened at its first context and closed at its end (`succeeded`;
`cancelled` when the sweep ended it). The compaction and end **episodes are
filed as `session_episode` artifacts** under that run, with the memory
event id in their properties — a session's trail is searchable and
traceable in the store. The injected header carries `store_run=…` so the
model can file its own artifacts against the same run with the tools.

**The MCP surface is the daemon's, scoped by name.** `/mcp` serves
`memory_recall / memory_read / memory_remember / memory_correct /
memory_checkpoint` (scoped by `project`) and `store_put_artifact /
store_search / store_get_artifact / store_lineage / store_open_run /
store_close_run / store_run_tree / store_list_namespaces` (against the
session's run or an explicit one), plus `bridge_status / bridge_sessions`.
Two credential classes open it: the daemon bearer (automation, the plugin's
`.mcp.json`, `claude mcp add --header`) and an origo OAuth token
(claude.ai custom connector; `MCP_BASE_URL` / `MCP_CLIENT_ID` /
`MCP_CLIENT_SECRET`, pre-registered private client, `MCP_AUTO_APPROVE`).
The guide advertises the tools (`tools_available`) exactly when the mount is
enabled. `memory_recall` is the explicit lexical search the guide promises
("an absent packet does not mean the store is empty").

**Distribution as a plugin.** `plugins/miragen-memory` carries the hooks
(shell form, `PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m miragen_hook
claude-code`), the vendored adapter (`tests/test_plugin_bundle.py` pins it
identical to `miragen_hook/`), the `miragen-bridge` MCP server config with
`${user_config.*}` substitution, and the `memory-bridge` skill (§18.8's
detailed guidance). Options arrive at hooks as `CLAUDE_PLUGIN_OPTION_*`,
with `MIRAGEND_URL` / `MIRAGEND_TOKEN` as the environment fallback — the
form a cloud environment uses. The marketplace is the repository itself
(`.claude-plugin/marketplace.json`). Plugins declared in a repository's
`.claude/settings.json`, or enabled for the claude.ai account, reach cloud
sessions; user-level settings never do.

**Raw HTTP hooks as the zero-install path.** Claude Code `type: http` hooks
POST the payload with a bearer interpolated from `allowedEnvVars`.
`POST /sessions/v1/hooks/{harness}` normalizes server-side and answers in
the harness's output shape; `miragen-hook install claude-code --http
--settings .claude/settings.json` writes the entries for a repository.

## 5. Deliberately not done

- No transcript reading; episodes are still built from what hooks carry.
- No per-client scope authority: all clients of one daemon share its
  principal and its policy. Multi-tenant bridges are a different design.
- Codex has no HTTP hooks; it uses the adapter (plugin or `install codex`).
- The extraction worker and a recall selector model are deployment
  choices, not part of this record (the selector backends —
  `claude-code:<model>` on the subscription, or a pydantic-ai model with an
  optional `base_url` — are documented in
  [../external-sessions.md](../external-sessions.md)).
- **No secret redaction of captured prompts (decided 2026-09-16).** Prompts
  and turn ends are captured verbatim; a token pasted into a prompt lands in
  the project's memory scope. Pattern lists for secrets are brittle and
  fail open, so the decision is to keep captures faithful. If redaction is
  ever added, it should be an entropy-based detector on whitespace-delimited
  tokens (high Shannon entropy over a minimum length), applied at the adapter
  before the envelope leaves the harness host, with the redaction counted on
  `/health` — never a denylist of known prefixes. Erasure exists today
  through Loimi's operator surface (`POST /memory/v1/events/{id}/erase`).
