"""Install/remove miragen's owned hook entries in a harness configuration.

Merge discipline (§18.7): only entries carrying our command marker are
owned — removed and re-added on every install — and every user entry is
preserved verbatim. A configuration file we cannot parse is the user's:
refusing to touch it beats silently rewriting it.

Claude Code: `~/.claude/settings.json` → `hooks.<Event>[]` groups (each
`{"matcher"?, "hooks": [{"type": "command", "command", "timeout"}]}`).
Codex: `~/.codex/hooks.json`, the same group shape; the `hooks` feature is
stable in codex-cli 0.153 (`codex features list`).
"""

from __future__ import annotations

import json
from pathlib import Path

OWNED_MARKERS = ("miragen-hook", "miragen memory-hook")

# (event, timeout_s). Timeouts sit above the adapter's own bounds
# (client.py) so the adapter, not the harness, is what gives up first.
# SessionEnd: Claude Code shares a 1.5 s budget across SessionEnd hooks
# and raises it to the longest declared timeout; Codex caps it at 3 s.
CLAUDE_CODE_EVENTS = (
    ("SessionStart", 15),
    ("UserPromptSubmit", 15),
    ("PostToolUseFailure", 5),
    ("PreCompact", 5),
    ("PostCompact", 5),
    ("Stop", 5),
    ("SubagentStart", 5),
    ("SubagentStop", 5),
    ("SessionEnd", 3),
)
CODEX_EVENTS = (
    ("SessionStart", 15),
    ("UserPromptSubmit", 15),
    ("PreCompact", 5),
    ("PostCompact", 5),
    ("Stop", 5),
    ("SubagentStop", 5),
    ("SessionEnd", 3),
)
# The in-container bridge (`miragen memory-hook codex`, executor tier) does
# the Loimi write inside the hook process, bounded at 8 s — its entries get
# room for that. The forwarding adapter only hands off to the daemon.
CODEX_BRIDGE_EVENTS = tuple(
    (event, 3 if event == "SessionEnd" else 10) for event, _ in CODEX_EVENTS
)


def default_settings_path(harness: str) -> Path:
    if harness == "claude-code":
        return Path.home() / ".claude" / "settings.json"
    if harness == "codex":
        return Path.home() / ".codex" / "hooks.json"
    raise ValueError(f"unknown harness '{harness}'")


def hook_command(harness: str, *, daemon_url: str | None, token_file: str | None) -> str:
    parts = ["miragen-hook", harness]
    if daemon_url:
        parts += ["--daemon", daemon_url]
    if token_file:
        parts += ["--token-file", token_file]
    return " ".join(parts)


def _read_config(path: Path) -> dict:
    try:
        existing = json.loads(path.read_text())
        return existing if isinstance(existing, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError:
        raise RuntimeError(
            f"{path} exists but is not valid JSON — fix or remove it "
            "before miragen can install its memory hooks"
        ) from None


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _group_is_owned(group: dict) -> bool:
    return any(
        any(marker in str(hook.get("command", "")) for marker in OWNED_MARKERS)
        for hook in group.get("hooks", [])
        if isinstance(hook, dict)
    )


def install_hook_entries(
    path: Path, command: str, events: tuple[tuple[str, int], ...],
    *, status_message: str = "miragen memory",
) -> Path:
    existing = _read_config(path)
    hooks = existing.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"{path}: 'hooks' is not an object")
    for event_name, timeout in events:
        groups = [g for g in hooks.get(event_name, []) if not _group_is_owned(g)]
        groups.append({
            "hooks": [{
                "type": "command",
                "command": command,
                "timeout": timeout,
                "statusMessage": status_message,
            }]
        })
        hooks[event_name] = groups
    _write_config(path, existing)
    return path


def remove_hook_entries(path: Path) -> Path:
    existing = _read_config(path)
    hooks = existing.get("hooks")
    if isinstance(hooks, dict):
        for event_name in list(hooks):
            kept = [g for g in hooks[event_name] if not _group_is_owned(g)]
            if kept:
                hooks[event_name] = kept
            else:
                del hooks[event_name]
        if not hooks:
            del existing["hooks"]
        _write_config(path, existing)
    return path


def install_hooks(
    harness: str, *, daemon_url: str | None, token_file: str | None,
    settings_path: Path | None = None,
) -> Path:
    path = settings_path or default_settings_path(harness)
    command = hook_command(harness, daemon_url=daemon_url, token_file=token_file)
    events = CLAUDE_CODE_EVENTS if harness == "claude-code" else CODEX_EVENTS
    return install_hook_entries(path, command, events)


def uninstall_hooks(harness: str, *, settings_path: Path | None = None) -> Path:
    return remove_hook_entries(settings_path or default_settings_path(harness))


def install_codex_hooks(hooks_path: Path, command: str = "miragen memory-hook codex") -> None:
    """The in-container Codex path (executor tier): entries point at the
    in-process bridge by default. Kept for the codex executor adapter."""
    install_hook_entries(hooks_path, command, CODEX_BRIDGE_EVENTS)
