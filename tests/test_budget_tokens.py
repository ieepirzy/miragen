"""Budget tokens: uncached input and output count 1:1; cache reads and writes
are weighted by the model's own rates from the configured source, and count
in full whenever those rates can't weight them."""

import logging
from datetime import datetime, timezone

import pytest

from miragen import model_rates
from miragen.models import AgentProfile, CallUsage, RunUsage, budget_model, budget_tokens
from miragen.model_rates import StaticRatesSource, parse_rates_document

TABLE = parse_rates_document({"models": {
    # reads 0.05, 5-minute writes 1.25, 1-hour writes 2
    "anthropic:claude-x": {"input": "2.00", "cached_input": "0.10", "cache_write": "2.50",
                           "cache_write_1h": "4.00", "output": "10.00"},
    # reads 0.5, no write rates
    "openai:gpt-x": {"input": "2.50", "cached_input": "1.25", "output": "10.00"},
    # reads 0.02
    "deepseek:ds": {"input": "0.15", "cached_input": "0.003", "output": "0.60"},
    # a flat-rate subscription harness: rated at zero
    "harness:sub": {"input": "0", "cached_input": "0", "output": "0"},
    # a harness whose catalogue serves the API model's rates for it
    "harness:fronted": {"input": "2.00", "cached_input": "0.10", "output": "10.00"},
}})

# 100 uncached, 900 cache reads, 50 out.
CACHE_HEAVY = RunUsage(requests=1, input_tokens=1000, cached_input_tokens=900, output_tokens=50)


# ── fallback: never undercount ──────────────────────────────────────────────


def test_without_a_rates_source_cache_reads_count_in_full(caplog):
    with caplog.at_level(logging.WARNING, logger="miragen.model_rates"):
        assert budget_tokens(CACHE_HEAVY, "anthropic:claude-x") == 1050
        assert budget_tokens(CACHE_HEAVY, "anthropic:claude-x") == 1050
    assert sum("no model rates source" in r.getMessage() for r in caplog.records) == 1


def test_an_unlisted_model_counts_cache_reads_in_full(caplog):
    with caplog.at_level(logging.WARNING, logger="miragen.model_rates"):
        assert budget_tokens(CACHE_HEAVY, "acme:unlisted", rates=TABLE) == 1050
        assert budget_tokens(CACHE_HEAVY, "acme:unlisted", rates=TABLE) == 1050
        assert budget_tokens(CACHE_HEAVY, None, rates=TABLE) == 1050
    warned = [r.getMessage() for r in caplog.records if "no usable model rates" in r.getMessage()]
    assert len(warned) == 2 and "acme:unlisted" in warned[0]  # once per model


def test_a_model_rated_at_zero_counts_cache_reads_in_full():
    assert budget_tokens(CACHE_HEAVY, "harness:sub", rates=TABLE) == 1050
    writes = RunUsage(requests=1, input_tokens=1000, cache_write_tokens=800,
                      cache_write_1h_tokens=300)
    assert budget_tokens(writes, "harness:sub", rates=TABLE) == 1000


def test_the_configured_source_is_what_budgets_read():
    model_rates.configure(StaticRatesSource(TABLE))
    assert budget_tokens(CACHE_HEAVY, "anthropic:claude-x") == 195
    model_rates.configure(None)
    assert budget_tokens(CACHE_HEAVY, "anthropic:claude-x") == 1050


def test_a_failing_source_counts_in_full():
    class Broken:
        def current(self):
            raise RuntimeError("boom")

    model_rates.configure(Broken())
    assert budget_tokens(CACHE_HEAVY, "anthropic:claude-x") == 1050


# ── weights from the model's rates ──────────────────────────────────────────


@pytest.mark.parametrize("model,expected", [
    ("anthropic:claude-x", 100 + 45 + 50),   # 900 × 0.10/2.00
    ("openai:gpt-x", 100 + 450 + 50),        # 900 × 1.25/2.50
    ("deepseek:ds", 100 + 18 + 50),          # 900 × 0.003/0.15
    ("harness:fronted", 100 + 45 + 50),
])
def test_cache_reads_weigh_cached_input_over_input(model, expected):
    assert budget_tokens(CACHE_HEAVY, model, rates=TABLE) == expected


def test_cache_writes_weigh_their_own_rates_by_ttl():
    # 200 uncached, 500 five-minute writes (×1.25), 300 one-hour writes (×2)
    usage = RunUsage(requests=1, input_tokens=1000, cache_write_tokens=800,
                     cache_write_1h_tokens=300)
    assert budget_tokens(usage, "anthropic:claude-x", rates=TABLE) == 200 + 625 + 600


