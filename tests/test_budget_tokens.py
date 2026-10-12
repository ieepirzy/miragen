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
