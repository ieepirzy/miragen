"""The retrieval judgment log (docs/design/memory-effectiveness.md P2).

Every relevance-selector call leaves one JSONL line: the query, every
candidate card with its rank, whether the selector picked it and why, and
the model. Nobody labels anything: the selector's picks are the positives
and its non-picks the hard negatives — the training data for the learned
candidate adapter (#121), written from the first day on.

It holds prompt text, so: files are 0600, rotated daily, pruned by age
and by total size (the state volume is on a disk-constrained host), and
the whole log is covered by erasure once it moves into Loimi (#123).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("miragen.sessions.judgments")

_PREFIX = "judgments-"
_CARD_TEXT = 300          # the selector sees at most this much per card
_QUERY_TEXT = 2000


class JudgmentLog:
    def __init__(
        self, directory: Path, *, retention_days: int = 30, max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.directory = directory
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.written = 0
        self.failures = 0
        self._pruned_for: str | None = None

    def _file(self, day: str) -> Path:
        return self.directory / f"{_PREFIX}{day}.jsonl"

    def record(
        self, *, session: str, scope: str, recall_id: str, query: str,
        cards: list[dict[str, Any]], selected: list[tuple[str, str]], status: str,
        model: str | None, delivered: bool | None = None,
    ) -> None:
        """One selector call. Never raises: losing a training row must not
        cost the agent its recall."""
        picks = dict(selected)
        now = datetime.now(UTC)
        row = {
            "ts": now.isoformat(),
            "recall_id": recall_id,
            "session": session,
            "scope": scope,
            "query": query[:_QUERY_TEXT],
            "status": status,
            "model": model,
            "embedding_space": None,
            "delivered": delivered,
            "candidates": [
                {
                    "record_id": card.get("record_id"),
                    "type": card.get("type"),
                    "rank": rank,
                    "channels": card.get("channels") or card.get("ranks"),
                    "text": _card_text(card),
                    "selected": card.get("record_id") in picks,
                    "reason": picks.get(card.get("record_id")),
                }
                for rank, card in enumerate(cards, 1)
            ],
        }
        self._append(now.strftime("%Y-%m-%d"), row)

    def mark_delivered(self, *, recall_id: str, session: str, state: str) -> None:
        """Delivery is known later than selection: a small follow-up line,
        joined on recall_id (the log stays append-only)."""
        now = datetime.now(UTC)
        self._append(now.strftime("%Y-%m-%d"), {
            "ts": now.isoformat(), "recall_id": recall_id, "session": session,
            "delivery": state,
        }, count=False)

    def _append(self, day: str, row: dict[str, Any], *, count: bool = True) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self._file(day)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            if count:
                self.written += 1
            if self._pruned_for != day:
                self._pruned_for = day
                self.prune()
        except OSError as exc:
            self.failures += 1
            logger.warning(f"judgment log write failed: {exc}")

    def prune(self) -> None:
        """Drop files past retention, then the oldest until under max_bytes
        (today's file is never dropped by the size cap)."""
        files = sorted(self.directory.glob(f"{_PREFIX}*.jsonl"))
        cutoff = time.time() - self.retention_days * 86400
        kept = []
        for path in files:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                else:
                    kept.append(path)
            except OSError:
                continue
        total = sum(_size(path) for path in kept)
        for path in kept[:-1]:
            if total <= self.max_bytes:
                break
            total -= _size(path)
            path.unlink(missing_ok=True)

    def describe(self) -> dict[str, Any]:
        files = sorted(self.directory.glob(f"{_PREFIX}*.jsonl")) if self.directory.exists() else []
        return {
            "written": self.written, "failures": self.failures, "files": len(files),
            "bytes": sum(_size(path) for path in files),
        }


def _card_text(card: dict[str, Any]) -> str:
    payload = card.get("payload") or {}
    return str(payload.get("text") or payload.get("value") or payload)[:_CARD_TEXT]


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
