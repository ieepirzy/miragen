"""Install/remove miragen's owned hook entries in a harness configuration.

Merge discipline (§18.7): only entries carrying our command marker are
owned — removed and re-added on every install — and every user entry is
preserved verbatim. A configuration file we cannot parse is the user's:
refusing to touch it beats silently rewriting it.

Claude Code: `~/.claude/settings.json` → `hooks.<Event>[]` groups (each
`{"matcher"?, "hooks": [{"type": "command", "command", "timeout"}]}`).
Codex: `~/.codex/hooks.json`, the same group shape; the `hooks` feature is
stable in codex-cli 0.153 (`codex features list`).
Grok Build: `~/.grok/hooks/miragen.json`, the same group shape (a file of
its own — Grok reads every `*.json` there). This is THE Grok hook path even
when the plugin is enabled: Grok 1.0.41 builds a session's hooks from hook
files only and merges plugin hooks only after a plugin reload, so the
plugin's Grok manifest declares no hooks (the MCP server does load from it).
The entry runs this stdlib adapter from where it is installed now
(`PYTHONPATH=<dir> python3 -m miragen_hook`), so no console script is needed.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

OWNED_MARKERS = ("miragen-hook", "miragen memory-hook")
# The Grok entry (`python3 …/miragen_hook/__main__.py`, formerly
# `python3 -m miragen_hook`), not `-m miragen_hook_wrapper`.
_OWNED_MODULE = re.compile(r"(-m miragen_hook|miragen_hook/__main__\.py'?)(\s|$)")
OWNED_URL_MARKER = "/sessions/v1/hooks/"

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
# Grok Build: the Claude set plus PostToolUse, the first point after
# SessionStart/UserPromptSubmit where Grok passes additionalContext to the
# model (client.py delivers the queued context there). Grok's own defaults
# are 5 s for observe events and 1.5 s for SessionEnd.
GROK_BUILD_EVENTS = (
    ("SessionStart", 15),
    ("UserPromptSubmit", 15),
    ("PostToolUse", 5),
    ("PostToolUseFailure", 5),
    ("PreCompact", 5),
    ("PostCompact", 5),
    ("Stop", 5),
    ("SubagentStart", 5),
    ("SubagentStop", 5),
    ("SessionEnd", 2),
)
EVENTS_BY_HARNESS = {
    "claude-code": CLAUDE_CODE_EVENTS,
    "codex": CODEX_EVENTS,
    "grok-build": GROK_BUILD_EVENTS,
}
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
    if harness == "grok-build":
        return Path.home() / ".grok" / "hooks" / "miragen.json"
    raise ValueError(f"unknown harness '{harness}'")


def adapter_root() -> Path:
    """The directory that holds this `miragen_hook` package (the plugin root
    for the vendored copy, the repository for a checkout)."""
    return Path(__file__).resolve().parent.parent


def hook_command(harness: str, *, daemon_url: str | None, token_file: str | None) -> str:
    if harness == "grok-build":
        # Grok scans hook commands for `$VAR` references WITHOUT regard to
        # quoting (and refuses to run one whose variable is unset), so a `$`
        # in a baked value would silently disable the hook.
        for value in (str(adapter_root()), daemon_url, token_file):
            if value and "$" in value:
                raise RuntimeError(
                    f"'{value}' contains '$': Grok would read it as a variable and "
                    "not run the hook — use a path/URL without '$'"
                )
        # Grok runs shell-form commands through `sh -c` with the repository
        # as working directory: quote every part, and run the adapter by FILE
        # — `python3 -m` puts the working directory ahead of PYTHONPATH, so
        # inside a miragen checkout it would import the checkout's copy (no
        # manifest → loopback URL → every event silently lost).
        root = adapter_root()
        parts = [f"PYTHONPATH={shlex.quote(str(root))}", "python3",
                 shlex.quote(str(root / "miragen_hook" / "__main__.py")), harness]
    else:
        parts = ["miragen-hook", harness]
    if daemon_url:
        parts += ["--daemon", shlex.quote(daemon_url)]
    if token_file:
        parts += ["--token-file", shlex.quote(token_file)]
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
    for hook in group.get("hooks", []):
        if not isinstance(hook, dict):
            continue
        command = str(hook.get("command", ""))
        if any(marker in command for marker in OWNED_MARKERS) or _OWNED_MODULE.search(command):
            return True
        if OWNED_URL_MARKER in str(hook.get("url", "")):
            return True
    return False


def http_hook_entry(harness: str, *, daemon_url: str, timeout: int) -> dict:
    """An HTTP hook: the harness POSTs its raw payload to the daemon, which
    normalizes it server-side (routes.py). The bearer is interpolated from
    the environment WHERE THE HARNESS RUNS — Claude Code resolves only the
    variables named in allowedEnvVars — so a cloud VM needs MIRAGEND_TOKEN
    in its environment and nothing installed."""
    return {
        "type": "http",
        "url": f"{daemon_url.rstrip('/')}{OWNED_URL_MARKER}{harness}",
        "headers": {"Authorization": "Bearer $MIRAGEND_TOKEN"},
        "allowedEnvVars": ["MIRAGEND_TOKEN"],
        "timeout": timeout,
    }


def install_hook_entries(
    path: Path, command: str | None, events: tuple[tuple[str, int], ...],
    *, status_message: str = "miragen memory", http_daemon_url: str | None = None,
    harness: str | None = None,
) -> Path:
    existing = _read_config(path)
    hooks = existing.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"{path}: 'hooks' is not an object")
    for event_name, timeout in events:
        groups = [g for g in hooks.get(event_name, []) if not _group_is_owned(g)]
        if http_daemon_url:
            entry = http_hook_entry(harness or "claude-code", daemon_url=http_daemon_url,
                                    timeout=timeout)
        else:
            entry = {
                "type": "command",
                "command": command,
                "timeout": timeout,
                "statusMessage": status_message,
            }
        groups.append({"hooks": [entry]})
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
    settings_path: Path | None = None, http: bool = False,
) -> Path:
    path = settings_path or default_settings_path(harness)
    events = EVENTS_BY_HARNESS[harness]
    if http:
        if harness != "claude-code":
            # Grok Build has http hooks but no `headers` field, so it could
            # never carry the bearer a hosted daemon requires.
            raise RuntimeError("HTTP hooks are a Claude Code feature; Codex and Grok Build use the adapter")
        if not daemon_url:
            raise RuntimeError("--http needs the daemon URL (--daemon or MIRAGEND_URL)")
        return install_hook_entries(
            path, None, events, http_daemon_url=daemon_url, harness=harness,
        )
    command = hook_command(harness, daemon_url=daemon_url, token_file=token_file)
    return install_hook_entries(path, command, events)


def uninstall_hooks(harness: str, *, settings_path: Path | None = None) -> Path:
    return remove_hook_entries(settings_path or default_settings_path(harness))


def install_codex_hooks(hooks_path: Path, command: str = "miragen memory-hook codex") -> None:
    """The in-container Codex path (executor tier): entries point at the
    in-process bridge by default. Kept for the codex executor adapter."""
    install_hook_entries(hooks_path, command, CODEX_BRIDGE_EVENTS)
