"""The retrieval judgment log's own contract: private files, bounded disk."""

from __future__ import annotations

import json
import os
import stat
import time

from miragen.daemon.sessions.judgments import JudgmentLog


def test_rows_are_private_and_labelled(tmp_path):
    log = JudgmentLog(tmp_path / "j")
    log.record(session="s", scope="g", recall_id="s#1", query="q",
               cards=[{"record_id": "a", "payload": {"text": "A"}},
                      {"record_id": "b", "payload": {"text": "B"}}],
               selected=[("b", "applies")], status="ok", model="claude-code:haiku")
    (path,) = (tmp_path / "j").iterdir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "j").stat().st_mode) == 0o700
    row = json.loads(path.read_text())
    assert [(c["record_id"], c["rank"], c["selected"], c["reason"]) for c in row["candidates"]] == [
        ("a", 1, False, None), ("b", 2, True, "applies")]
    assert log.describe()["written"] == 1


def test_old_files_age_out_and_the_total_stays_under_the_cap(tmp_path):
    directory = tmp_path / "j"
    directory.mkdir()
    old = directory / "judgments-2026-01-01.jsonl"
    old.write_text("x\n")
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    for day in ("2026-09-20", "2026-09-21", "2026-09-22"):
        (directory / f"judgments-{day}.jsonl").write_text("y" * 400)
    log = JudgmentLog(directory, retention_days=30, max_bytes=900)
    log.prune()
    remaining = sorted(p.name for p in directory.iterdir())
    assert "judgments-2026-01-01.jsonl" not in remaining
    assert remaining == ["judgments-2026-09-21.jsonl", "judgments-2026-09-22.jsonl"]


def test_a_write_failure_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    log = JudgmentLog(blocker / "j")  # a directory under a file cannot exist
    log.record(session="s", scope="g", recall_id="s#1", query="q", cards=[], selected=[],
               status="ok", model=None)
    assert log.failures == 1 and log.written == 0
