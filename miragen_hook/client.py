"""The forwarding client: one hook event → one envelope → miragend.

Trust and failure contract:
- credentials come from the host (a token file or MIRAGEND_TOKEN), never
  from the payload; the payload can describe a session, not choose a
  store, a scope or a principal — the daemon resolves those itself;
- bounded timeouts UNDER the harness's own hook budget (SessionEnd hooks
  share 1.5 s in Claude Code and 3 s max in Codex), so the adapter gives
  up before the harness gives up on it;
- exit 0 always, stdout only when there is context to inject, one line on
  stderr when the daemon could not be reached (the harness shows stderr
  only in debug output — no noise in normal use).
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from miragen_hook import ADAPTER_VERSION
from miragen_hook.normalize import (
    CONTEXT_BEARING,
    CONTEXT_OPENING,
    DEFERRED_CONTEXT_HARNESSES,
    HARNESSES,
    NormalizedEvent,
    harness_output,
    normalize_hook_payload,
)

DEFAULT_DAEMON_URL = "http://127.0.0.1:8420"
EVENTS_PATH = "/sessions/v1/events"
HOOKS_PATH = "/sessions/v1/hooks"  # raw harness payloads (HTTP hooks, no adapter)

# Where the daemon URL / token come from, first match wins. The plugin
# form (plugins/miragen-memory) hands its user options to hook processes
# as CLAUDE_PLUGIN_OPTION_<KEY> — but only Claude Code does: Codex and Grok
# Build load the same plugin and set none of them (Grok 1.0.41 source; the
# Codex break of 2026-09-22), so the URL then falls through to the
# environment, the option the user saved in Claude Code's settings, and the
# manifest's default — never silently to a daemon nobody configured.
URL_ENV_VARS = ("CLAUDE_PLUGIN_OPTION_DAEMON_URL", "MIRAGEND_URL")
TOKEN_ENV_VARS = ("CLAUDE_PLUGIN_OPTION_TOKEN", "MIRAGEND_TOKEN")
PLUGIN_NAME = "miragen-memory"
PLUGIN_ROOT_ENV_VARS = ("GROK_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")
_GIT_TIMEOUT_S = 2.0

# Seconds. Context-bearing events wait for retrieval; captures only wait
# for the daemon to journal + acknowledge (it does the write itself, after
# the hook has already returned — that is the point of a daemon).
TIMEOUT_CONTEXT_S = 10.0
TIMEOUT_CAPTURE_S = 2.0
TIMEOUT_SESSION_END_S = 1.0

_HARNESS_PROCESS_MARKERS = {
    "claude-code": ("claude",),
    "codex": ("codex",),
    "grok-build": ("grok",),
}

# Grok Build honours additionalContext on tool results up to 10,000 chars.
DEFERRED_CONTEXT_CAP = 10_000
# Codex spills additionalContext above its per-hook token limit (bytes/4) to
# a file, showing the model only a head/tail preview. The daemon-written
# entries raise the limit to 6,000 tokens (harness_setup); the context is
# capped in UTF-8 BYTES below that, keeping the head (the session header
# with store_run=… and the guide lead).
CODEX_CONTEXT_CAP_BYTES = 6_000 * 4 - 200
_TRUNCATION_NOTE = "\n[miragen-hook: context truncated here ({} more bytes) — memory_read / memory_recall for the rest]"
_DELIVERY_EVENTS = ("PostToolUse", "PostToolUseFailure")


def cap_context_bytes(text: str, limit: int) -> str:
    """`text` within `limit` UTF-8 bytes, head kept, with a note of the cut."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    note_room = len(_TRUNCATION_NOTE.format(len(raw)).encode("utf-8"))
    head = raw[:max(0, limit - note_room)].decode("utf-8", "ignore")
    return head + _TRUNCATION_NOTE.format(len(raw) - len(head.encode("utf-8")))


def timeout_for(event: NormalizedEvent) -> float:
    if event.name == "context.closed":
        return TIMEOUT_SESSION_END_S
    if event.name in CONTEXT_BEARING:
        return TIMEOUT_CONTEXT_S
    return TIMEOUT_CAPTURE_S


# ── process discovery (registration/liveness only, never semantics) ─────────


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace"
        )
    except OSError:
        return ""


