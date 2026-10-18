"""miragend's lifecycle audit log: one JSON line per action, appended durably.

Every mutating lifecycle call (create, config update, start, stop, restart,
delete, import, workspace file writes) and every credential plan the daemon
applies to a container leaves a record here, whatever its outcome. The log
is for the agents host (mirarun ADR-041 lanes) first: "who started what, and
which credentials did it get" must be answerable after the fact.

Records carry names, never values: an agent name, an action, an outcome, the
error code of a refusal, and for credential grants the *names* of the keys,
tokens and secret mounts. A value never reaches this module.

Enabled by ``MIRAGEND_AUDIT_LOG`` (a file path, typically on the daemon's
state volume). Unset, nothing is written and nothing changes. A write that
fails is logged and swallowed: the action already happened, and failing the
request because its audit line could not be written would misreport it.
The writer flushes and fsyncs every line, so a crash loses at most the line
being written.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUDIT_LOG_ENV = "MIRAGEND_AUDIT_LOG"


class AuditLog:
    """Append-only JSONL. Thread-safe within one process (the daemon's
    request handlers run in worker threads)."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    def record(self, action: str, *, outcome: str = "succeeded", **fields: Any) -> None:
        entry = {
            "ts": self._clock().isoformat(),
            "service": "miragend",
            "action": action,
            "outcome": outcome,
            **{k: v for k, v in fields.items() if v is not None},
        }
        line = json.dumps(entry, sort_keys=True, default=str) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
                try:
                    os.write(fd, line.encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as exc:
            logger.error("audit: could not write %s record for %s: %s",
                         action, fields.get("agent"), exc)


class NullAuditLog:
    """The default: auditing off."""

    path = None

    def record(self, action: str, *, outcome: str = "succeeded", **fields: Any) -> None:
        return None


def audit_log_from_env(environ: Mapping[str, str] | None = None) -> AuditLog | NullAuditLog:
    env = os.environ if environ is None else environ
    path = (env.get(AUDIT_LOG_ENV) or "").strip()
    return AuditLog(Path(path)) if path else NullAuditLog()


def names(values: Iterable[str]) -> list[str]:
    """A sorted, de-duplicated list of names for a record."""
    return sorted({str(v) for v in values})
