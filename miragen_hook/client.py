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
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from miragen_hook import ADAPTER_VERSION
from miragen_hook.normalize import (
    CONTEXT_BEARING,
    HARNESSES,
    NormalizedEvent,
    harness_output,
    normalize_hook_payload,
)

DEFAULT_DAEMON_URL = "http://127.0.0.1:8420"
EVENTS_PATH = "/sessions/v1/events"

# Seconds. Context-bearing events wait for retrieval; captures only wait
# for the daemon to journal + acknowledge (it does the write itself, after
# the hook has already returned — that is the point of a daemon).
TIMEOUT_CONTEXT_S = 10.0
TIMEOUT_CAPTURE_S = 2.0
TIMEOUT_SESSION_END_S = 1.0

_HARNESS_PROCESS_MARKERS = {
    "claude-code": ("claude",),
    "codex": ("codex",),
}


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


def build_envelope(
    harness: str, payload: dict, event: NormalizedEvent, *, environ: dict | None = None,
    pid: int | None = None,
) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry
        user = None
    return {
        "harness": harness,
        "session_id": event.session_id,
        "event": event.to_dict(),
        "client": {
            "pid": pid,
            "cwd": payload.get("cwd") or os.getcwd(),
            "user": user,
            "transcript_path": payload.get("transcript_path"),
            "project_dir": env.get("CLAUDE_PROJECT_DIR"),
            # A harness spawned by another agent can be told who spawned it;
            # nothing in the payload itself can claim a parent.
            "parent_session": env.get("MIRAGEN_PARENT_SESSION"),
            "agent": env.get("MIRAGEN_AGENT"),
            "adapter": ADAPTER_VERSION,
        },
        "sent_at": datetime.now(timezone.utc).isoformat(),
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
    env = os.environ if environ is None else environ
    if token_file:
        try:
            return Path(token_file).read_text().strip() or None
        except OSError:
            return None
    return env.get("MIRAGEND_TOKEN") or None


# ── entry point ──────────────────────────────────────────────────────────────


def run(
    harness: str, payload: dict, *, daemon_url: str, token: str | None,
    environ: dict | None = None, opener=None, pid: int | None = None,
) -> dict | None:
    """The whole adapter, testable: returns the harness stdout JSON (or
    None when nothing is to be printed)."""
    event = normalize_hook_payload(harness, payload)
    if event is None or event.session_id is None:
        return None
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
        return harness_output(harness, event.original_event, context)
    return None


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

    args_list = list(sys.argv[1:] if argv is None else argv)
    # `miragen-hook claude-code` is the hook command line: default subcommand.
    if args_list and args_list[0] in HARNESSES:
        args_list = ["forward", *args_list]
    args = parser.parse_args(args_list)

    daemon_url = args.daemon or os.environ.get("MIRAGEND_URL") or DEFAULT_DAEMON_URL

    if args.command == "install":
        from miragen_hook.install import install_hooks, uninstall_hooks

        path = install_hooks(
            args.harness, daemon_url=daemon_url, token_file=args.token_file,
            settings_path=Path(args.settings) if args.settings else None,
        ) if not args.uninstall else uninstall_hooks(
            args.harness, settings_path=Path(args.settings) if args.settings else None
        )
        print(f"{'removed from' if args.uninstall else 'installed into'} {path}")
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
