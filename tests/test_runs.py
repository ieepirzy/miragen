import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from miragen.models import RunRecord, RunUsage, ToolCallRecord
from miragen.runs import (
    AmbiguousRunIdError,
    RunStore,
    extract_run_details,
    reserved_tokens_in_flight,
    simplify_history_messages,
    tokens_used_since,
)


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

    def test_filters_by_trigger_and_exposes_provenance(self, tmp_path):
        from miragen.models import RunProvenance

        store = RunStore(root=tmp_path)
        managed = store.start(
            agent_name="a",
            trigger="managed",
            prompt="managed fire",
            provenance=RunProvenance(
                trigger_id="sched-1",
                environment_revision="env-rev-1",
                routine_id="routine-1",
            ),
        )
        store.start(agent_name="a", trigger="http", prompt="manual")

        managed_only = store.list(trigger="managed")
        assert [item.run_id for item in managed_only] == [managed.run_id]
        assert managed_only[0].provenance is not None
        assert managed_only[0].provenance.trigger_id == "sched-1"
        assert managed_only[0].trigger == "managed"


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
        result.usage = usage
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


class TestSimplifyHistoryMessages:
    def _part(self, part_kind, **kw):
        p = MagicMock()
        p.part_kind = part_kind
        for k, v in kw.items():
            setattr(p, k, v)
        return p

    def _message(self, kind, parts):
        m = MagicMock()
        m.kind = kind
        m.parts = parts
        return m

    def test_system_and_user_prompt_request(self):
        sys_part = self._part("system-prompt", content="You are helpful.")
        user_part = self._part("user-prompt", content="hi")
        simplified = simplify_history_messages([self._message("request", [sys_part, user_part])])

        assert simplified == [{"role": "system", "content": "You are helpful.\nhi"}]

    def test_user_prompt_only_request(self):
        user_part = self._part("user-prompt", content="hi")
        simplified = simplify_history_messages([self._message("request", [user_part])])

        assert simplified == [{"role": "user", "content": "hi"}]

    def test_tool_return_request(self):
        ret = self._part("tool-return", content="42 degrees")
        simplified = simplify_history_messages([self._message("request", [ret])])

        assert simplified == [{"role": "tool", "content": "42 degrees"}]

    def test_response_text(self):
        text = self._part("text", content="hello there")
        simplified = simplify_history_messages([self._message("response", [text])])

        assert simplified == [{"role": "assistant", "content": "hello there"}]

    def test_response_tool_call(self):
        call = self._part("tool-call", tool_name="get_weather", args={"city": "Turku"})
        simplified = simplify_history_messages([self._message("response", [call])])

        assert simplified[0]["role"] == "assistant"
        assert "get_weather" in simplified[0]["content"]
        assert "Turku" in simplified[0]["content"]

    def test_empty_messages_returns_empty_list(self):
        assert simplify_history_messages([]) == []


class TestTokensUsedSince:
    def _finished_record(self, run_id, started_at, input_tokens, output_tokens):
        return RunRecord(
            run_id=run_id, agent_name="a", trigger="cron", status="succeeded",
            prompt="p", started_at=started_at,
            usage=RunUsage(requests=1, input_tokens=input_tokens, output_tokens=output_tokens),
        )

    def test_empty_store_is_zero(self, tmp_path):
        store = RunStore(root=tmp_path)
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert tokens_used_since(store, since) == 0

    def test_sums_usage_across_matching_records(self, tmp_path):
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        store._write(self._finished_record("1" * 32, base, 100, 50))
        store._write(self._finished_record("2" * 32, base + timedelta(hours=1), 200, 80))

        total = tokens_used_since(store, base)
        assert total == (100 + 50) + (200 + 80)

    def test_excludes_records_before_since(self, tmp_path):
        store = RunStore(root=tmp_path)
        midnight = datetime(2026, 1, 2, tzinfo=timezone.utc)
        yesterday = self._finished_record("1" * 32, midnight - timedelta(hours=1), 1_000, 1_000)
        today = self._finished_record("2" * 32, midnight + timedelta(hours=1), 100, 50)
        store._write(yesterday)
        store._write(today)

        total = tokens_used_since(store, midnight)
        assert total == 150

    def test_record_at_exact_boundary_counts(self, tmp_path):
        store = RunStore(root=tmp_path)
        midnight = datetime(2026, 1, 2, tzinfo=timezone.utc)
        store._write(self._finished_record("1" * 32, midnight, 10, 5))

        assert tokens_used_since(store, midnight) == 15

    def test_records_without_usage_count_as_zero(self, tmp_path):
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        no_usage = RunRecord(
            run_id="1" * 32, agent_name="a", trigger="cron", status="failed",
            prompt="p", started_at=base, error="boom",
        )
        store._write(no_usage)
        store._write(self._finished_record("2" * 32, base + timedelta(seconds=1), 10, 5))

        assert tokens_used_since(store, base) == 15

    def test_input_or_output_tokens_none_counts_as_zero(self, tmp_path):
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = RunRecord(
            run_id="1" * 32, agent_name="a", trigger="cron", status="succeeded",
            prompt="p", started_at=base,
            usage=RunUsage(requests=1, input_tokens=None, output_tokens=20),
        )
        store._write(record)

        assert tokens_used_since(store, base) == 20


