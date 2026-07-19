"""Sequenced event envelope + cursor replay (issue #33 Phase C) — per-run
monotonic sequence, schema-versioned envelope, legacy-file compatibility,
resume continuation, page boundaries, and the HTTP cursor contract.
"""

import json
import sys

import pytest
from httpx import ASGITransport, AsyncClient

import miragen.app  # noqa: F401 — ensure module is registered in sys.modules

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.executor.base import EVENT_SCHEMA, _EventWriter
from miragen.runs import RunStore

from tests.test_executor import StubThread, _executor_profile, default_events, make_executor


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._executor = None


# ── Envelope ─────────────────────────────────────────────────────────────────


async def test_every_event_carries_envelope_fields(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    await executor.run_job("task", "run-env")
    events = executor.read_events("run-env")
    assert events, "expected events"
    for event in events:
        assert isinstance(event["seq"], int)
        assert event["schema"] == EVENT_SCHEMA
        assert "ts" in event and "type" in event
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))


async def test_lifecycle_setup_and_harvest_timing_events(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    await executor.run_job("task", "run-lc")
    by_type = {e["type"]: e for e in executor.read_events("run-lc")}
    assert by_type["lifecycle.setup.started"]["first_turn"] is True
    assert by_type["lifecycle.setup.completed"]["duration_ms"] >= 0
    assert by_type["lifecycle.setup.completed"]["phase"] == "workspace"
    assert by_type["lifecycle.harvest.completed"]["diff_bytes"] >= 0


async def test_resume_continues_the_same_sequence(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    result = await executor.run_job("first", "run-resume")
    first_count = len(executor.read_events("run-resume"))

    await executor.run_job(
        "again", "run-resume", thread_id=result.thread_id, first_turn=False,
    )
    events = executor.read_events("run-resume", limit=1000)
    # one uninterrupted monotonic stream across turns — no restart at 1
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert len(events) > first_count


# ── Legacy compatibility ─────────────────────────────────────────────────────


def _write_legacy_events(runs_root, run_id, count=5):
    """Pre-envelope events.jsonl: no seq, no schema — how existing runs on
    disk look."""
    path = runs_root / f"{run_id}.events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(json.dumps({"type": "thread.started", "thread_id": "thr_old", "ts": "t0"}) + "\n")
        for i in range(count - 2):
            f.write(json.dumps({"type": "item.completed", "item": {"n": i}, "ts": f"t{i + 1}"}) + "\n")
        f.write(json.dumps({"type": "turn.completed", "usage": {}, "ts": "tN"}) + "\n")
    return path


async def test_legacy_files_get_line_derived_sequences(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    _write_legacy_events(executor.runs_root, "run-legacy", count=5)
    events = executor.read_events("run-legacy")
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]
    page = executor.read_events_page("run-legacy", after=2, limit=10)
    assert [e["seq"] for e in page.events] == [3, 4, 5]


async def test_writer_continues_after_legacy_lines(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    _write_legacy_events(executor.runs_root, "run-mixed", count=5)
    await executor.run_job(
        "resume old run", "run-mixed", thread_id="thr_old", first_turn=False,
    )
    events = executor.read_events("run-mixed", limit=1000)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    # the new envelope-era events carry explicit seqs continuing the stream
    assert events[5]["schema"] == EVENT_SCHEMA and events[5]["seq"] == 6


def test_unparsable_lines_occupy_a_sequence_slot(tmp_path):
    path = tmp_path / "x.events.jsonl"
    with path.open("w") as f:
        f.write(json.dumps({"type": "a"}) + "\n")
        f.write("NOT JSON{{{\n")
        f.write(json.dumps({"type": "b"}) + "\n")
    writer = _EventWriter(path)
    try:
        writer.write({"type": "c"})
    finally:
        writer._fh.close()
    lines = [json.loads(l) for l in path.read_text().splitlines() if not l.startswith("NOT")]
    assert lines[-1]["seq"] == 4  # a=1, garbage=2, b=3, c=4


# ── Cursor paging ────────────────────────────────────────────────────────────


async def test_cursor_replay_pages_and_is_idempotent(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    await executor.run_job("task", "run-page")
    all_events = executor.read_events("run-page", limit=1000)
    assert len(all_events) >= 5

    replayed, after = [], 0
    for _ in range(50):
        page = executor.read_events_page("run-page", after=after, limit=2)
        if not page.events:
            assert page.has_more is False
            break
        assert len(page.events) <= 2
        replayed.extend(page.events)
        # replaying the SAME cursor returns the same page — (run_id, seq) dedup
        again = executor.read_events_page("run-page", after=after, limit=2)
        assert [e["seq"] for e in again.events] == [e["seq"] for e in page.events]
        after = page.next_after
    assert [e["seq"] for e in replayed] == [e["seq"] for e in all_events]


async def test_cursor_past_end_and_boundaries(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    await executor.run_job("task", "run-bound")
    total = len(executor.read_events("run-bound", limit=1000))

    exact = executor.read_events_page("run-bound", after=0, limit=total)
    assert len(exact.events) == total and exact.has_more is False
    assert exact.next_after == total

    past = executor.read_events_page("run-bound", after=total + 100, limit=10)
    assert past.events == [] and past.has_more is False
    assert past.next_after == total + 100  # cursor is stable, not rewound

    empty = executor.read_events_page("run-none", after=0, limit=10)
    assert empty.events == [] and empty.has_more is False and empty.next_after == 0


# ── HTTP contract ────────────────────────────────────────────────────────────


@pytest.fixture
async def executor_client(tmp_path):
    profile = _executor_profile()
    executor = make_executor(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_events_endpoint_cursor_mode(executor_client):
    run_id = (await executor_client.post("/run", json={"prompt": "go"})).json()["run_id"]

    replayed, after = [], 0
    for _ in range(50):
        body = (await executor_client.get(
            f"/runs/{run_id}/events", params={"after": after, "limit": 3}
        )).json()
        if not body["events"]:
            assert body["has_more"] is False
            break
        replayed.extend(body["events"])
        after = body["next_after"]

    tail = (await executor_client.get(f"/runs/{run_id}/events")).json()
    # cursor replay reconstructs exactly the stream the tail read shows
    assert [e["seq"] for e in replayed] == [e["seq"] for e in tail["events"]]
    # tail mode keeps the original shape (no cursor fields)
    assert "next_after" not in tail and "count" in tail


async def test_events_endpoint_after_restart_replays_from_any_cursor(executor_client, tmp_path):
    """A projector that lost its state can rebuild from after=0; one that
    kept a checkpoint resumes mid-stream — same durable file serves both."""
    run_id = (await executor_client.post("/run", json={"prompt": "go"})).json()["run_id"]
    full = (await executor_client.get(f"/runs/{run_id}/events", params={"after": 0, "limit": 1000})).json()

    # simulate projector restart: new read from a mid-stream checkpoint
    checkpoint = full["events"][2]["seq"]
    resumed = (await executor_client.get(
        f"/runs/{run_id}/events", params={"after": checkpoint, "limit": 1000}
    )).json()
    assert [e["seq"] for e in resumed["events"]] == [e["seq"] for e in full["events"][3:]]
