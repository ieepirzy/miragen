"""Where a turn's time goes (Mira#86): spans relative to the turn's start.

Measurement only. A harness builds one ``TurnTimer`` per turn and hands
``as_dict()`` to the run record (``RunRecord.timings``):

    {"started_at": "2026-10-18T00:26:38.535Z",   # wall clock of the turn start
     "cold": true,                               # the agent process was started for it
     "total_ms": 57528.2,
     "spans": [{"name": "spawn", "start_ms": 3.1, "duration_ms": 812.0, "detail": {}}, ...],
     "summary": {"spawn_ms": 812.0, "first_update_ms": 4120.4, ...}}

A span with ``duration_ms: null`` is a point event (e.g. one model response
completing). ``summary`` sums spans by kind, so a reader sees the split at a
glance; the spans are the waterfall.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

# A turn with hundreds of tool calls must still be a bounded record.
SPANS_CAP = 400


class TurnTimer:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.started_at = datetime.now(UTC)
        self.cold: bool | None = None
        self.instance: str | None = None   # whose turn: listings are claimed by it
        self.spans: list[dict[str, Any]] = []
        self.truncated = False
        # Per-name totals and first starts over EVERY span, kept or not: the
        # summary stays whole when the span list hits SPANS_CAP.
        self._totals: dict[str, float] = {}
        self._firsts: dict[str, float] = {}

    def ms(self, t: float | None = None) -> float:
        return round(((time.monotonic() if t is None else t) - self.t0) * 1000, 1)

    def add(self, name: str, start: float, end: float | None = None, **detail: Any) -> None:
        duration = None if end is None else round((end - start) * 1000, 1)
        at = self.ms(start)
        self._firsts[name] = min(self._firsts.get(name, at), at)   # spans can arrive late
        if duration is not None:
            self._totals[name] = self._totals.get(name, 0.0) + duration
        if len(self.spans) >= SPANS_CAP:
            self.truncated = True
            return
        self.spans.append({
            "name": name,
            "start_ms": at,
            "duration_ms": duration,
            "detail": {k: v for k, v in detail.items() if v is not None},
        })

    def event(self, name: str, **detail: Any) -> None:
        self.add(name, time.monotonic(), None, **detail)

    @contextlib.contextmanager
    def span(self, name: str, **detail: Any) -> Iterator[dict[str, Any]]:
        """Times the block; the yielded dict's entries join the detail."""
        start = time.monotonic()
        extra: dict[str, Any] = {}
        try:
            yield extra
        finally:
            self.add(name, start, time.monotonic(), **detail, **extra)

    def _sum(self, *names: str) -> float | None:
        vals = [self._totals[n] for n in names if n in self._totals]
        return round(sum(vals), 1) if vals else None

    def _first(self, name: str) -> float | None:
        return self._firsts.get(name)

    def as_dict(self, *, model_api_ms: float | None = None) -> dict[str, Any]:
        total = self.ms()
        summary = {
            "lifecycle_wait_ms": self._sum("lifecycle_wait"),
            "agent_ms": self._sum("agent"),
            "spawn_ms": self._sum("spawn"),
            "initialize_ms": self._sum("initialize"),
            "session_open_ms": self._sum("session_open"),
            "list_tools_ms": self._sum("list_tools"),
            "lock_wait_ms": self._sum("lock_wait"),
            "compose_ms": self._sum("compose"),
            "first_update_ms": self._first("first_update"),
            "model_api_ms": model_api_ms,
            "tools_ms": self._sum("tool_call"),
            "total_ms": total,
        }
        out = {"started_at": self.started_at.isoformat().replace("+00:00", "Z"),
               "cold": self.cold, "total_ms": total, "spans": self.spans,
               "summary": {k: v for k, v in summary.items() if v is not None}}
        if self.truncated:
            out["spans_truncated"] = True
        return out

    def summary_line(self, summary: dict[str, Any]) -> str:
        def f(key: str) -> str:
            v = summary.get(key)
            return "-" if v is None else f"{v:.0f}"
        return (f"{'cold' if self.cold else 'warm'} spawn={f('spawn_ms')} "
                f"init={f('initialize_ms')} session={f('session_open_ms')} "
                f"list_tools={f('list_tools_ms')} lock={f('lock_wait_ms')} "
                f"first_update={f('first_update_ms')} model_api={f('model_api_ms')} "
                f"tools={f('tools_ms')} total={f('total_ms')} (ms)")