def test_cache_writes_with_an_unknown_split_take_the_dearer_weight():
    usage = RunUsage(requests=1, input_tokens=1000, cache_write_tokens=800)
    assert budget_tokens(usage, "anthropic:claude-x", rates=TABLE) == 200 + 1600


def test_cache_writes_without_a_rate_count_one_to_one():
    usage = RunUsage(requests=1, input_tokens=1000, cache_write_tokens=800,
                     cache_write_1h_tokens=300)
    assert budget_tokens(usage, "openai:gpt-x", rates=TABLE) == 1000
    assert budget_tokens(usage) == 1000  # no source at all


def test_a_one_hour_rate_falls_back_to_the_five_minute_rate():
    table = parse_rates_document({"models": {"p:m": {
        "input": "1", "cached_input": "0.1", "cache_write": "1.5", "output": "2"}}})
    usage = RunUsage(requests=1, input_tokens=100, cache_write_tokens=100,
                     cache_write_1h_tokens=100)
    assert budget_tokens(usage, "p:m", rates=table) == 150


def test_unknown_usage_is_zero_and_counts_never_go_negative():
    assert budget_tokens(None) == 0
    assert budget_tokens(RunUsage(requests=1)) == 0
    # Reads above input (stored rows are normalised on load; live usage is
    # not) still count, never cancel output.
    assert budget_tokens(RunUsage(requests=1, input_tokens=10, cached_input_tokens=50)) == 50
    assert budget_tokens(RunUsage(requests=1, input_tokens=10, cached_input_tokens=50),
                         "anthropic:claude-x", rates=TABLE) == 3


def test_the_result_is_an_int_rounded_up():
    usage = RunUsage(requests=1, input_tokens=1, cached_input_tokens=1)
    result = budget_tokens(usage, "anthropic:claude-x", rates=TABLE)
    assert result == 1 and isinstance(result, int)


# ── per-call weighting ──────────────────────────────────────────────────────


def _calls(*calls, **totals):
    return RunUsage(requests=len(calls), calls=list(calls), **totals)


def test_each_call_is_weighted_by_its_own_model():
    usage = _calls(
        CallUsage(model="claude-x-20260901", input_tokens=1050, cached_input_tokens=1000),
        CallUsage(model="gpt-x", input_tokens=1050, cached_input_tokens=1000),
        input_tokens=2100, cached_input_tokens=2000, output_tokens=10,
    )
    # 100 uncached + 10 out; claude-x 1000 × 0.05, gpt-x (openai entry) 1000 × 0.5
    assert budget_tokens(usage, "anthropic:claude-x", rates=TABLE) == 110 + 50 + 500


def test_each_call_rounds_up_on_its_own():
    usage = _calls(
        CallUsage(model="claude-x", input_tokens=1, cached_input_tokens=1),
        CallUsage(model="claude-x", input_tokens=1, cached_input_tokens=1),
        input_tokens=2, cached_input_tokens=2,
    )
    assert budget_tokens(usage, "anthropic:claude-x", rates=TABLE) == 2


def test_an_unlisted_call_model_counts_in_full_even_under_a_listed_run_model():
    usage = _calls(CallUsage(model="mystery-1", input_tokens=1000, cached_input_tokens=900),
                   input_tokens=1000, cached_input_tokens=900, output_tokens=50)
    assert budget_tokens(usage, "anthropic:claude-x", rates=TABLE) == 1050


def test_a_call_without_a_model_uses_the_run_model():
    usage = _calls(CallUsage(input_tokens=1000, cached_input_tokens=900),
                   input_tokens=1000, cached_input_tokens=900, output_tokens=50)
    assert budget_tokens(usage, "anthropic:claude-x", rates=TABLE) == 195


def test_usage_the_calls_do_not_cover_is_weighted_by_the_run_model():
    # Truncated breakdown: the calls cover 1000 of 3000 reads.
    usage = _calls(CallUsage(model="gpt-x", input_tokens=1000, cached_input_tokens=1000),
                   input_tokens=3100, cached_input_tokens=3000, output_tokens=0)
    usage.calls_truncated = True
    # 100 uncached; 1000 × 0.5 (gpt-x); 2000 × 0.05 (run model)
    assert budget_tokens(usage, "anthropic:claude-x", rates=TABLE) == 100 + 500 + 100


