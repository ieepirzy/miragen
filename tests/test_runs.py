import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from miragen.models import RunRecord, RunUsage, ToolCallRecord
from miragen.runs import AmbiguousRunIdError, RunStore, extract_run_details


class TestRunStoreStartFinish:
    def test_start_writes_running_record(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="cron", prompt="hi")

        assert record.status == "running"
        assert record.agent_name == "a"
        assert record.trigger == "cron"
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1

    def test_start_truncates_long_prompt(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="http", prompt="x" * 25_000)
        assert len(record.prompt) == 20_000

    def test_finish_overwrites_same_file(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="cron", prompt="hi")
        store.finish(record, status="succeeded", output="done")

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1

    def test_finish_sets_terminal_fields(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="cron", prompt="hi")
        updated = store.finish(
            record,
            status="succeeded",
            output="the output",
            usage=RunUsage(requests=2, input_tokens=100, output_tokens=50),
            tool_calls=[ToolCallRecord(tool_name="get_weather", args="{}", ok=True)],
        )

        assert updated.status == "succeeded"
        assert updated.output == "the output"
        assert updated.finished_at is not None
        assert updated.duration_s is not None
        assert updated.duration_s >= 0
        assert updated.usage.requests == 2
        assert len(updated.tool_calls) == 1

    def test_finish_truncates_long_output(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="cron", prompt="hi")
        updated = store.finish(record, status="succeeded", output="y" * 150_000)
        assert len(updated.output) == 100_000

    def test_finish_failed_with_error(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="cron", prompt="hi")
        updated = store.finish(record, status="failed", error="boom")
        assert updated.status == "failed"
        assert updated.error == "boom"
        assert updated.output is None

    def test_write_is_atomic_no_tmp_left_behind(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="cron", prompt="hi")
        store.finish(record, status="succeeded", output="done")
        assert list(tmp_path.glob("*.tmp")) == []


class TestRunStoreGet:
    def test_get_by_full_id(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="cron", prompt="hi")
        fetched = store.get(record.run_id)
        assert fetched.run_id == record.run_id

    def test_get_by_unique_prefix(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="cron", prompt="hi")
        fetched = store.get(record.run_id[:8])
        assert fetched.run_id == record.run_id

    def test_get_unknown_returns_none(self, tmp_path):
        store = RunStore(root=tmp_path)
        assert store.get("deadbeef") is None

    def test_get_ambiguous_prefix_raises(self, tmp_path):
        store = RunStore(root=tmp_path)
        # Two run_ids sharing an 8-char prefix, written directly with distinct
        # started_at timestamps so their filenames don't collide.
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        r1 = RunRecord(
            run_id="aaaaaaaa1111111111111111111111aa",
            agent_name="a", trigger="cron", status="succeeded",
            prompt="1", started_at=base,
        )
        r2 = RunRecord(
            run_id="aaaaaaaa2222222222222222222222bb",
            agent_name="a", trigger="cron", status="succeeded",
            prompt="2", started_at=base + timedelta(seconds=1),
        )
        store._write(r1)
        store._write(r2)

        with pytest.raises(AmbiguousRunIdError) as exc_info:
            store.get("aaaaaaaa")
        assert len(exc_info.value.candidates) == 2

    def test_get_on_empty_store_returns_none(self, tmp_path):
        store = RunStore(root=tmp_path / "does-not-exist")
        assert store.get("anything") is None


class TestRunStoreList:
    def test_empty_store(self, tmp_path):
        store = RunStore(root=tmp_path)
        assert store.list() == []

    def test_newest_first(self, tmp_path):
        # Filenames carry second-precision timestamps, so construct records with
        # explicit distinct started_at values rather than relying on wall-clock
        # gaps between two start() calls milliseconds apart.
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        r1 = RunRecord(
            run_id="1" * 32, agent_name="a", trigger="cron", status="running",
            prompt="first", started_at=base,
        )
        r2 = RunRecord(
            run_id="2" * 32, agent_name="a", trigger="cron", status="running",
            prompt="second", started_at=base + timedelta(seconds=1),
        )
        store.finish(r1, status="succeeded", output="1")
        store.finish(r2, status="succeeded", output="2")

        summaries = store.list()
        assert [s.run_id for s in summaries] == [r2.run_id, r1.run_id]

    def test_respects_limit(self, tmp_path):
        store = RunStore(root=tmp_path)
        for i in range(5):
            r = store.start(agent_name="a", trigger="cron", prompt=f"run {i}")
            store.finish(r, status="succeeded", output="ok")
        assert len(store.list(limit=2)) == 2

    def test_filters_by_status(self, tmp_path):
        store = RunStore(root=tmp_path)
        ok = store.start(agent_name="a", trigger="cron", prompt="ok")
        store.finish(ok, status="succeeded", output="done")
        bad = store.start(agent_name="a", trigger="cron", prompt="bad")
        store.finish(bad, status="failed", error="boom")

        succeeded = store.list(status="succeeded")
        assert len(succeeded) == 1
        assert succeeded[0].run_id == ok.run_id

    def test_summary_has_previews_not_full_text(self, tmp_path):
        store = RunStore(root=tmp_path)
        r = store.start(agent_name="a", trigger="cron", prompt="x" * 500)
        store.finish(r, status="succeeded", output="y" * 500)
        summary = store.list()[0]
        assert len(summary.prompt_preview) == 200
        assert len(summary.output_preview) == 200


