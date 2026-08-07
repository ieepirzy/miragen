"""Durable per-run event streams — the tier-neutral half of the contract.

One append-only `<run_id>.events.jsonl` per run under the run store root,
shared by BOTH backend tiers: executor adapters stream their native events
into it during a turn, and model-tier runs write their turn's events after
`Agent.run()` completes (walked from the same messages that produce the
run record's tool-call trace). `GET /runs/{id}/events` reads either with
the same envelope, sequence, and cursor semantics — a projector never needs
to know which tier produced a stream.

Envelope (issue #33 Phase C, unchanged): every persisted event carries
`seq` (per-run monotonic, 1-based), `schema`, `ts`, and `type` alongside its
payload fields. The envelope is flat — new keys on the same JSONL object —
so existing tail readers keep working unchanged.

This module was extracted from miragen/executor/base.py when the event log
stopped being executor-only; base re-exports the old names for existing
importers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from miragen.models import RunUsage, ToolCallRecord

EVENT_SCHEMA = "miragen/executor-event/v1"

EVENTS_SUFFIX = ".events.jsonl"


def events_path(root: Path, run_id: str) -> Path:
    """The run's event stream file. Both tiers use the run store root, so
    executor- and model-tier streams live in the same namespace."""
    return Path(root) / f"{run_id}{EVENTS_SUFFIX}"


def iter_event_lines(path: Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    """Yield (line_no, parsed | None) for every non-blank line. Unparsable
    lines yield None but still occupy a line number, so sequence assignment
    stays stable no matter who reads the file."""
    with path.open() as f:
        line_no = 0
        for line in f:
            line = line.strip()
            if not line:
                continue
            line_no += 1
            try:
                yield line_no, json.loads(line)
            except ValueError:
                yield line_no, None


def effective_seq(line_no: int, event: dict[str, Any]) -> int:
    """Explicit seq when the writer stamped one; the 1-based line number for
    legacy pre-envelope lines (writers have always been append-only and
    single-writer per run, so line order IS event order)."""
    seq = event.get("seq")
    return seq if isinstance(seq, int) else line_no


class EventWriter:
    """Append-only events.jsonl sink owning the per-run monotonic sequence.

    On open, scans the existing file (resume case) and continues numbering
    after the last effective sequence — a resumed turn's events extend the
    same ordered stream rather than restarting at 1. Each write is flushed so
    cursor/tail readers and crash forensics see events promptly.
    """

    def __init__(self, path: Path):
        self.path = path
        self._last_seq = last_seq(path)
        self._fh = path.open("a")

    def write(self, payload: dict[str, Any]) -> None:
        self._last_seq += 1
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        payload["seq"] = self._last_seq
        payload["schema"] = EVENT_SCHEMA
        self._fh.write(json.dumps(payload, default=str) + "\n")
        self._fh.flush()

    def __enter__(self) -> "EventWriter":
        return self

    def __exit__(self, *exc) -> None:
        self._fh.close()


def last_seq(path: Path) -> int:
    """Last effective sequence in the stream (0 for a missing/empty file) —
    the cursor a caller records before a turn to slice out that turn's
    events afterwards."""
    seq = 0
    if path.exists():
        for line_no, event in iter_event_lines(path):
            seq = effective_seq(line_no, event) if event is not None else line_no
    return seq


@dataclass
class EventPage:
    """One page of a cursor read: events with seq > `after`, in order.
    Replay contract: (run_id, seq) is the deduplication key; re-reading any
    cursor returns the same events."""

    events: list[dict[str, Any]] = field(default_factory=list)
    next_after: int = 0  # pass as `after` to get the next page
    has_more: bool = False


def parsed_events(path: Path) -> list[dict[str, Any]]:
    """All parseable events with an effective `seq` stamped — explicit for
    envelope-era lines, line-number-derived for legacy pre-envelope files,
    so replay/dedup by (run_id, seq) works across both."""
    if not path.exists():
        return []
    events = []
    for line_no, event in iter_event_lines(path):
        if event is None:
            continue
        event.setdefault("seq", effective_seq(line_no, event))
        events.append(event)
    return events


def read_events(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Tail read (newest `limit` events, in order) — the original polling
    contract, preserved."""
    return parsed_events(path)[-limit:]


def read_events_page(path: Path, *, after: int = 0, limit: int = 200) -> EventPage:
    """Cursor read: up to `limit` events with seq > `after`, oldest first.
    Feed `next_after` back as `after` to continue; a cursor past the end
    returns an empty page with has_more=False. Reads are pure — replaying
    the same cursor yields the same page, and a projector can rebuild its
    projection from after=0 at any time."""
    newer = [e for e in parsed_events(path) if e["seq"] > after]
    page = newer[:limit]
    return EventPage(
        events=page,
        next_after=page[-1]["seq"] if page else after,
        has_more=len(newer) > limit,
    )


def append_event(path: Path, payload: dict[str, Any]) -> None:
    """Append one event to a run's durable stream from outside a turn. Safe
    because a finished/suspended run has no writer holding the file; the
    sequence continues from the last effective seq."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with EventWriter(path) as sink:
        sink.write(payload)


# ── Model-tier emission ──────────────────────────────────────────────────────
#
# The executor tier streams events live; the model tier owns the whole loop
# through pydantic-ai and only sees the finished result — so its stream is
# written once, after the turn, from the run's extracted details. Post-hoc,
# not live, but the same vocabulary: item.completed per tool call,
# turn.completed with usage, turn.failed with the error. lifecycle.* setup
# events don't apply (there is no workspace to prepare).


def write_model_turn_events(
    path: Path,
    *,
    tool_calls: list[ToolCallRecord] = (),
    usage: RunUsage | None = None,
    output: str | None = None,
    error: str | None = None,
) -> None:
    """Write one model-tier turn into the run's event stream. `error` set
    means the turn failed (turn.failed instead of turn.completed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with EventWriter(path) as sink:
        for call in tool_calls:
            sink.write({
                "type": "item.completed",
                "item": {
                    "type": "tool_call",
                    "name": call.tool_name,
                    "args": call.args,
                    "status": "completed" if call.ok else "failed",
                },
            })
        if output is not None:
            sink.write({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": output},
            })
        if error is not None:
            sink.write({"type": "turn.failed", "error": {"message": error}})
        else:
            sink.write({
                "type": "turn.completed",
                "usage": {
                    "input_tokens": usage.input_tokens if usage else None,
                    "output_tokens": usage.output_tokens if usage else None,
                    "cached_input_tokens": usage.cached_input_tokens if usage else None,
                }
                if usage
                else None,
            })
