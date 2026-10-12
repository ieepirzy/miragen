"""Budgets count fresh input and output; cache reads don't use them up."""

from datetime import datetime, timezone

from miragen.models import RunUsage, budget_tokens


def test_cache_reads_are_left_out_of_budget_tokens():
    # A cache-heavy turn: 100 fresh, 900 reused from cache, 50 out.
    usage = RunUsage(requests=1, input_tokens=1000, cached_input_tokens=900, output_tokens=50)
    assert budget_tokens(usage) == 150


def test_cache_writes_count_as_fresh_input():
    usage = RunUsage(requests=1, input_tokens=1000, cached_input_tokens=0,
                     cache_write_tokens=800, output_tokens=0)
    assert budget_tokens(usage) == 1000


def test_unknown_usage_is_zero_and_counts_never_go_negative():
    assert budget_tokens(None) == 0
    assert budget_tokens(RunUsage(requests=1)) == 0
    assert budget_tokens(RunUsage(requests=1, input_tokens=10, cached_input_tokens=50)) == 0


def test_daily_budget_sums_budget_tokens(tmp_path):
    from miragen.runs import RunStore, tokens_used_since

    store = RunStore(tmp_path)
    record = store.start(agent_name="a", trigger="http", prompt="p")
    store.finish(record, status="succeeded", usage=RunUsage(
        requests=2, input_tokens=1000, cached_input_tokens=900, output_tokens=50))
    assert tokens_used_since(store, datetime(2000, 1, 1, tzinfo=timezone.utc)) == 150


def _stored(usage: dict, **record) -> "RunRecord":
    from miragen.models import RunRecord

    base = {"run_id": "r" * 32, "agent_name": "a", "trigger": "http", "prompt": "p",
            "status": "succeeded", "started_at": "2026-10-12T00:00:00+00:00", "usage": usage}
    return RunRecord.model_validate({**base, **record})


def test_a_legacy_uncached_record_is_made_inclusive_on_load():
    # Claude Code executor before miragen#189: input_tokens was uncached only.
    record = _stored({"requests": 1, "input_tokens": 1000, "cached_input_tokens": 50000,
                      "output_tokens": 500}, executor="claude-code")
    assert record.usage.input_tokens == 51000
    assert record.usage.input_convention == "inclusive"
    harness = _stored({"requests": 1, "input_tokens": 10, "cached_input_tokens": 5},
                      model="claude-code:sonnet")
    assert harness.usage.input_tokens == 15


def test_a_legacy_codex_record_was_already_inclusive():
    record = _stored({"requests": 1, "input_tokens": 1000, "cached_input_tokens": 800},
                     executor="codex")
    assert record.usage.input_tokens == 1000


def test_cache_reads_above_input_mark_a_row_uncached_whoever_wrote_it():
    record = _stored({"requests": 1, "input_tokens": 100, "cached_input_tokens": 900})
    assert record.usage.input_tokens == 1000


def test_a_new_record_round_trips_unchanged():
    from miragen.models import RunRecord

    record = _stored({"requests": 1, "input_tokens": 1000, "cached_input_tokens": 50000 - 49100,
                      "output_tokens": 1, "input_convention": "inclusive"}, executor="claude-code")
    again = RunRecord.model_validate_json(record.model_dump_json())
    assert again.usage.input_tokens == 1000


def test_a_resumed_run_over_a_legacy_record_keeps_its_fresh_input_in_the_budget():
    from miragen.models import sum_usage

    legacy = _stored({"requests": 1, "input_tokens": 1000, "cached_input_tokens": 50000,
                      "output_tokens": 500}, executor="claude-code").usage
    resumed = RunUsage(requests=1, input_tokens=60000, cached_input_tokens=55000,
                       output_tokens=1000)
    # fresh: 1000 (legacy) + 5000 (new) input, 1500 output
    assert budget_tokens(sum_usage(legacy, resumed)) == 7500


def test_sum_usage_keeps_a_reported_zero():
    from miragen.models import sum_usage

    zero = RunUsage(requests=1, input_tokens=0, cached_input_tokens=0, output_tokens=0)
    total = sum_usage(zero, zero)
    assert (total.input_tokens, total.cached_input_tokens, total.output_tokens) == (0, 0, 0)
    unknown = sum_usage(RunUsage(requests=1), RunUsage(requests=1))
    assert unknown.cached_input_tokens is None