class TestReservedTokensInFlight:
    """miragen#58: tokens_used_since only sees usage recorded by finish(), so
    a still-running record must be reserved separately or a burst of
    concurrent run requests can each read the same not-yet-exceeded total."""

    def _running_record(self, run_id, started_at):
        return RunRecord(
            run_id=run_id, agent_name="a", trigger="http_async", status="running",
            prompt="p", started_at=started_at,
        )

    def _finished_record(self, run_id, started_at, input_tokens, output_tokens):
        return RunRecord(
            run_id=run_id, agent_name="a", trigger="cron", status="succeeded",
            prompt="p", started_at=started_at,
            usage=RunUsage(requests=1, input_tokens=input_tokens, output_tokens=output_tokens),
        )

    def _resumed_record(self, run_id, started_at, input_tokens, output_tokens):
        """RunStore.reopen() flips status back to 'running' but keeps the
        usage accumulated across earlier turns -- unlike a fresh running
        record, this one is not usage=None."""
        return RunRecord(
            run_id=run_id, agent_name="a", trigger="http_async", status="running",
            prompt="p", started_at=started_at,
            usage=RunUsage(requests=1, input_tokens=input_tokens, output_tokens=output_tokens),
        )

    def test_no_running_records_is_zero(self, tmp_path):
        store = RunStore(root=tmp_path)
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert reserved_tokens_in_flight(store, since, per_run_reserve=1_000, remaining_budget=5_000) == 0

    def test_finished_records_are_not_reserved(self, tmp_path):
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store._write(self._finished_record("1" * 32, base, 100, 50))

        assert reserved_tokens_in_flight(store, base, per_run_reserve=1_000, remaining_budget=5_000) == 0

    def test_running_records_before_since_are_excluded(self, tmp_path):
        store = RunStore(root=tmp_path)
        midnight = datetime(2026, 1, 2, tzinfo=timezone.utc)
        store._write(self._running_record("1" * 32, midnight - timedelta(hours=1)))

        assert reserved_tokens_in_flight(store, midnight, per_run_reserve=1_000, remaining_budget=5_000) == 0

    def test_reserves_per_run_reserve_per_running_record(self, tmp_path):
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store._write(self._running_record("1" * 32, base))
        store._write(self._running_record("2" * 32, base + timedelta(minutes=1)))
        # A finished record must not add to the reservation on top of the two running ones.
        store._write(self._finished_record("3" * 32, base, 10, 10))

        reserved = reserved_tokens_in_flight(store, base, per_run_reserve=1_000, remaining_budget=50_000)
        assert reserved == 2_000

    def test_no_per_run_reserve_configured_reserves_whole_remaining_budget(self, tmp_path):
        """No tokens_per_run to size the reservation on -- a single in-flight
        run's real cost is unknown and could be anything up to the whole
        budget, so reserve all of it rather than under-reserve."""
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store._write(self._running_record("1" * 32, base))
        store._write(self._running_record("2" * 32, base + timedelta(minutes=1)))

        reserved = reserved_tokens_in_flight(store, base, per_run_reserve=None, remaining_budget=5_000)
        assert reserved == 5_000

    def test_negative_remaining_budget_reserves_zero_not_negative(self, tmp_path):
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store._write(self._running_record("1" * 32, base))

        reserved = reserved_tokens_in_flight(store, base, per_run_reserve=None, remaining_budget=-500)
        assert reserved == 0

    def test_resumed_run_reserves_only_its_remaining_allowance(self, tmp_path):
        """A resumed run already has 500 of a 600 tokens_per_run cap counted
        by tokens_used_since (its usage survives reopen()). Reserving the
        full 600 again here would double-count it and falsely exceed the
        budget for unrelated runs -- only the 100 tokens it could still use
        should be reserved."""
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store._write(self._resumed_record("1" * 32, base, input_tokens=300, output_tokens=200))

        reserved = reserved_tokens_in_flight(store, base, per_run_reserve=600, remaining_budget=50_000)
        assert reserved == 100

    def test_resumed_run_that_already_used_its_full_allowance_reserves_zero(self, tmp_path):
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store._write(self._resumed_record("1" * 32, base, input_tokens=400, output_tokens=250))

        reserved = reserved_tokens_in_flight(store, base, per_run_reserve=600, remaining_budget=50_000)
        assert reserved == 0

    def test_fresh_and_resumed_running_records_combine_correctly(self, tmp_path):
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store._write(self._running_record("1" * 32, base))  # fresh: usage=None, reserves the full cap
        store._write(self._resumed_record("2" * 32, base, input_tokens=500, output_tokens=0))  # reserves 100

        reserved = reserved_tokens_in_flight(store, base, per_run_reserve=600, remaining_budget=50_000)
        assert reserved == 600 + 100

    def test_matches_daily_budget_status_end_to_end_for_a_resumed_run(self, tmp_path):
        """Full miragen#58 + Codex-review scenario together: a profile with
        tokens_per_day=1000 and tokens_per_run=600 has one resumed run that
        already used 500. used (tokens_used_since) must fold in the 100
        remaining reservation, not another full 600, for a total of 600 --
        not 1100."""
        store = RunStore(root=tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store._write(self._resumed_record("1" * 32, base, input_tokens=300, output_tokens=200))

        limit = 1_000
        used = tokens_used_since(store, base)
        used += reserved_tokens_in_flight(store, base, per_run_reserve=600, remaining_budget=limit - used)

        assert used == 600