def test_per_call_write_splits_are_weighted_per_call():
    usage = _calls(
        CallUsage(model="claude-x", input_tokens=500, cache_write_tokens=500,
                  cache_write_1h_tokens=0),
        CallUsage(model="claude-x", input_tokens=300, cache_write_tokens=300,
                  cache_write_1h_tokens=300),
        input_tokens=800, cache_write_tokens=800, cache_write_1h_tokens=300,
    )
    assert budget_tokens(usage, "anthropic:claude-x", rates=TABLE) == 625 + 600


# ── matching a reported model to an entry ───────────────────────────────────


def test_a_reported_bare_id_matches_the_run_models_prefixed_entry():
    usage = _calls(CallUsage(model="ds", input_tokens=1000, cached_input_tokens=1000),
                   input_tokens=1000, cached_input_tokens=1000)
    assert budget_tokens(usage, "deepseek:ds", rates=TABLE) == 20


def test_a_harness_reporting_the_api_model_behind_its_selector_matches_that_entry():
    usage = _calls(CallUsage(model="claude-x-20260901", input_tokens=1000,
                             cached_input_tokens=1000),
                   input_tokens=1000, cached_input_tokens=1000)
    assert budget_tokens(usage, "harness:sub", rates=TABLE) == 50


def test_several_matching_entries_take_the_highest_weights():
    table = parse_rates_document({"models": {
        "a:m": {"input": "1", "cached_input": "0.05", "output": "1"},
        "b:m": {"input": "1", "cached_input": "0.5", "output": "1"},
        "z:m": {"input": "0", "cached_input": "0", "output": "0"},
    }})
    usage = _calls(CallUsage(model="m", input_tokens=1000, cached_input_tokens=1000),
                   input_tokens=1000, cached_input_tokens=1000)
    assert budget_tokens(usage, "c:other", rates={k: table[k] for k in ("a:m", "b:m")}) == 500
    # One of them rated at zero: no ratio to trust, so in full.
    assert budget_tokens(usage, "c:other", rates=table) == 1000


def test_an_executor_profiles_run_model_matches_its_bare_id():
    profile = AgentProfile.model_validate({
        "name": "w", "mode": "interactive", "triggers": [{"type": "http"}],
        "executor": {"executor": "codex", "instructions": "x", "model": "gpt-x"},
    })
    assert budget_model(profile) == "codex:gpt-x"
    assert budget_tokens(CACHE_HEAVY, budget_model(profile), rates=TABLE) == 600
    profile.executor.model = None
    assert budget_model(profile) is None
    assert budget_tokens(CACHE_HEAVY, budget_model(profile), rates=TABLE) == 1050


def test_a_model_tier_profiles_run_model_is_spec_model():
    profile = AgentProfile.model_validate({
        "name": "m", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-x", "instructions": "x"},
    })
    assert budget_model(profile) == "anthropic:claude-x"


# ── daily budget ────────────────────────────────────────────────────────────


def test_daily_budget_and_in_flight_reserve_weigh_the_same_way(tmp_path):
    from miragen.runs import RunStore, reserved_tokens_in_flight, tokens_used_since

    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    store = RunStore(tmp_path)
    done = store.start(agent_name="a", trigger="http", prompt="p")
    store.finish(done, status="succeeded", usage=CACHE_HEAVY)
    model_rates.configure(StaticRatesSource(TABLE))
    assert tokens_used_since(store, since, "anthropic:claude-x") == 195
    assert tokens_used_since(store, since, "openai:gpt-x") == 600
    assert tokens_used_since(store, since) == 1050  # record has no model: in full

    running = store.start(agent_name="a", trigger="http", prompt="p")
    running.usage = CACHE_HEAVY
    store._write(running)
    # reserve 1000 minus what the running record already used, same weights
    assert reserved_tokens_in_flight(store, since, 1000, 10_000, "anthropic:claude-x") == 805


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
    # fresh: 1000 (legacy) + 5000 (new) input, 1500 output; reads 105000 at 0.05
    assert budget_tokens(sum_usage(legacy, resumed), "anthropic:claude-x", rates=TABLE) == 12750


def test_sum_usage_keeps_a_reported_zero():
    from miragen.models import sum_usage

    zero = RunUsage(requests=1, input_tokens=0, cached_input_tokens=0, output_tokens=0)
    total = sum_usage(zero, zero)
    assert (total.input_tokens, total.cached_input_tokens, total.output_tokens) == (0, 0, 0)
    unknown = sum_usage(RunUsage(requests=1), RunUsage(requests=1))
    assert unknown.cached_input_tokens is None
