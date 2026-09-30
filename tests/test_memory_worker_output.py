"""The memory-worker loop's pacing and log lines (miragen/cli.py): a sweep
that only skipped pre-cutoff backlog runs the next sweep at once, and the
output says when each job's event happened, what it was and where."""

from __future__ import annotations

import re

from miragen.cli import drain_backlog, sweep_lines, utc_stamp


def _skip(n: int, at: str) -> dict:
    return {"job_id": f"j{n}", "status": "skipped_backlog", "event_at": at,
            "source_kind": "harness:claude-code", "scope_id": "group:project.x"}


def test_a_full_sweep_of_backlog_drains_without_waiting():
    full = [_skip(i, f"2026-09-16T10:00:0{i}Z") for i in range(5)]
    assert drain_backlog(full, limit=5)


def test_anything_else_keeps_the_normal_pace():
    backlog = [_skip(i, "2026-09-16T10:00:00Z") for i in range(4)]
    assert not drain_backlog(backlog, limit=5), "a short sweep means the queue is empty"
    assert not drain_backlog(backlog + [{"job_id": "d", "status": "done"}], limit=5)
    assert not drain_backlog(backlog + [{"job_id": "f", "status": "failed"}], limit=5)
    assert not drain_backlog([], limit=5)


def test_skipped_backlog_is_one_line_with_the_event_span():
    lines = sweep_lines([_skip(2, "2026-09-16T10:00:09Z"), _skip(1, "2026-09-16T10:00:01Z")])
    assert lines == ["skipped 2 backlog job(s): events 2026-09-16T10:00:01Z .. 2026-09-16T10:00:09Z"]


def test_processed_and_failed_jobs_say_when_what_and_where():
    lines = sweep_lines([
        {"job_id": "d1", "status": "done", "accepted": 2, "quarantined": 1, "dropped": ["x"],
         "event_at": "2026-09-24T08:00:00Z", "source_kind": "session_episode",
         "scope_id": "group:project.miragen"},
        {"job_id": "d2", "status": "done", "skipped": True,
         "event_at": "2026-09-24T08:01:00Z", "source_kind": "tool_result", "scope_id": "s"},
        {"job_id": "f1", "status": "failed", "error": "usage limit"},
    ])
    assert lines == [
        "job d1 done (session_episode event 2026-09-24T08:00:00Z in group:project.miragen):"
        " +2 accepted, 1 quarantined, 1 dropped",
        "job d2 done (tool_result event 2026-09-24T08:01:00Z in s): nothing to extract",
        "job f1 failed: usage limit",
    ]


def test_the_stamp_is_utc_iso_to_the_second():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", utc_stamp())