def _proc_parent(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # "<pid> (<comm>) <state> <ppid> ..." — comm may contain spaces/parens.
        after = stat[stat.rindex(")") + 2:].split()
        return int(after[1])
    except (OSError, ValueError, IndexError):
        return None


def harness_pid(harness: str, *, start: int | None = None, depth: int = 5) -> int | None:
    """Best-effort pid of the harness process that ran this hook: walk up
    from our parent (the hook's shell) looking for the harness binary.
    Used by the daemon for liveness sweeps only — a wrong guess costs a
    late finalization, never a wrong memory."""
    markers = _HARNESS_PROCESS_MARKERS.get(harness, ())
    pid = start if start is not None else os.getppid()
    fallback = pid
    for _ in range(depth):
        if pid is None or pid <= 1:
            break
        cmdline = _proc_cmdline(pid)
        if any(marker in cmdline for marker in markers):
            return pid
        pid = _proc_parent(pid)
    return fallback


# ── envelope ─────────────────────────────────────────────────────────────────


def project_remote(cwd: str | None) -> str | None:
    """The repository's origin URL as seen from the harness's host — what a
    hosted daemon identifies the project by. Best-effort and bounded; a
    directory without git or without a remote yields None."""
    if not cwd:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:  # pragma: no cover
        return None


def build_envelope(
    harness: str, payload: dict, event: NormalizedEvent, *, environ: dict | None = None,
    pid: int | None = None, host: str | None = "", user: str | None = "",
    remote: bool | None = None, project_remote_url: str | None = "", cwd: str | None = "",
) -> dict[str, Any]:
    """`host`/`user`/`project_remote_url`/`cwd` default to "observe them
    here" (the adapter runs on the harness's machine); pass None to leave a
    field unknown — the daemon does that when it normalizes a raw hook
    payload, because those facts are not its to claim (its own working
    directory is NOT the session's)."""
    env = os.environ if environ is None else environ
    if cwd == "":
        cwd = payload.get("cwd") or os.getcwd()
    if user == "":
        try:
            user = getpass.getuser()
        except Exception:  # pragma: no cover - no passwd entry
            user = None
    if host == "":
        host = _hostname()
    if project_remote_url == "":
        project_remote_url = project_remote(cwd)
    if remote is None:
        remote = env.get("CLAUDE_CODE_REMOTE") == "true"
    return {
        "harness": harness,
        "session_id": event.session_id,
        "event": event.to_dict(),
        "client": {
            "pid": pid,
            "cwd": cwd,
            "user": user,
            "host": host,
            "remote": remote,
            "project_remote": project_remote_url,
            "transcript_path": payload.get("transcript_path"),
            "project_dir": env.get("CLAUDE_PROJECT_DIR"),
            # A harness spawned by another agent can be told who spawned it;
            # nothing in the payload itself can claim a parent.
            "parent_session": env.get("MIRAGEN_PARENT_SESSION"),
            "agent": env.get("MIRAGEN_AGENT"),
            "adapter": ADAPTER_VERSION,
            # "deferred": context answered for this event reaches the model
            # only on the next tool result (Grok Build), not now.
            "context_delivery": (
                "deferred" if harness in DEFERRED_CONTEXT_HARNESSES else "immediate"
            ),
        },
        "sent_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 — Python 3.10 floor
    }


def post_envelope(
    envelope: dict, *, daemon_url: str, token: str | None, timeout: float,
    opener=None,
) -> dict | None:
    """POST one envelope; the daemon's JSON answer, or None when it could
    not be reached / refused (logged to stderr, never raised)."""
    data = json.dumps(envelope).encode()
    headers = {"Content-Type": "application/json", "User-Agent": ADAPTER_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        daemon_url.rstrip("/") + EVENTS_PATH, data=data, headers=headers, method="POST"
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace") if exc.fp else ""
        print(f"miragen-hook: daemon answered {exc.code}: {detail}", file=sys.stderr)
        return None
    except Exception as exc:  # URLError, socket.timeout, ConnectionRefused…
        print(f"miragen-hook: daemon unreachable ({exc})", file=sys.stderr)
        return None
    try:
        return json.loads(body) if body else {}
    except ValueError:
        print("miragen-hook: daemon answered non-JSON", file=sys.stderr)
        return None


def read_token(token_file: str | None, environ: dict | None = None) -> str | None:
    """--token-file (a 0600 file the daemon's harness setup names) → the
    environment. A named file that is missing or empty falls through: the
    environment may still carry the bearer."""
    env = os.environ if environ is None else environ
    if token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError:
            token = ""
        if token:
            return token
    for name in TOKEN_ENV_VARS:
        if env.get(name):
            return env[name]
    return None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def saved_plugin_option(key: str, *, settings_path: Path | None = None) -> str | None:
    """The value the user saved for this plugin's option in Claude Code
    (`pluginConfigs["miragen-memory@<marketplace>"].options`) — what Claude
    Code itself would have exported as CLAUDE_PLUGIN_OPTION_<KEY>. Read for
    the harnesses that load the plugin without exporting it. Sensitive
    options (the token) are not stored there."""
    home = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    path = settings_path or Path(home) / "settings.json"
    configs = _read_json(path).get("pluginConfigs")
    if not isinstance(configs, dict):
        return None
    for plugin_key in sorted(configs):
        if not str(plugin_key).startswith(f"{PLUGIN_NAME}@"):
            continue
        options = configs[plugin_key].get("options") if isinstance(configs[plugin_key], dict) else None
        value = options.get(key) if isinstance(options, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


def own_plugin_root() -> Path | None:
    """This adapter's plugin directory when it runs as the plugin's vendored
    copy (a native Grok hook file points straight at it), else None."""
    root = Path(__file__).resolve().parent.parent
    return root if (root / ".claude-plugin" / "plugin.json").is_file() else None


def _plugin_roots(env: dict) -> list[Path]:
    roots = [Path(env[name]) for name in PLUGIN_ROOT_ENV_VARS if env.get(name)]
    own = own_plugin_root()
    if own is not None:
        roots.append(own)
    return roots


def manifest_option_default(key: str, environ: dict | None = None) -> str | None:
    """The plugin manifest's declared default for an option — the URL the
    plugin was published to talk to, found through the plugin root the
    harness exports to hooks, or the plugin this adapter was vendored into."""
    env = os.environ if environ is None else environ
    for root in _plugin_roots(env):
        spec = _read_json(root / ".claude-plugin" / "plugin.json").get("userConfig")
        option = spec.get(key) if isinstance(spec, dict) else None
        default = option.get("default") if isinstance(option, dict) else None
        if isinstance(default, str) and default:
            return default
    return None


def resolve_daemon_url(
    explicit: str | None, environ: dict | None = None, *, settings_path: Path | None = None,
) -> str:
    """explicit → plugin option env (Claude Code) → MIRAGEND_URL → the option
    saved in Claude Code's settings → the manifest default → loopback."""
    env = os.environ if environ is None else environ
    if explicit:
        return explicit
    for name in URL_ENV_VARS:
        if env.get(name):
            return env[name]
    if _plugin_roots(env):
        # Only the plugin's adapter has an option to recover; a bare
        # `miragen-hook` install keeps its explicit/env/loopback contract.
        return (
            saved_plugin_option("daemon_url", settings_path=settings_path)
            or manifest_option_default("daemon_url", env)
            or DEFAULT_DAEMON_URL
        )
    return DEFAULT_DAEMON_URL


# ── entry point ──────────────────────────────────────────────────────────────


# ── deferred context (harnesses that ignore start/prompt stdout) ──────────────


def pending_dir(environ: dict | None = None) -> Path:
    """Where context waits for its next delivery point: the harness's plugin
    data dir if one is exported (Grok exports none to native hook files, so
    in practice the user's state dir). Never the repository."""
    env = os.environ if environ is None else environ
    base = env.get("GROK_PLUGIN_DATA") or env.get("CLAUDE_PLUGIN_DATA")
    if base:
        return Path(base) / "pending"
    state = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "miragen-hook" / "pending"


# A queued block older than this is from a session that never reached its
# SessionEnd (crash, kill): stale, never delivered into a later resume.
PENDING_TTL_S = 12 * 3600


def _pending_file(session_id: str, environ: dict | None) -> Path:
    name = hashlib.sha256(session_id.encode()).hexdigest()[:32]
    return pending_dir(environ) / f"{name}.jsonl"


def _read_entries(path: Path) -> list[dict]:
    entries = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return entries
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("text"), str):
            entries.append(entry)
    return entries


def stash_context(
    session_id: str, context: str, environ: dict | None = None, *, origin: str = "SessionStart",
) -> None:
    """Queue context for the session's next tool result. A start (SessionStart)
    replaces everything queued — it is the whole picture; a prompt's context
    replaces the previous prompt's, so one turn's recall never reaches the
    model in a later turn next to a newer one."""
    path = _pending_file(session_id, environ)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if origin == "SessionStart":
            kept: list[dict] = []
        else:
            kept = [e for e in _read_entries(path) if e.get("from") != origin]
        kept.append({"from": origin, "text": context})
        data = "".join(json.dumps(e) + "\n" for e in kept).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        _sweep_stale(path.parent)
    except OSError as exc:
        print(f"miragen-hook: could not queue context ({exc})", file=sys.stderr)


def _sweep_stale(directory: Path) -> None:
    """Queues of sessions that never reached SessionEnd (crash, kill) and
    claims orphaned mid-delivery: nothing will ever take them."""
    cutoff = time.time() - PENDING_TTL_S
    for stale in directory.glob("*"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            continue


def take_context(session_id: str, environ: dict | None = None) -> str | None:
    """The queued context, removed so it is delivered exactly once (the
    rename claims it atomically against a concurrent tool hook). Over the
    harness cap the OLDEST text goes: a start block superseded by a newer
    prompt's context is the lesser loss."""
    path = _pending_file(session_id, environ)
    claimed = path.with_suffix(f".taking-{os.getpid()}")
    try:
        path.rename(claimed)
    except OSError:
        return None
    try:
        if time.time() - claimed.stat().st_mtime > PENDING_TTL_S:
            return None
        text = "\n\n".join(e["text"] for e in _read_entries(claimed))
        return text[-DEFERRED_CONTEXT_CAP:] or None
    except OSError:
        return None
    finally:
        claimed.unlink(missing_ok=True)


def discard_context(session_id: str, environ: dict | None = None) -> None:
    _pending_file(session_id, environ).unlink(missing_ok=True)


def foreign_entry_under_grok(harness: str, environ: dict | None = None) -> bool:
    """Grok Build also runs the hook entries it finds in Claude Code's
    settings files and plugins (compat is on by default) — a
    `miragen-hook claude-code` entry there would forward every event a
    second time next to the native `~/.grok/hooks/miragen.json`. Only the
    entry that names `grok-build` speaks for a Grok session; GROK_HOOK_EVENT
    (set for every Grok hook) marks the others, which then do nothing."""
    env = os.environ if environ is None else environ
    return bool(env.get("GROK_HOOK_EVENT")) and harness != "grok-build"


def _session_id_of(payload: dict) -> str | None:
    value = payload.get("session_id") or payload.get("sessionId")
    return str(value) if value else None


def run(
    harness: str, payload: dict, *, daemon_url: str, token: str | None,
    environ: dict | None = None, opener=None, pid: int | None = None,
) -> dict | None:
    """The whole adapter, testable: returns the harness stdout JSON (or
    None when nothing is to be printed)."""
    if foreign_entry_under_grok(harness, environ):
        return None
    deferred = harness in DEFERRED_CONTEXT_HARNESSES
    event_name = payload.get("hook_event_name") or ""
    delivered = None
    if deferred and event_name in _DELIVERY_EVENTS:
        session_id = _session_id_of(payload)
        delivered = take_context(session_id, environ) if session_id else None

    output = _forward(harness, payload, daemon_url=daemon_url, token=token,
                      environ=environ, opener=opener, pid=pid, deferred=deferred)
    if delivered:
        return harness_output(harness, event_name, delivered)
    return output


def _forward(
    harness: str, payload: dict, *, daemon_url: str, token: str | None,
    environ: dict | None, opener, pid: int | None, deferred: bool,
) -> dict | None:
    event = normalize_hook_payload(harness, payload)
    if event is None or event.session_id is None:
        return None
    if deferred and event.name == "context.closed":
        discard_context(event.session_id, environ)
    if pid is None:
        pid = harness_pid(harness)
    envelope = build_envelope(harness, payload, event, environ=environ, pid=pid)
    answer = post_envelope(
        envelope, daemon_url=daemon_url, token=token,
        timeout=timeout_for(event), opener=opener,
    )
    if not answer:
        return None
    context = answer.get("context")
    if context and event.name in CONTEXT_BEARING:
        if deferred:
            stash_context(event.session_id, context, environ,
                          origin="SessionStart" if event.name in CONTEXT_OPENING else "UserPromptSubmit")
            return None
        if harness == "codex":
            context = cap_context_bytes(context, CODEX_CONTEXT_CAP_BYTES)
        return harness_output(harness, event.original_event, context)
    return None


def _setup_command(args) -> int:
    from miragen_hook import harness_setup

    ensure = {"grok-build": harness_setup.ensure_grok, "codex": harness_setup.ensure_codex}
    remove = {"grok-build": harness_setup.remove_grok, "codex": harness_setup.remove_codex}
    try:
        if args.remove:
            status = remove[args.harness](args.home)
        else:
            if not args.daemon:
                print("miragen-hook: setup needs --daemon URL (where the sessions should report)",
                      file=sys.stderr)
                return 2
            status = ensure[args.harness](args.home, url=args.daemon, token_file=args.token_file,
                                          managed_by=harness_setup.MANAGED_BY_CLI)
    except harness_setup.SetupError as exc:
        print(f"miragen-hook: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(status, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="miragen-hook", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    forward = sub.add_parser("forward", help="forward one hook event (default)")
    forward.add_argument("harness", choices=HARNESSES)
    forward.add_argument("--daemon", default=None, help="miragend base URL")
    forward.add_argument("--token-file", default=None)

    install = sub.add_parser("install", help="install the hook entries into a harness")
    install.add_argument("harness", choices=HARNESSES)
    install.add_argument("--daemon", default=None)
    install.add_argument("--token-file", default=None)
    install.add_argument("--settings", default=None,
                         help="settings file to edit (default: the harness's user-global file)")
    install.add_argument("--uninstall", action="store_true")
    install.add_argument(
        "--http", action="store_true",
        help="install HTTP hooks (the harness POSTs raw payloads to the daemon; no adapter "
             "on the machine) — for a repository's .claude/settings.json so cloud sessions "
             "join too. The bearer is read from $MIRAGEND_TOKEN where the harness runs.",
    )

    setup = sub.add_parser(
        "setup", help="write/refresh (or --remove) the daemon-managed Grok/Codex setup by hand "
                      "(miragend does this itself; manual/debug fallback)")
    setup.add_argument("harness", choices=("grok-build", "codex"))
    setup.add_argument("--daemon", default=None, help="URL the harness sessions report to")
    setup.add_argument("--token-file", default=None)
    setup.add_argument("--home", default=None, help="GROK_HOME / CODEX_HOME (default: env, ~/.grok, ~/.codex)")
    setup.add_argument("--remove", action="store_true")

    from miragen_hook.mcp_proxy import add_parser as add_proxy_parser
    add_proxy_parser(sub)

    args_list = list(sys.argv[1:] if argv is None else argv)
    # `miragen-hook claude-code` is the hook command line: default subcommand.
    if args_list and args_list[0] in HARNESSES:
        args_list = ["forward", *args_list]
    args = parser.parse_args(args_list)

    if args.command == "mcp-proxy":
        from miragen_hook import mcp_proxy
        return mcp_proxy.main(args)

    if args.command == "setup":
        return _setup_command(args)

    daemon_url = resolve_daemon_url(args.daemon)

    if args.command == "install":
        if args.harness == "grok-build" and not args.daemon and not args.uninstall:
            explicit = os.environ.get("MIRAGEND_URL")
            if explicit:
                daemon_url = explicit  # what the installer was told: keep it
            elif own_plugin_root() is not None:
                # The plugin's copy resolves per event (env → saved option →
                # manifest default), so a later URL change needs no reinstall.
                daemon_url = None
                hooks_url = resolve_daemon_url(None)
                mcp_url = manifest_option_default("daemon_url") or DEFAULT_DAEMON_URL
                if hooks_url.rstrip("/") != mcp_url.rstrip("/"):
                    print(f"note: the hooks will use {hooks_url} (your saved Claude Code option) "
                          f"but Grok's bridge MCP server reads MIRAGEND_URL only (default {mcp_url}) "
                          f"— export MIRAGEND_URL={hooks_url} where grok starts if those differ")
            else:
                # A checkout/console install has no manifest to fall back
                # on: at runtime that would be the loopback — possibly a
                # different daemon than the one meant (split memory).
                print("miragen-hook: install grok-build needs --daemon URL (or MIRAGEND_URL), "
                      "or run it from the plugin: PYTHONPATH=<plugin dir> python3 -m "
                      "miragen_hook install grok-build", file=sys.stderr)
                return 2
        from miragen_hook.install import install_hooks, uninstall_hooks

        try:
            path = install_hooks(
                args.harness, daemon_url=daemon_url, token_file=args.token_file,
                settings_path=Path(args.settings) if args.settings else None,
                http=args.http,
            ) if not args.uninstall else uninstall_hooks(
                args.harness, settings_path=Path(args.settings) if args.settings else None
            )
        except RuntimeError as exc:
            print(f"miragen-hook: {exc}", file=sys.stderr)
            return 2
        print(f"{'removed from' if args.uninstall else 'installed into'} {path}")
        if (args.harness == "grok-build" and not args.uninstall and daemon_url
                and daemon_url != os.environ.get("MIRAGEND_URL")):
            # The plugin's MCP server reads only the environment.
            print(f"note: Grok's bridge MCP server reads MIRAGEND_URL only — export "
                  f"MIRAGEND_URL={daemon_url} where grok starts, or tools and hooks "
                  "reach different daemons")
        return 0

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        output = run(
            args.harness, payload, daemon_url=daemon_url,
            token=read_token(args.token_file),
        )
        if output is not None:
            sys.stdout.write(json.dumps(output))
            sys.stdout.flush()
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        print(f"miragen-hook: {exc}", file=sys.stderr)
    return 0
