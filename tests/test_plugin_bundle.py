"""The Claude Code plugin (plugins/miragen-memory) and its marketplace: the
vendored adapter is byte-identical to miragen_hook/, the hook entries match
the installer's event table, the MCP config points at the daemon, and the
manifests parse."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from miragen_hook.install import CLAUDE_CODE_EVENTS

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "miragen-memory"


def _py_files(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.glob("*.py"))}


def test_vendored_adapter_is_in_sync():
    assert _py_files(ROOT / "miragen_hook") == _py_files(PLUGIN / "miragen_hook"), (
        "plugins/miragen-memory/miragen_hook drifted from miragen_hook/ — run "
        "scripts/sync_plugin_adapter.sh"
    )


def test_vendored_adapter_runs_standalone(tmp_path):
    """Exactly what the plugin hook does: PYTHONPATH=<plugin root>, stdlib
    python, garbage/unknown payload → exit 0, nothing on stdout."""
    result = subprocess.run(
        [sys.executable, "-m", "miragen_hook", "claude-code"],
        input=json.dumps({"hook_event_name": "PostToolUse", "session_id": "x", "tool_name": "Bash"}),
        capture_output=True, text=True, timeout=20,
        env={"PYTHONPATH": str(PLUGIN), "PATH": "/usr/bin:/bin", "MIRAGEND_URL": "http://127.0.0.1:1"},
    )
    assert (result.returncode, result.stdout) == (0, "")


def test_hooks_json_matches_installer_table():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(hooks) == {event for event, _ in CLAUDE_CODE_EVENTS}
    for event, timeout in CLAUDE_CODE_EVENTS:
        groups = hooks[event]
        assert len(groups) == 1 and len(groups[0]["hooks"]) == 1
        entry = groups[0]["hooks"][0]
        assert entry["type"] == "command" and entry["timeout"] == timeout
        assert entry["command"] == 'PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m miragen_hook claude-code'


def test_manifests_and_mcp_config():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "miragen-memory"
    assert manifest["userConfig"]["token"]["sensitive"] is True
    assert set(manifest["userConfig"]) == {"daemon_url", "token"}
    mcp = json.loads((PLUGIN / ".mcp.json").read_text())["miragen-bridge"]
    assert mcp == {"type": "http", "url": "${user_config.daemon_url}/mcp",
                   "headers": {"Authorization": "Bearer ${user_config.token}"}}
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert marketplace["name"] == "miragen"
    [entry] = marketplace["plugins"]
    assert entry["name"] == "miragen-memory" and entry["source"] == "./plugins/miragen-memory"
    assert (PLUGIN / "skills" / "memory-bridge" / "SKILL.md").read_text().startswith("---\ndescription:")
