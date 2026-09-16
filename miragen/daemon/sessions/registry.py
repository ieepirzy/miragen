"""Persisted session registry + the capture journal.

Both are plain JSON on the daemon's state dir: `sessions.json` (rewritten
atomically on change) and `journal/<key>.jsonl` (appended before a
capture is processed, deleted when the session is finalized). On start
the journal is replayed through the plane; Loimi's idempotency keys make
a replay of already-written events harmless, which is what lets the
journal stay this simple.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from miragen.daemon.sessions.models import EventEnvelope, ExternalSession, now_iso

_KEY_SAFE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class SessionRegistry:
    def __init__(self, state_dir: Path, *, retention_hours: int, stale_after_minutes: int) -> None:
        self.state_dir = Path(state_dir)
        self.retention = timedelta(hours=retention_hours)
        self.stale_after = timedelta(minutes=stale_after_minutes)
        self._sessions: dict[str, ExternalSession] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _path(self) -> Path:
        return self.state_dir / "sessions.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self._path().read_text())
        except (OSError, ValueError):
            return
        for key, data in (raw.get("sessions") or {}).items():
            try:
                self._sessions[key] = ExternalSession.model_validate(data)
            except Exception:  # a corrupt entry is dropped, not fatal
                continue

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": now_iso(),
            "sessions": {k: s.model_dump() for k, s in self._sessions.items()},
        }
        tmp = self._path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, self._path())

    # -- access ---------------------------------------------------------------

    def get(self, key: str) -> ExternalSession | None:
        return self._sessions.get(key)

    def upsert(self, envelope: EventEnvelope) -> tuple[ExternalSession, bool]:
        """The session for this envelope (created on first contact — any
        event, not only session start, so a restarted daemon relearns
        live sessions), plus whether it was just created."""
        session = self._sessions.get(envelope.key)
        created = session is None
        if session is None:
            session = ExternalSession(
                key=envelope.key, harness=envelope.harness, session_id=envelope.session_id,
            )
            self._sessions[envelope.key] = session
        elif session.state != "active" and envelope.event.name != "context.closed":
            # A session we thought gone speaks again (late hook, or a
            # resume under the same id): it is active.
            session.state = "active"
            session.ended_at = None
            session.end_reason = None
        session.touch(envelope)
        return session, created

    def list(self, *, active_only: bool = False) -> list[ExternalSession]:
        sessions = list(self._sessions.values())
        if active_only:
            sessions = [s for s in sessions if s.state == "active"]
        return sorted(sessions, key=lambda s: s.last_seen_at, reverse=True)

    def remove(self, key: str) -> None:
        self._sessions.pop(key, None)

    # -- housekeeping -----------------------------------------------------------

    def sweep(
        self, *, now: datetime | None = None, is_alive: Callable[[int], bool] | None = None,
    ) -> tuple[list[ExternalSession], list[str]]:
        """Returns (sessions that just went stale — to finalize, keys of
        old ended sessions that were pruned). A session with a known pid
        goes stale when the process is gone; one without goes stale after
        `stale_after` of silence."""
        current = now or datetime.now(timezone.utc)
        went_stale: list[ExternalSession] = []
        pruned: list[str] = []
        for key, session in list(self._sessions.items()):
            if session.state == "active":
                silent_for = current - _parse(session.last_seen_at)
                if session.pid is not None and is_alive is not None:
                    gone = not is_alive(session.pid) and silent_for > timedelta(seconds=5)
                else:
                    gone = silent_for > self.stale_after
                if gone:
                    session.state = "stale"
                    session.ended_at = now_iso()
                    session.end_reason = "process_gone" if session.pid else "silent"
                    went_stale.append(session)
            elif session.ended_at and current - _parse(session.ended_at) > self.retention:
                del self._sessions[key]
                pruned.append(key)
        return went_stale, pruned


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class EventJournal:
    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir) / "journal"

    def _file(self, key: str) -> Path:
        return self.dir / (_KEY_SAFE.sub("_", key) + ".jsonl")

    def append(self, envelope: EventEnvelope) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._file(envelope.key).open("a") as fh:
            fh.write(json.dumps(envelope.model_dump()) + "\n")

    def clear(self, key: str) -> None:
        try:
            self._file(key).unlink()
        except FileNotFoundError:
            pass

    def pending(self) -> Iterator[EventEnvelope]:
        if not self.dir.is_dir():
            return
        for path in sorted(self.dir.glob("*.jsonl")):
            try:
                lines = path.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    yield EventEnvelope.model_validate(json.loads(line))
                except Exception:
                    continue
