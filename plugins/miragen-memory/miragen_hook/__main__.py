"""`python3 -m miragen_hook <harness>` (or run by file path).

PostToolUse fires on EVERY tool call. It has one job — deliver a pending
background recall — so it exits here, before the adapter is even imported,
unless a recall-pending marker exists for the session (and the tool call is
the main thread's). The marker path must match client.recall_marker_path.
Stdlib only, Python 3.10+ (it runs under the harness host's python3).
"""

import hashlib
import io
import json
import os
import sys
from pathlib import Path

if not __package__:
    # Run by FILE path (`python3 <copy>/miragen_hook/__main__.py …`, what the
    # daemon-written hook and MCP entries do): make THIS copy's package the
    # one imported — ahead of the working directory (a miragen checkout),
    # PYTHONPATH and site-packages — and stop this directory's own modules
    # from being importable as top-level names.
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fast_exit() -> bool:
    if os.environ.get("MIRAGEN_WORKER"):
        return True  # a memory model call miragen started: never captured
    if len(sys.argv) < 2 or sys.argv[1] != "claude-code":
        return False
    raw = sys.stdin.read()
    sys.stdin = io.StringIO(raw)
    if '"PostToolUse"' not in raw:
        return False
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PostToolUse":
        return False
    session_id = payload.get("session_id")
    if not session_id or payload.get("agent_id"):
        return True
    base = os.environ.get("GROK_PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if base:
        directory = Path(base) / "pending"
    else:
        state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        directory = Path(state) / "miragen-hook" / "pending"
    name = hashlib.sha256(str(session_id).encode()).hexdigest()[:32]
    return not (directory / f"recall-{name}.json").exists()


if _fast_exit():
    raise SystemExit(0)

from miragen_hook.client import main  # noqa: E402

raise SystemExit(main())
