#!/usr/bin/env bash
# miragen memory bridge — one-shot bootstrap for an environment you do not
# otherwise control (Claude Code cloud environments, CI runners, a fresh box).
#
#   curl -fsSL https://raw.githubusercontent.com/ieepirzy/miragen/main/scripts/cloud-bootstrap.sh | bash
#
# What it does, idempotently, for the invoking user's ~/.claude:
#   1. registers the `miragen` plugin marketplace and installs the
#      `miragen-memory` plugin (hooks + MCP + skill) — WITHOUT the token
#      option: the hooks read MIRAGEND_TOKEN from the environment, so no
#      secret ever sits on a command line or in settings;
#   2. writes Claude Code `type: http` hooks into ~/.claude/settings.json as
#      the zero-dependency path (the harness POSTs raw payloads to the daemon
#      with `Authorization: Bearer $MIRAGEND_TOKEN`), merging with whatever is
#      there and refreshing only entries it owns.
# Either path alone is enough; the daemon drops raw hooks for a session it
# already knows through the plugin adapter, so running both never injects
# context twice. Never fails the caller (exit 0 always): a memory bridge
# that cannot be reached must not stop a session from starting.
#
# Configuration (environment):
#   MIRAGEND_URL      daemon base URL     (default https://memory.muutto365.fi)
#   MIRAGEND_TOKEN    bridge bearer       (read by the hooks at run time;
#                                          not needed to run this script)
#   MIRAGEN_BOOTSTRAP_NO_PLUGIN=1   skip the plugin, hooks only
#   MIRAGEN_BOOTSTRAP_NO_HOOKS=1    skip the hooks, plugin only
#   CLAUDE_BIN        path to the claude CLI if not on PATH

set -u
DAEMON="${MIRAGEND_URL:-https://memory.muutto365.fi}"
DAEMON="${DAEMON%/}"
SETTINGS="${CLAUDE_SETTINGS_PATH:-$HOME/.claude/settings.json}"
log() { printf 'miragen-bootstrap: %s\n' "$*"; }

# ── 1. plugin (marketplace + install) ───────────────────────────────────────
if [ -z "${MIRAGEN_BOOTSTRAP_NO_PLUGIN:-}" ]; then
    CLAUDE="${CLAUDE_BIN:-}"
    [ -z "$CLAUDE" ] && CLAUDE="$(command -v claude 2>/dev/null || true)"
    if [ -z "$CLAUDE" ]; then
        for candidate in /opt/node22/bin/claude /opt/node20/bin/claude /usr/local/bin/claude \
                         "$HOME/.local/bin/claude" "$HOME/.npm-global/bin/claude"; do
            [ -x "$candidate" ] && CLAUDE="$candidate" && break
        done
    fi
    if [ -n "$CLAUDE" ]; then
        log "claude at $CLAUDE ($("$CLAUDE" --version 2>/dev/null | head -1))"
        "$CLAUDE" plugin marketplace add ieepirzy/miragen --scope user 2>&1 | tail -1 | sed 's/^/miragen-bootstrap:   /' || true
        "$CLAUDE" plugin install miragen-memory@miragen --scope user \
            --config "daemon_url=${DAEMON}" 2>&1 | tail -1 | sed 's/^/miragen-bootstrap:   /' || true
        "$CLAUDE" plugin list 2>&1 | grep -i miragen | sed 's/^/miragen-bootstrap:   /' || log "plugin not listed (hooks below still apply)"
    else
        log "claude CLI not found; skipping the plugin (hooks below still apply)"
    fi
fi

# ── 2. HTTP hooks in user settings (stdlib python3, merge, own-only refresh) ─
if [ -z "${MIRAGEN_BOOTSTRAP_NO_HOOKS:-}" ]; then
    if command -v python3 >/dev/null 2>&1; then
        SETTINGS="$SETTINGS" DAEMON="$DAEMON" python3 - <<'PY' || log "could not write hooks"
import json, os
from pathlib import Path

path = Path(os.environ["SETTINGS"]); daemon = os.environ["DAEMON"]
marker = "/sessions/v1/hooks/"
events = [("SessionStart", 15), ("UserPromptSubmit", 15), ("PostToolUseFailure", 5),
          ("PreCompact", 5), ("PostCompact", 5), ("Stop", 5), ("SubagentStart", 5),
          ("SubagentStop", 5), ("SessionEnd", 3)]
try:
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("settings is not an object")
except ValueError as exc:
    raise SystemExit(f"miragen-bootstrap: {path} is not valid JSON ({exc}); left untouched")
hooks = data.setdefault("hooks", {})
def owned(group):
    return any(marker in str(h.get("url", "")) for h in group.get("hooks", []) if isinstance(h, dict))
for event, timeout in events:
    kept = [g for g in hooks.get(event, []) if not owned(g)]
    kept.append({"hooks": [{
        "type": "http",
        "url": f"{daemon}{marker}claude-code",
        "headers": {"Authorization": "Bearer $MIRAGEND_TOKEN"},
        "allowedEnvVars": ["MIRAGEND_TOKEN"],
        "timeout": timeout,
    }]})
    hooks[event] = kept
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2) + "\n")
print(f"miragen-bootstrap: HTTP hooks for {len(events)} events written to {path}")
PY
    else
        log "python3 not found; skipping hooks"
    fi
fi

if [ -z "${MIRAGEND_TOKEN:-}" ]; then
    log "note: MIRAGEND_TOKEN is not set in this shell; the hooks read it from the session environment at run time"
fi
exit 0