class TestRunStoreRetention:
    def test_prunes_oldest_beyond_retention(self, tmp_path):
        store = RunStore(root=tmp_path, retention=5)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        records = []
        for i in range(6):
            r = RunRecord(
                run_id=f"{i:032x}",
                agent_name="a", trigger="cron", status="running",
                prompt=f"run {i}", started_at=base + timedelta(seconds=i),
            )
            updated = store.finish(r, status="succeeded", output=f"out {i}")
            records.append(updated)

        files = sorted(tmp_path.glob("*.json"))
        assert len(files) == 5
        # The oldest (first) run's file should be gone; the newest remains.
        remaining_ids = {store.get(r.run_id).run_id for r in records if store.get(r.run_id) is not None}
        assert records[0].run_id not in remaining_ids
        assert records[-1].run_id in remaining_ids


class TestRunStoreSweepInterrupted:
    def test_marks_running_as_interrupted(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="cron", prompt="hi")

        count = store.sweep_interrupted()

        assert count == 1
        fetched = store.get(record.run_id)
        assert fetched.status == "interrupted"
        assert fetched.finished_at is not None

    def test_does_not_touch_terminal_records(self, tmp_path):
        store = RunStore(root=tmp_path)
        record = store.start(agent_name="a", trigger="cron", prompt="hi")
        store.finish(record, status="succeeded", output="done")

        count = store.sweep_interrupted()

        assert count == 0
        assert store.get(record.run_id).status == "succeeded"

    def test_empty_store_returns_zero(self, tmp_path):
        store = RunStore(root=tmp_path)
        assert store.sweep_interrupted() == 0


class TestExtractRunDetails:
    def _mock_result(self, usage, messages):
        result = MagicMock()
        result.usage.return_value = usage
        result.all_messages.return_value = messages
        return result

    def _usage(self, requests=1, input_tokens=10, output_tokens=5):
        u = MagicMock()
        u.requests = requests
        u.input_tokens = input_tokens
        u.output_tokens = output_tokens
        return u

    def _part(self, part_kind, **kw):
        p = MagicMock()
        p.part_kind = part_kind
        for k, v in kw.items():
            setattr(p, k, v)
        return p

    def _message(self, parts):
        m = MagicMock()
        m.parts = parts
        return m

    def test_usage_extracted(self):
        result = self._mock_result(self._usage(requests=3, input_tokens=200, output_tokens=80), [])
        usage, _ = extract_run_details(result)
        assert usage.requests == 3
        assert usage.input_tokens == 200
        assert usage.output_tokens == 80

    def test_successful_tool_call_recorded(self):
        call = self._part("tool-call", tool_call_id="c1", tool_name="get_weather", args={"city": "Turku"})
        ret = self._part("tool-return", tool_call_id="c1", outcome="success")
        result = self._mock_result(self._usage(), [self._message([call]), self._message([ret])])

        _, tool_calls = extract_run_details(result)

        assert len(tool_calls) == 1
        assert tool_calls[0].tool_name == "get_weather"
        assert tool_calls[0].ok is True
        assert json.loads(tool_calls[0].args) == {"city": "Turku"}

    def test_denied_tool_call_recorded_as_not_ok(self):
        call = self._part("tool-call", tool_call_id="c1", tool_name="delete_file", args={})
        ret = self._part("tool-return", tool_call_id="c1", outcome="denied")
        result = self._mock_result(self._usage(), [self._message([call, ret])])

        _, tool_calls = extract_run_details(result)

        assert tool_calls[0].ok is False

    def test_retry_prompt_marks_not_ok(self):
        call = self._part("tool-call", tool_call_id="c1", tool_name="risky", args={})
        retry = self._part("retry-prompt", tool_call_id="c1")
        result = self._mock_result(self._usage(), [self._message([call, retry])])

        _, tool_calls = extract_run_details(result)

        assert tool_calls[0].ok is False

    def test_args_truncated(self):
        call = self._part("tool-call", tool_call_id="c1", tool_name="t", args="x" * 3000)
        result = self._mock_result(self._usage(), [self._message([call])])

        _, tool_calls = extract_run_details(result)

        assert len(tool_calls[0].args) == 2000

    def test_no_calls_returns_empty_list(self):
        result = self._mock_result(self._usage(), [])
        _, tool_calls = extract_run_details(result)
        assert tool_calls == []
