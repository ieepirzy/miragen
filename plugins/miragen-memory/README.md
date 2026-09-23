# miragen-memory (Claude Code + Grok Build plugin)

Joins every Claude Code session — on your machine **and in cloud sessions** —
to the miragen memory bridge: a hosted `miragend` in front of Loimi's memory
(`/memory/v1`) and artifact store (`/v0`).

What you get, automatically, per session:

| When | What |
|---|---|
| session start / resume / after compaction | the project's working state + the memory guide injected as context; a Loimi run opened for the session |
| each prompt | captured (idempotently); selective recall injected when the daemon has a selector model |
| tool failures, turn ends, subagents | captured as operational trail |
| compaction, session end | a session episode written to memory AND filed as a `session_episode` artifact under the session's run; the run closed at end |

Plus the `miragen-bridge` MCP server: `memory_recall / memory_read /
memory_remember / memory_correct / memory_checkpoint` and `store_put_artifact /
store_search / store_get_artifact / store_lineage / store_open_run /
store_close_run / store_run_tree / store_list_namespaces`, and
`bridge_status / bridge_sessions`. The skill `memory-bridge` tells the model
when to use them.

## Install

```shell
/plugin marketplace add ieepirzy/miragen
/plugin install miragen-memory@miragen
```

You are asked for two options: the daemon URL and its bearer token
(`MIRAGEND_TOKEN`; stored in the credential store, not in settings.json).
The hooks read them as `CLAUDE_PLUGIN_OPTION_DAEMON_URL` /
`CLAUDE_PLUGIN_OPTION_TOKEN`, falling back to `MIRAGEND_URL` / `MIRAGEND_TOKEN`
in the environment — which is how a cloud environment supplies them when
plugin options are not available there.

The hook adapter (`miragen_hook/` in this plugin, a vendored copy of the
repository package) is **stdlib-only Python 3**; nothing is installed on the
machine. Every hook fails open within its timeout.

## Grok Build

Grok Build (xAI's `grok`, ≥ 1.0) loads Claude Code plugins itself: once this
plugin is enabled in `~/.claude/settings.json`, `grok` reads its
`.grok-plugin/plugin.json` (Claude Code ignores that directory) and starts the
bridge MCP server from `grok.mcp.json`:
`${MIRAGEND_URL:-https://memory.muutto365.fi}/mcp`, the bearer from
`MIRAGEND_TOKEN` (`bearer_token_env_var`: sent only when set) and
`X-Harness-Session: grok-build:{{session_id}}`. Grok never shows the model the
session header, so the bridge binds omitted `project`/`session` arguments to
the session named on the connection (only a session it already knows).

**Hooks need one install step.** Grok 1.0.41 builds a session's hooks from
hook *files* only and merges plugin hooks only after a plugin reload
(`/reload-plugins`) — so plugin hooks never see SessionStart. Install the
lifecycle hooks as a file instead, from wherever the plugin lives:

```shell
PYTHONPATH=<plugin dir> python3 -m miragen_hook install grok-build [--daemon URL]
# writes ~/.grok/hooks/miragen.json; re-run to refresh, --uninstall to remove
```

The Grok manifest points at an explicitly empty hooks file, so the two never
both fire. Point it at a stable directory (a directory-source marketplace
clone), not a versioned plugin-cache path that an update deletes.

Grok **discards** SessionStart/UserPromptSubmit hook output. The start block
and prompt recall are queued (0600, under `~/.local/state/miragen-hook/`) and
handed to the model with the **first tool result** that follows, exactly
once. A turn that uses no tool gets none: its prompt context waits for the
next tool result and is replaced by the next prompt's, a new start replaces
everything, and anything older than 12 h is dropped — the MCP tools are the
fallback.

Grok has **no plugin options**: nothing reaches it from `/plugin install
--config`. Installed from the plugin dir without `--daemon` or `MIRAGEND_URL` (either
is written into the entry), the hooks resolve the daemon per event:
`MIRAGEND_URL` → the `daemon_url` you saved for this plugin in Claude Code →
the manifest default. The bearer comes only from the environment — export
`MIRAGEND_TOKEN` (and `MIRAGEND_URL` for a non-default bridge; the MCP config
reads only the environment) in the shell that starts `grok`; Claude Code's
`settings.json` `env` does not reach Grok. Without the token Grok tries the
bridge's OAuth flow, which the hosted bridge's private client registration
refuses — so export it.

Codex benefits from the same resolution order: it also loads the plugin
without exporting `CLAUDE_PLUGIN_OPTION_*`.

## Cloud sessions

Enable the plugin for your claude.ai account (synced plugins) or declare it in
the repository's `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "miragen": { "source": { "source": "github", "repo": "ieepirzy/miragen" } }
  },
  "enabledPlugins": { "miragen-memory@miragen": true }
}
```

The cloud environment must be allowed to reach the daemon's host (network
access **Custom** with the host listed, or **Full**) and should carry
`MIRAGEND_URL` and `MIRAGEND_TOKEN` as environment variables.

Found live (2026-09-16): plugins enabled for a claude.ai account did **not**
sync into Anthropic-hosted VMs, and a repository's `enabledPlugins` did not
install there either. What does work is the environment's **setup script**
installing the plugin with the CLI's absolute path and **no token argument**
(the hooks read `MIRAGEND_TOKEN` from the environment; the `token` option is
optional for exactly this reason):

```bash
CLAUDE="$(command -v claude || echo /opt/node22/bin/claude)"
"$CLAUDE" plugin marketplace add ieepirzy/miragen --scope user || true
"$CLAUDE" plugin install miragen-memory@miragen --scope user \
  --config daemon_url=https://memory.muutto365.fi || true
```

The plugin's bundled MCP server then has no bearer in cloud sessions and shows
as failed; enable the bridge as a claude.ai connector on the session for tools.
Repository HTTP hooks (below) work without any plugin.

Alternative without any plugin: `miragen-hook install claude-code --http
--daemon https://… --settings .claude/settings.json` writes `type: http` hooks
into a repository's settings — the harness POSTs raw payloads to
`/sessions/v1/hooks/claude-code` and the daemon normalizes them. That path
needs `MIRAGEND_TOKEN` in the session environment and identifies the project
by directory name (the daemon adopts a project it already knows by that name).

## Keep the vendored adapter in sync

`scripts/sync_plugin_adapter.sh` copies `miragen_hook/` here;
`tests/test_plugin_bundle.py` fails when the two drift.
