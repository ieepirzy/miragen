# miragen-memory (Claude Code, Codex and Grok Build plugin)

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
repository package) is **stdlib-only Python ≥ 3.10** (the Codex setup
alone needs 3.11); nothing is installed on the machine. Every hook fails open within its t## Codex and Grok Build: the local daemon sets them up

Requires Grok Build ≥ 1.0.41 (older builds pass stdio MCP servers no
`GROK_SESSION_ID` / `GROK_PLUGIN_ROOT`) and codex-cli ≥ 0.156.

On a machine that runs AI harnesses, the local `miragend` writes the Codex
and Grok Build setup itself — no per-machine step, no trust prompt — once
its `sessions.yaml` names where sessions report:

```yaml
harness_setup:
  url: https://memory.muutto365.fi
  token_file: ~/.config/miragend/bridge.token   # 0600
```

- **Grok Build**: `$GROK_HOME/hooks/miragen.json` (every lifecycle event,
  harness `grok-build`). Grok 1.0.41 builds a session's hooks from hook
  *files* only (plugin hooks load only after `/reload-plugins`), so the Grok
  manifest points at an explicitly empty hooks file and the two never both
  fire.
- **Codex**: native entries in `$CODEX_HOME/hooks.json` (harness `codex`),
  their `trusted_hash` in `config.toml` (`codex exec` silently skips
  untrusted hooks), and `[mcp_servers.miragen-bridge]` — which replaces a
  hand-made entry of that name, e.g. one from `codex mcp add`.

Details, the exact files and `/health` status:
[docs/external-sessions.md](../../docs/external-sessions.md). By hand (a
machine without a daemon, or debugging):

```shell
PYTHONPATH=<plugin dir> python3 -m miragen_hook setup grok-build --daemon URL [--token-file F]
PYTHONPATH=<plugin dir> python3 -m miragen_hook setup codex      --daemon URL [--token-file F]
# --remove undoes it
```

**MCP tools under Codex and Grok** run as a stdio proxy from this plugin
(`miragen_hook/__main__.py mcp-proxy`), so they reach the same daemon with
the same token as the hooks: `--daemon`/`--token-file` → the daemon's
`<home>/miragen-adapter/setup.json` → `MIRAGEND_URL` / `MIRAGEND_TOKEN` →
the `daemon_url` saved for this plugin in Claude Code → the manifest
default. Under Grok the proxy binds the connection to the session
(`X-Harness-Session: grok-build:<id>`; Grok never shows the model the session
header, so omitted `project`/`session` arguments mean this session), and on
a machine without a daemon it writes the Grok hook file itself — hooks then
start with the *next* session. Codex installs the plugin with `codex plugin
marketplace add ieepirzy/miragen && codex plugin add miragen-memory@miragen`;
its `.codex-plugin/plugin.json` declares an empty hooks file (the daemon's
native hooks are the Codex path) and the proxy.

Grok **discards** SessionStart/UserPromptSubmit hook output. The start block
and prompt recall are queued (0600, under `~/.local/state/miragen-hook/`) and
handed to the model with the **first tool result** that follows, exactly
once. A turn that uses no tool gets none: its prompt context waits for the
next tool result and is replaced by the next prompt's, a new start replaces
everything, and anything older than 12 h is dropped — the MCP tools are the
fallback. Codex shows SessionStart/UserPromptSubmit context directly; it is
capped (in bytes) under Codex's spill limit. Codex reports no tool failures
to hooks, so none are captured there.

ION_*`.

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
