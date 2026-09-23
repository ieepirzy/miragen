"""Harness setup the daemon owns: Grok Build and Codex hooks + MCP, written
into the harness's own home so no human step (and no trust prompt) is ever
needed on a machine that runs miragend.

`ensure_grok` / `ensure_codex` are idempotent and cheap when nothing changed
(a pure read: the daemon calls them at startup and every few minutes), and
no-ops when that harness has no home on this machine — they never create
one. Every write is atomic and touches only what this module owns:

- an adapter copy under `<home>/miragen-adapter/<digest>/miragen_hook/`
  (stable across plugin updates; superseded copies are pruned a day after
  they stop being referenced — a long-running session still runs the old
  command line);
- Grok: `$GROK_HOME/hooks/miragen.json` (Grok 1.0.41 loads hook FILES in
  every mode with no trust step; plugin hooks never see SessionStart);
- Codex: our groups in `$CODEX_HOME/hooks.json` (user groups preserved),
  and in `$CODEX_HOME/config.toml` the `[mcp_servers.miragen-bridge]`
  table (the stdio MCP proxy) plus `[hooks.state."<key>"]` tables holding
  the `trusted_hash` of exactly our own hook entries — computed as Codex
  computes it (codex-rs hooks/src/engine/discovery.rs `hook_hash`), since
  `codex exec` silently skips untrusted hooks;
- `<home>/miragen-adapter/setup.json`: the URL + token file this setup
  baked, read by the plugin's MCP proxy so tools reach the daemon the hooks
  reach.

Stdlib only (the adapter's contract). Python ≥ 3.10 for the hooks, the MCP
proxy and the Grok setup; the Codex setup reads TOML with tomllib (3.11+,
imported lazily — without it ensure_codex raises SetupError and nothing
else is affected). TOML is edited as text, table by table, then re-parsed —
a result that changes anything but our own tables is refused rather than
written. Files that are symlinks (dotfile managers) are written THROUGH:
the link stays, its target changes.

Cross-writer safety: every read-modify-write holds an exclusive flock on
`<home>/.mira-harness-setup.lock` — the same file the MiraDesign adapter's
setup locks, so the two daemons never interleave edits of one hooks.json or
config.toml.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from miragen_hook.install import CODEX_EVENTS, GROK_BUILD_EVENTS, _group_is_owned

PACKAGE = "miragen_hook"
NAME = "miragen"
ADAPTER_DIR = f"{NAME}-adapter"
SETUP_FILE = "setup.json"
MCP_SERVER_NAME = "miragen-bridge"
STATUS_MESSAGE = "miragen memory"
MANAGED_COMMENT = "# managed by miragen harness setup"
MANAGED_BY_DAEMON = "miragend"
MANAGED_BY_CLI = "cli"          # `miragen-hook setup`: authoritative like the daemon's
MANAGED_BY_PLUGIN = "plugin"    # the proxy's fallback: only mirrors the resolution chain
AUTHORITATIVE = frozenset({MANAGED_BY_DAEMON, MANAGED_BY_CLI})

# The ONE place the Codex tool approval is set (decided by Ilari 2026-09-23;
# mirrored by hand in plugins/miragen-memory/codex.mcp.json), scoped to our
# own server table. `codex exec` runs with approval policy `never` and
# refuses any MCP tool that needs approval ("MCP tool call requires
# approval, but approval policy is never", found live).
CODEX_TOOLS_APPROVAL_MODE = "approve"

# A superseded adapter copy is deleted this long after it stopped being
# referenced (a session started before the switch keeps its command line).
PRUNE_AFTER_S = 24 * 3600
_SUPERSEDED = ".superseded"

# Codex spills hook additionalContext above ~2,500 tokens (bytes/4) to a
# file and shows the model a head/tail preview. Our context-bearing entries
# raise the threshold instead; the adapter caps Codex context below it
# (client.CODEX_CONTEXT_CAP_BYTES), so nothing we send is ever spilled.
CODEX_CONTEXT_TOKEN_LIMIT = 6_000
CODEX_DEFAULT_CONTEXT_TOKEN_LIMIT = 2_500
_CODEX_CONTEXT_LIMIT_EVENTS = frozenset(
    {"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart"}
)
_CODEX_EVENT_LABELS = {
    "PreToolUse": "pre_tool_use", "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use", "PreCompact": "pre_compact",
    "PostCompact": "post_compact", "SessionStart": "session_start",
    "SessionEnd": "session_end", "UserPromptSubmit": "user_prompt_submit",
    "SubagentStart": "subagent_start", "SubagentStop": "subagent_stop",
    "Stop": "stop", "Interrupt": "interrupt",
}
_CODEX_SESSION_END_MAX_S = 3
_CODEX_DEFAULT_TIMEOUT_S = 600


class SetupError(RuntimeError):
    """A file this setup must edit is not in a state it may edit."""


LOCK_FILE = ".mira-harness-setup.lock"  # shared with the MiraDesign adapter: keep the name


@contextmanager
def harness_lock(home: Path):
    """Exclusive advisory lock for one harness home (blocking; the other
    writer's critical section is milliseconds). No fcntl (Windows): no lock."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        yield
        return
    fd = os.open(home / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _tomllib():
    try:
        import tomllib
    except ImportError:  # Python 3.10
        raise SetupError("the Codex setup needs Python >= 3.11 (tomllib); the hooks and the "
                         "MCP proxy do not") from None
    return tomllib


# ── homes / installation ─────────────────────────────────────────────────────


def grok_home(environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("GROK_HOME") or Path(env.get("HOME") or Path.home()) / ".grok")


def codex_home(environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("CODEX_HOME") or Path(env.get("HOME") or Path.home()) / ".codex")


def _binary(name: str, environ: dict | None = None) -> str | None:
    env = os.environ if environ is None else environ
    found = shutil.which(name, path=env.get("PATH"))
    if found:
        return found
    home = Path(env.get("HOME") or Path.home())
    for candidate in (home / ".local" / "bin" / name, home / f".{name}" / "bin" / name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# ── atomic, change-only writes ───────────────────────────────────────────────


def _atomic_write(path: Path, data: str, *, default_mode: int = 0o644) -> None:
    """Replace `path` atomically, keeping its mode; a symlink (a dotfile
    manager's) is followed so the link survives and its target changes."""
    target = Path(os.path.realpath(path))
    try:
        mode = target.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = default_mode
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write_if_changed(path: Path, data: str, changed: list[str]) -> None:
    if _read_text(path) == data:
        return
    _atomic_write(path, data)
    changed.append(str(path))


# ── the adapter copy ─────────────────────────────────────────────────────────


def adapter_source() -> Path:
    """The `miragen_hook` package this code runs from (installed package,
    checkout, or a plugin's vendored copy — they are byte-identical)."""
    return Path(__file__).resolve().parent


def _package_files(package: Path) -> list[Path]:
    return sorted(p for p in package.glob("*.py") if p.is_file())


def adapter_digest(package: Path | None = None) -> str:
    package = package or adapter_source()
    digest = hashlib.sha256()
    for path in _package_files(package):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def snapshot_adapter(dest: Path) -> Path:
    """Copy the running adapter package to `dest/miragen_hook` ONCE and
    return that package dir: the daemon installs from this snapshot, so a
    `git checkout` in an editable install's live checkout never propagates
    into the harness homes (and the digest always names what is copied)."""
    source = adapter_source()
    for _ in range(3):
        digest = adapter_digest(source)
        package = dest / PACKAGE
        shutil.rmtree(package, ignore_errors=True)
        package.mkdir(parents=True)
        for path in _package_files(source):
            shutil.copy2(path, package / path.name)
        if (source / SKILLS_DIR).is_dir():
            shutil.copytree(source / SKILLS_DIR, package / SKILLS_DIR,
                            ignore=shutil.ignore_patterns("__pycache__"))
        if adapter_digest(package) == digest:
            return package
    raise SetupError(f"{source} kept changing while it was snapshotted")


# ── the memory-bridge skill ──────────────────────────────────────────────────
#
# The plugin's skill, vendored into this package (miragen_hook/skills/, kept
# identical by scripts/sync_plugin_adapter.sh + a test), installed into the
# harness's USER skills dir — the dirs each reads at startup (source-read):
# Codex 0.156 `$CODEX_HOME/skills/<name>/SKILL.md` (ext/skills host_roots.rs,
# the user config layer's folder), Grok ≥ 1.0.41 `$GROK_HOME/skills/<name>/
# SKILL.md` (xai-grok-agent prompt/skills.rs: grok_home + SKILL_SUBDIRS).
# Ours only by a marker file inside it; a same-named skill without it is the
# user's and is never touched (skipped, reported).

SKILLS_DIR = "skills"
SKILL_MARKER = ".miragen-managed"


def skill_sources(source: Path | None = None) -> list[Path]:
    root = (source or adapter_source()) / SKILLS_DIR
    return sorted(d for d in root.iterdir() if (d / "SKILL.md").is_file()) if root.is_dir() else []


def _tree_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file() and p.name != SKILL_MARKER
                       and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(directory)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def ensure_skills(home: Path, changed: list[str], source: Path | None = None) -> dict[str, str]:
    """Install/refresh our skills under `<home>/skills/`. Returns name →
    'current' | 'written' | 'skipped: …'. Pure read when current."""
    result: dict[str, str] = {}
    root = home / SKILLS_DIR
    for skill in skill_sources(source):
        target = root / skill.name
        digest = _tree_digest(skill)
        marker = target / SKILL_MARKER
        if target.exists() and not marker.is_file():
            result[skill.name] = "skipped: a skill of that name exists and is not ours"
            continue
        if marker.is_file() and marker.read_text().strip() == digest and _tree_digest(target) == digest:
            result[skill.name] = "current"
            continue
        root.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{skill.name}.", dir=root))
        try:
            shutil.copytree(skill, staging, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", SKILL_MARKER))
            (staging / SKILL_MARKER).write_text(digest + "\n")
            if _tree_digest(staging) != digest:
                raise SetupError(f"{skill} changed while it was copied — retrying next run")
            old = None
            if target.exists():
                old = root / f".{skill.name}.old-{os.getpid()}"
                os.rename(target, old)
            os.rename(staging, target)
            if old is not None:
                shutil.rmtree(old, ignore_errors=True)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        changed.append(str(target))
        result[skill.name] = "written"
    return result


def remove_skills(home: Path, changed: list[str], source: Path | None = None) -> None:
    for skill in skill_sources(source):
        target = home / SKILLS_DIR / skill.name
        if (target / SKILL_MARKER).is_file():
            shutil.rmtree(target)
            changed.append(str(target))


def ensure_adapter_copy(home: Path, changed: list[str], source: Path | None = None) -> Path:
    """`<home>/miragen-adapter/<digest>` holding a `miragen_hook` package:
    created once per adapter version by an atomic directory rename, then
    only read. Returns the copy's root (what goes on sys.path). The copy is
    verified against the digest before it is published."""
    source = source or adapter_source()
    digest = adapter_digest(source)
    base = home / ADAPTER_DIR
    target = base / digest
    if (target / PACKAGE / "__main__.py").is_file() and adapter_digest(target / PACKAGE) == digest:
        (target / _SUPERSEDED).unlink(missing_ok=True)
        return target
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{digest}.", dir=base))
    try:
        (staging / PACKAGE).mkdir()
        for path in _package_files(source):
            shutil.copy2(path, staging / PACKAGE / path.name)
        if adapter_digest(staging / PACKAGE) != digest:
            raise SetupError(f"{source} changed while it was copied — retrying next run")
        if target.exists():  # a broken/partial copy: replace it
            shutil.rmtree(target)
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    changed.append(str(target))
    return target


def prune_adapter_copies(home: Path, keep: Path, *, now: float | None = None) -> list[str]:
    """Mark copies other than `keep` superseded; delete those marked more
    than PRUNE_AFTER_S ago. Staging leftovers are removed the same way."""
    base = home / ADAPTER_DIR
    removed: list[str] = []
    now = time.time() if now is None else now
    if not base.is_dir():
        return removed
    for entry in base.iterdir():
        if not entry.is_dir() or entry.resolve() == keep.resolve():
            continue
        marker = entry / _SUPERSEDED
        try:
            if not marker.exists():
                marker.touch()
            elif now - marker.stat().st_mtime > PRUNE_AFTER_S:
                shutil.rmtree(entry)
                removed.append(str(entry))
        except OSError:
            continue
    return removed


def _check_no_dollar(*values: str | None) -> None:
    # Grok scans hook commands for `$VAR` regardless of quoting and refuses
    # to run one whose variable is unset; Codex substitutes `${VAR}` for
    # plugin hooks. A `$` in a baked value would silently disable the hook.
    for value in values:
        if value and "$" in value:
            raise SetupError(f"'{value}' contains '$': the harness would read it as a "
                             "variable — use a path/URL without '$'")


def adapter_command(copy_root: Path, *args: str) -> str:
    """`python3 <copy>/miragen_hook/__main__.py <args…>`, every part quoted
    (harnesses run hooks through `sh -c` in the session's cwd). By FILE
    path, never `-m`: the working directory would shadow the copy."""
    main = copy_root / PACKAGE / "__main__.py"
    return " ".join(["python3", shlex.quote(str(main)), *(shlex.quote(a) for a in args)])


def _forward_args(harness: str, url: str, token_file: str | None) -> list[str]:
    args = [harness, "--daemon", url]
    if token_file:
        args += ["--token-file", token_file]
    return args


def _setup_record(url: str, token_file: str | None, managed_by: str) -> str:
    return json.dumps({"url": url, "token_file": token_file, "managed_by": managed_by},
                      indent=2, sort_keys=True) + "\n"


def read_setup_record(home: Path) -> dict:
    try:
        data = json.loads((home / ADAPTER_DIR / SETUP_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ── JSON hook files (Grok hook file, Codex hooks.json) ───────────────────────


def _load_hooks_json(path: Path) -> dict:
    text = _read_text(path)
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise SetupError(f"{path} is not valid JSON — not touching it") from None
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise SetupError(f"{path}: unexpected shape — not touching it")
    return data


def handler_is_owned(handler: Any) -> bool:
    return isinstance(handler, dict) and _group_is_owned({"hooks": [handler]})


def merge_owned_groups(data: dict, entries: dict[str, dict]) -> dict:
    """`data` with our handler for each `entries` event replaced IN PLACE (or
    appended as a group of its own when the event has none), further owned
    duplicates and our handlers of events we no longer use removed,
    everything else untouched. In place matters: Codex keys hook trust by
    group AND handler index, so moving our group behind a later one (a
    user's, another daemon's) would silently untrust that one — and two
    setups each re-appending would ping-pong. A user group that holds our
    handler next to theirs (or under a matcher) keeps its shape: only our
    handler inside it is replaced or removed."""
    merged = json.loads(json.dumps(data))
    hooks = merged.setdefault("hooks", {})
    for event in list(hooks):
        groups = hooks[event] if isinstance(hooks[event], list) else []
        replacement = entries.get(event)
        kept: list = []
        placed = False
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list) or not any(handler_is_owned(h) for h in handlers):
                kept.append(group)
                continue
            new_handlers = []
            for handler in handlers:
                if not handler_is_owned(handler):
                    new_handlers.append(handler)
                elif replacement is not None and not placed:
                    new_handlers.append(replacement)
                    placed = True
            if new_handlers:
                kept.append({**group, "hooks": new_handlers})
        if replacement is not None and not placed:
            kept.append({"hooks": [replacement]})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    for event, entry in entries.items():
        if event not in hooks:
            hooks[event] = [{"hooks": [entry]}]
    if not hooks:
        del merged["hooks"]
    return merged


def _dump_json(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def _write_hooks_json(path: Path, current: dict, merged: dict, changed: list[str]) -> None:
    if merged == current and path.exists():
        return  # semantically current: never rewrite (user formatting kept)
    _atomic_write(path, _dump_json(merged))
    changed.append(str(path))


# ── Grok Build ───────────────────────────────────────────────────────────────


def grok_hook_entries(copy_root: Path, *, url: str, token_file: str | None) -> dict[str, dict]:
    command = adapter_command(copy_root, *_forward_args("grok-build", url, token_file))
    return {
        event: {"type": "command", "command": command, "timeout": timeout,
                "statusMessage": STATUS_MESSAGE}
        for event, timeout in GROK_BUILD_EVENTS
    }


def ensure_grok(
    home: Path | str | None = None, *, url: str, token_file: str | None = None,
    managed_by: str = MANAGED_BY_DAEMON, environ: dict | None = None, source: Path | None = None,
) -> dict[str, Any]:
    """Grok Build: the hook file + adapter copy + setup record. A no-op
    (installed=False) when `$GROK_HOME` does not exist. `source`: the
    adapter package to install (the daemon's start-time snapshot)."""
    home = Path(home) if home is not None else grok_home(environ)
    status: dict[str, Any] = {"harness": "grok-build", "home": str(home),
                              "installed": home.is_dir(), "binary": _binary("grok", environ),
                              "changed": [], "current": False}
    if not status["installed"]:
        return status
    if not url:
        raise SetupError("no URL to point the Grok hooks at")
    changed: list[str] = status["changed"]
    with harness_lock(home):
        copy_root = ensure_adapter_copy(home, changed, source)
        _check_no_dollar(str(copy_root), url, token_file)
        hook_file = home / "hooks" / f"{NAME}.json"
        current = _load_hooks_json(hook_file)
        merged = merge_owned_groups(current, grok_hook_entries(copy_root, url=url, token_file=token_file))
        _write_hooks_json(hook_file, current, merged, changed)
        _write_if_changed(home / ADAPTER_DIR / SETUP_FILE, _setup_record(url, token_file, managed_by),
                          changed)
        status["skills"] = ensure_skills(home, changed, source)
        status["pruned"] = prune_adapter_copies(home, copy_root)
    status["current"] = True
    status["hook_file"] = str(hook_file)
    return status


def remove_grok(home: Path | str | None = None, *, environ: dict | None = None) -> dict[str, Any]:
    home = Path(home) if home is not None else grok_home(environ)
    changed: list[str] = []
    if not home.is_dir():
        return {"harness": "grok-build", "home": str(home), "changed": changed}
    with harness_lock(home):
        hook_file = home / "hooks" / f"{NAME}.json"
        if hook_file.exists():
            current = _load_hooks_json(hook_file)
            merged = merge_owned_groups(current, {})
            if merged:
                _write_hooks_json(hook_file, current, merged, changed)
            else:
                hook_file.unlink()
                changed.append(str(hook_file))
        remove_skills(home, changed)
        if (home / ADAPTER_DIR).is_dir():
            shutil.rmtree(home / ADAPTER_DIR)
            changed.append(str(home / ADAPTER_DIR))
    return {"harness": "grok-build", "home": str(home), "changed": changed}


# ── Codex: trust hashes ──────────────────────────────────────────────────────


def _codex_normalized_timeout(event: str, timeout: int | None) -> int:
    if event in ("SessionEnd", "Interrupt"):
        return max(1, min(timeout if timeout is not None else 1, _CODEX_SESSION_END_MAX_S))
    return max(1, timeout if timeout is not None else _CODEX_DEFAULT_TIMEOUT_S)


def codex_hook_hash(event: str, entry: dict, matcher: str | None = None) -> str:
    """Codex's `trusted_hash` for one command hook: sha256 over the
    canonical (sorted-key, compact) JSON of the normalized identity
    `{event_name, matcher?, hooks: [handler]}`, where the handler has its
    timeout normalized (SessionEnd clamped to 1–3 s, others default 600),
    `async` always present, `statusMessage` only when set (never `commandWindows`),
    and `additionalContextLimit` only on the events that honour it and only
    when it differs from the 2,500-token default."""
    handler: dict[str, Any] = {
        "type": "command",
        "command": entry["command"],
        "timeout": _codex_normalized_timeout(event, entry.get("timeout")),
        "async": bool(entry.get("async", False)),
    }
    # commandWindows is NOT part of the identity: Codex hashes the normalized
    # handler with command_windows = None (discovery.rs).
    if entry.get("statusMessage") is not None:
        handler["statusMessage"] = entry["statusMessage"]
    limit = entry.get("additionalContextLimit")
    if (limit is not None and event in _CODEX_CONTEXT_LIMIT_EVENTS
            and limit != CODEX_DEFAULT_CONTEXT_TOKEN_LIMIT):
        handler["additionalContextLimit"] = limit
    identity: dict[str, Any] = {"event_name": _CODEX_EVENT_LABELS[event], "hooks": [handler]}
    if matcher is not None:
        identity["matcher"] = matcher
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def codex_hook_key(hooks_json: str, event: str, group_index: int, handler_index: int = 0) -> str:
    return f"{hooks_json}:{_CODEX_EVENT_LABELS[event]}:{group_index}:{handler_index}"


def codex_hooks_json_spellings(home: Path) -> list[str]:
    """How Codex names `$CODEX_HOME/hooks.json` in trust keys: canonicalized
    when CODEX_HOME is set, `$HOME/.codex` verbatim otherwise. The daemon's
    environment may differ from the harness's, so both spellings of OUR
    file are trusted."""
    literal = str(Path(os.path.abspath(home)) / "hooks.json")
    canonical = str(Path(os.path.realpath(home)) / "hooks.json")
    return sorted({literal, canonical})


def codex_trust_states(home: Path, merged: dict) -> dict[str, str]:
    """key → trusted_hash for exactly the groups we own in the merged file."""
    states: dict[str, str] = {}
    for event, groups in (merged.get("hooks") or {}).items():
        if event not in _CODEX_EVENT_LABELS or not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            for handler_index, entry in enumerate(group.get("hooks") or []):
                if not handler_is_owned(entry) or entry.get("type") != "command":
                    continue  # a user's handler, even in a group with ours: never trusted by us
                digest = codex_hook_hash(event, entry, group.get("matcher"))
                for spelling in codex_hooks_json_spellings(home):
                    states[codex_hook_key(spelling, event, group_index, handler_index)] = digest
    return states


# ── Codex: config.toml, edited table by table ────────────────────────────────


# Quoted key parts may contain ']' and '#'.
_HEADER = re.compile(
    r"""^\s*\[\[?\s*(?P<key>(?:"(?:[^"\\]|\\.)*"|'[^']*'|[^\]"'#])+?)\s*\]\]?\s*(?P<comment>#.*)?$"""
)
_TOML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n",
                 "\f": "\\f", "\r": "\\r"}


def _toml_string(value: str) -> str:
    """A TOML basic string: quote, backslash and control characters escaped,
    everything else (non-BMP too) written as UTF-8 — never JSON's
    surrogate-pair escapes, which TOML rejects."""
    out = []
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _parse_key(raw: str) -> list[str] | None:
    """A TOML dotted key → its parts (bare, "basic" or 'literal' parts)."""
    parts: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        while i < n and raw[i] in " \t":
            i += 1
        if i >= n:
            return None
        if raw[i] == '"':
            j, buf = i + 1, []
            while j < n and raw[j] != '"':
                if raw[j] == "\\" and j + 1 < n:
                    try:
                        buf.append(json.loads('"' + raw[j:j + (6 if raw[j + 1] == "u" else 2)] + '"'))
                    except ValueError:
                        return None
                    j += 6 if raw[j + 1] == "u" else 2
                    continue
                buf.append(raw[j])
                j += 1
            if j >= n:
                return None
            parts.append("".join(buf))
            i = j + 1
        elif raw[i] == "'":
            j = raw.find("'", i + 1)
            if j < 0:
                return None
            parts.append(raw[i + 1:j])
            i = j + 1
        else:
            m = re.match(r"[A-Za-z0-9_-]+", raw[i:])
            if not m:
                return None
            parts.append(m.group(0))
            i += m.end()
        while i < n and raw[i] in " \t":
            i += 1
        if i < n:
            if raw[i] != ".":
                return None
            i += 1
    return parts or None


def _blocks(text: str) -> list[tuple[list[str] | None, bool, list[str]]]:
    """The file as (header key parts | None for the preamble, header carries
    our marker, lines) blocks. Only used to cut OUR tables out; the result
    is always re-parsed and compared, so a misread can refuse, not corrupt."""
    blocks: list[tuple[list[str] | None, bool, list[str]]] = [(None, False, [])]
    for line in text.splitlines(keepends=True):
        m = _HEADER.match(line.rstrip("\r\n"))
        if m:
            key = _parse_key(m.group("key"))
            blocks.append((key, bool(m.group("comment") and MANAGED_COMMENT in m.group("comment")), [line]))
        else:
            blocks[-1][2].append(line)
    return blocks


def _trailing_comments(lines: list[str]) -> list[str]:
    """The comment lines at the end of a block (blank lines between them
    kept): they introduce the NEXT table — a user's `# ---- projects ----`
    placed after one of our tables must survive our table's rewrite."""
    tail: list[str] = []
    for line in reversed(lines[1:]):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            tail.append(line)
        else:
            break
    tail.reverse()
    while tail and not tail[0].strip():
        tail.pop(0)
    return tail


def _is_ours(key: list[str] | None, marked: bool, state_keys: set[str]) -> bool:
    if not key:
        return False
    if key[:2] == ["mcp_servers", MCP_SERVER_NAME]:
        return True
    if len(key) == 3 and key[:2] == ["hooks", "state"]:
        return marked or key[2] in state_keys
    return False


def _strip_owned(parsed: dict, state_keys: set[str], marked_keys: set[str]) -> dict:
    data = json.loads(json.dumps(parsed, default=str))
    servers = data.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop(MCP_SERVER_NAME, None)
        if not servers:
            data.pop("mcp_servers")
    hooks = data.get("hooks")
    state = hooks.get("state") if isinstance(hooks, dict) else None
    if isinstance(state, dict):
        for key in state_keys | marked_keys:
            state.pop(key, None)
        if not state:
            hooks.pop("state")
        if not hooks:
            data.pop("hooks")
    return data


def _owned_values(parsed: dict, state_keys: set[str]) -> dict:
    servers = parsed.get("mcp_servers") if isinstance(parsed.get("mcp_servers"), dict) else {}
    hooks = parsed.get("hooks") if isinstance(parsed.get("hooks"), dict) else {}
    state = hooks.get("state") if isinstance(hooks.get("state"), dict) else {}
    return {
        "server": servers.get(MCP_SERVER_NAME),
        "state": {k: state.get(k) for k in sorted(state_keys)},
    }


def desired_server(parsed: dict, server: dict | None) -> dict | None:
    """Our server table as it should be: our fields over whatever the user
    added to the same table (`enabled = false`, `[….tools.<t>] approval_mode`
    narrowing our blanket approve, env, timeouts…) — the user's additions
    are carried over, never reverted on the next interval."""
    if server is None:
        return None
    existing = _owned_values(parsed, set())["server"]
    merged = {k: v for k, v in existing.items() if k not in server} if isinstance(existing, dict) else {}
    merged.update(server)
    return json.loads(json.dumps(merged, default=str))


def _split_bom(text: str) -> tuple[str, str]:
    return ("﻿", text[1:]) if text.startswith("﻿") else ("", text)


def render_codex_config(
    text: str | None, *, server: dict | None, trust: dict[str, str],
) -> str:
    """`text` with our tables replaced: the MCP server table (None removes
    it; the user's own additions to it are kept) and one
    `[hooks.state."<key>"]` per `trust` entry (stale marked ones removed). A
    user's `enabled` on one of our keys is carried over. Raises SetupError
    instead of returning anything that changes a byte of meaning outside our
    tables."""
    toml = _tomllib()
    bom, text = _split_bom(text or "")
    try:
        before = toml.loads(text)
    except toml.TOMLDecodeError as exc:
        raise SetupError(f"config.toml is not valid TOML ({exc}) — not touching it") from None
    state_keys = set(trust)
    blocks = _blocks(text)
    marked = {b[0][2] for b in blocks if b[0] and b[1] and len(b[0]) == 3 and b[0][:2] == ["hooks", "state"]}
    prior_state = ((before.get("hooks") or {}).get("state") or {}) if isinstance(before.get("hooks"), dict) else {}
    wanted_server = desired_server(before, server)

    server_block: list[str] = []
    if wanted_server is not None:
        server_block.append(f"[mcp_servers.{MCP_SERVER_NAME}]  {MANAGED_COMMENT}\n")
        for field, value in wanted_server.items():
            server_block.append(f"{_toml_key(field)} = {_toml_value(value)}\n")
        server_block.append("\n")
    state_block: list[str] = []
    for key in sorted(trust):
        state_block.append(f"[hooks.state.{_toml_string(key)}]  {MANAGED_COMMENT}\n")
        state_block.append(f"trusted_hash = {_toml_string(trust[key])}\n")
        previous = prior_state.get(key) if isinstance(prior_state, dict) else None
        if isinstance(previous, dict) and isinstance(previous.get("enabled"), bool):
            state_block.append(f"enabled = {'true' if previous['enabled'] else 'false'}\n")
        state_block.append("\n")

    # Our tables are rewritten WHERE THEY WERE (first occurrence), new ones
    # go to the end; comments that close one of our blocks introduce the
    # next table and stay with it.
    parts: list[str] = []
    server_placed = state_placed = False
    for key, is_marked, lines in blocks:
        if _is_ours(key, is_marked, state_keys):
            is_server = key[:2] == ["mcp_servers", MCP_SERVER_NAME]
            if is_server and not server_placed:
                parts.extend(server_block)
                server_placed = True
            elif not is_server and not state_placed:
                parts.extend(state_block)
                state_placed = True
            parts.extend(_trailing_comments(lines))
        else:
            if lines and parts and not parts[-1].endswith("\n"):
                parts[-1] += "\n"
            parts.extend(lines)
    tail = ([] if server_placed else server_block) + ([] if state_placed else state_block)
    body = "".join(parts).rstrip("\r\n")
    rendered = (body + "\n\n" if body else "") + "".join(tail)
    rendered = rendered.rstrip("\n") + "\n" if rendered.strip() else ""

    try:
        after = toml.loads(rendered)
    except toml.TOMLDecodeError as exc:
        raise SetupError(f"config.toml: our tables could not be placed ({exc}); "
                         f"is '{MCP_SERVER_NAME}' or hooks.state defined inline/dotted?") from None
    if _strip_owned(before, state_keys, marked) != _strip_owned(after, state_keys, marked):
        raise SetupError("config.toml: editing our tables would change other settings "
                         "(our keys defined inline or as dotted keys?) — not touching it")
    owned = _owned_values(after, state_keys)
    if json.loads(json.dumps(owned["server"], default=str)) != wanted_server or any(
        (owned["state"][k] or {}).get("trusted_hash") != trust[k] for k in state_keys
    ):
        raise SetupError("config.toml: our settings did not take effect as written "
                         "(defined a second time elsewhere?) — not touching it")
    return bom + rendered


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _toml_string(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if value in (float("inf"), float("-inf")):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise SetupError(f"cannot write a {type(value).__name__} back into config.toml")


def _config_is_current(text: str | None, server: dict | None, trust: dict[str, str]) -> bool:
    """Semantically current (our values as desired, no stale marked state):
    the periodic run must then be a pure read — Codex writes this file too."""
    toml = _tomllib()
    _, body = _split_bom(text or "")
    try:
        parsed = toml.loads(body)
    except toml.TOMLDecodeError:
        return False
    marked = {b[0][2] for b in _blocks(body)
              if b[0] and b[1] and len(b[0]) == 3 and b[0][:2] == ["hooks", "state"]}
    if marked - set(trust):
        return False
    owned = _owned_values(parsed, set(trust))
    current_server = json.loads(json.dumps(owned["server"], default=str)) if owned["server"] is not None else None
    return current_server == desired_server(parsed, server) and all(
        (owned["state"][k] or {}).get("trusted_hash") == trust[k] for k in trust
    )


# ── Codex ────────────────────────────────────────────────────────────────────


def codex_hook_entries(copy_root: Path, *, url: str, token_file: str | None) -> dict[str, dict]:
    command = adapter_command(copy_root, *_forward_args("codex", url, token_file))
    entries: dict[str, dict] = {}
    for event, timeout in CODEX_EVENTS:
        entry: dict[str, Any] = {"type": "command", "command": command, "timeout": timeout,
                                 "statusMessage": STATUS_MESSAGE}
        if event in ("SessionStart", "UserPromptSubmit"):
            entry["additionalContextLimit"] = CODEX_CONTEXT_TOKEN_LIMIT
        entries[event] = entry
    return entries


def codex_mcp_server(copy_root: Path, *, url: str, token_file: str | None) -> dict:
    # Codex starts stdio MCP servers with a cleared environment (HOME, PATH,
    # LANG… only): the URL and token file must be in the arguments.
    args = [str(copy_root / PACKAGE / "__main__.py"), "mcp-proxy", "--harness", "codex",
            "--daemon", url]
    if token_file:
        args += ["--token-file", token_file]
    # `codex exec` runs with approval policy `never`, under which a tool that
    # needs approval is refused outright ("MCP tool call requires approval,
    # but approval policy is never" — found live): the bridge's own tools are
    # approved up front, like its hooks are trusted. A user's per-tool
    # `[mcp_servers.miragen-bridge.tools.<t>] approval_mode` still narrows it.
    return {"command": "python3", "args": args, "default_tools_approval_mode": CODEX_TOOLS_APPROVAL_MODE}


def ensure_codex(
    home: Path | str | None = None, *, url: str, token_file: str | None = None,
    environ: dict | None = None, source: Path | None = None, managed_by: str = MANAGED_BY_DAEMON,
) -> dict[str, Any]:
    """Codex: native hooks (harness `codex`), their trust, and the bridge
    MCP server as the stdio proxy. A no-op when `$CODEX_HOME` does not exist."""
    home = Path(home) if home is not None else codex_home(environ)
    status: dict[str, Any] = {"harness": "codex", "home": str(home), "installed": home.is_dir(),
                              "binary": _binary("codex", environ), "changed": [], "current": False}
    if not status["installed"]:
        return status
    if not url:
        raise SetupError("no URL to point the Codex hooks at")
    _tomllib()  # before anything is written: no hooks without their trust
    changed: list[str] = status["changed"]
    with harness_lock(home):
        copy_root = ensure_adapter_copy(home, changed, source)
        hooks_path = home / "hooks.json"
        current = _load_hooks_json(hooks_path)
        merged = merge_owned_groups(current, codex_hook_entries(copy_root, url=url, token_file=token_file))
        trust = codex_trust_states(home, merged)
        server = codex_mcp_server(copy_root, url=url, token_file=token_file)
        config_path = home / "config.toml"
        config_text = _read_text(config_path)
        # config.toml (the trust) FIRST: if it cannot be written (refused,
        # read-only target), hooks.json stays as it was — new hooks without
        # their trust would be skipped by `codex exec` and prompt in the TUI.
        if not _config_is_current(config_text, server, trust):
            rendered = render_codex_config(config_text, server=server, trust=trust)
            if rendered != config_text:
                _atomic_write(config_path, rendered, default_mode=0o600)
                changed.append(str(config_path))
        _write_hooks_json(hooks_path, current, merged, changed)
        _write_if_changed(home / ADAPTER_DIR / SETUP_FILE,
                          _setup_record(url, token_file, managed_by), changed)
        status["skills"] = ensure_skills(home, changed, source)
        status["pruned"] = prune_adapter_copies(home, copy_root)
    status["current"] = True
    status["trusted_hooks"] = len(trust)
    return status


def remove_codex(home: Path | str | None = None, *, environ: dict | None = None) -> dict[str, Any]:
    home = Path(home) if home is not None else codex_home(environ)
    changed: list[str] = []
    if not home.is_dir():
        return {"harness": "codex", "home": str(home), "changed": changed}
    with harness_lock(home):
        hooks_path = home / "hooks.json"
        if hooks_path.exists():
            current = _load_hooks_json(hooks_path)
            _write_hooks_json(hooks_path, current, merge_owned_groups(current, {}), changed)
        config_path = home / "config.toml"
        text = _read_text(config_path)
        if text is not None:
            rendered = render_codex_config(text, server=None, trust={})
            if rendered != text:
                _atomic_write(config_path, rendered)
                changed.append(str(config_path))
        remove_skills(home, changed)
        if (home / ADAPTER_DIR).is_dir():
            shutil.rmtree(home / ADAPTER_DIR)
            changed.append(str(home / ADAPTER_DIR))
    return {"harness": "codex", "home": str(home), "changed": changed}
