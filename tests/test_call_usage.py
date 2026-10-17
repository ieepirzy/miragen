"""Per-request usage (RunUsage.calls) and the one token convention every
adapter follows: input_tokens INCLUDES cache reads and cache writes."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from miragen.models import (
    CALLS_CAP,
    CallUsage,
    RunRecord,
    RunUsage,
    anthropic_usage_fields,
    sum_usage,
)
from miragen.runs import extract_run_details


# ── RunUsage / sum_usage ─────────────────────────────────────────────────────

def test_calls_are_capped_and_flagged_but_totals_stay_true():
    calls = [CallUsage(input_tokens=1) for _ in range(CALLS_CAP + 5)]
    usage = RunUsage(requests=CALLS_CAP + 5, input_tokens=CALLS_CAP + 5, calls=calls)
    assert len(usage.calls) == CALLS_CAP and usage.calls_truncated is True
    assert usage.input_tokens == CALLS_CAP + 5 and usage.requests == CALLS_CAP + 5
    small = RunUsage(requests=2, calls=[CallUsage(), CallUsage()])
    assert small.calls_truncated is False


def test_sum_usage_new_fields_keep_none_vs_zero():
    reported_zero = RunUsage(requests=1, input_tokens=5, cache_write_tokens=0)
    unreported = RunUsage(requests=1, input_tokens=5)
    assert sum_usage(unreported, unreported).cache_write_tokens is None
    assert sum_usage(reported_zero, unreported).cache_write_tokens == 0
    assert sum_usage(reported_zero, RunUsage(requests=1, cache_write_tokens=7)).cache_write_tokens == 7


def test_sum_usage_1h_split_unknown_when_any_writing_side_lacks_it():
    known = RunUsage(requests=1, cache_write_tokens=10, cache_write_1h_tokens=4)
    unknown = RunUsage(requests=1, cache_write_tokens=6)
    no_writes = RunUsage(requests=1, cache_write_tokens=0)
    assert sum_usage(known, known).cache_write_1h_tokens == 8
    assert sum_usage(known, no_writes).cache_write_1h_tokens == 4
    combined = sum_usage(known, unknown)
    assert combined.cache_write_tokens == 16 and combined.cache_write_1h_tokens is None


def test_sum_usage_concatenates_calls_and_drops_an_incomplete_breakdown():
    a = RunUsage(requests=1, calls=[CallUsage(input_tokens=1)])
    b = RunUsage(requests=1, calls=[CallUsage(input_tokens=2)])
    assert [c.input_tokens for c in sum_usage(a, b).calls] == [1, 2]
    assert sum_usage(a, RunUsage(requests=1)).calls is None
    assert sum_usage(RunUsage(requests=1), b).calls is None


def test_sum_usage_reapplies_the_cap():
    half = CALLS_CAP // 2 + 1
    a = RunUsage(requests=half, calls=[CallUsage() for _ in range(half)])
    total = sum_usage(a, a)
    assert len(total.calls) == CALLS_CAP and total.calls_truncated is True
    assert total.requests == 2 * half
    # A truncated side stays truncated after summing.
    big = RunUsage(requests=CALLS_CAP + 1, calls=[CallUsage() for _ in range(CALLS_CAP + 1)])
    assert sum_usage(big, RunUsage(requests=1, calls=[])).calls_truncated is True


def test_run_record_json_carries_the_new_fields():
    at = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
    record = RunRecord(
        run_id="r", agent_name="a", trigger="http", status="succeeded",
        started_at=at, prompt="p",
        usage=RunUsage(requests=1, input_tokens=10, cached_input_tokens=4,
                       cache_write_tokens=3, cache_write_1h_tokens=1,
                       calls=[CallUsage(at=at, model="m", input_tokens=10, speed="fast")]),
    )
    usage = record.model_dump(mode="json")["usage"]
    assert usage["cache_write_tokens"] == 3 and usage["cache_write_1h_tokens"] == 1
    assert usage["calls_truncated"] is False
    assert usage["calls"][0] == {
        "at": "2026-10-12T08:00:00Z", "model": "m", "input_tokens": 10,
        "cached_input_tokens": None, "cache_write_tokens": None,
        "cache_write_1h_tokens": None, "output_tokens": None,
        "speed": "fast", "inference_geo": None,
    }
    # Records written before these fields existed still load.
    old = RunRecord.model_validate_json(
        '{"run_id":"r","agent_name":"a","trigger":"http","status":"succeeded",'
        '"started_at":"2026-10-12T08:00:00Z","prompt":"p",'
        '"usage":{"requests":1,"input_tokens":5}}')
    assert old.usage.calls is None and old.usage.cache_write_tokens is None


# ── Anthropic-shaped (exclusive) usage ──────────────────────────────────────

def test_anthropic_usage_is_normalised_to_inclusive_input():
    # A Claude Code ResultMessage.usage, as observed live.
    raw = {"input_tokens": 4, "cache_creation_input_tokens": 18053,
           "cache_read_input_tokens": 39394, "output_tokens": 123,
           "cache_creation": {"ephemeral_1h_input_tokens": 18053, "ephemeral_5m_input_tokens": 0},
           "service_tier": "standard", "speed": "standard", "inference_geo": "not_available"}
    assert anthropic_usage_fields(raw) == {
        "input_tokens": 4 + 18053 + 39394, "output_tokens": 123,
        "cached_input_tokens": 39394, "cache_write_tokens": 18053,
        "cache_write_1h_tokens": 18053,
    }


def test_anthropic_usage_without_cache_fields_reports_none_not_zero():
    assert anthropic_usage_fields({"input_tokens": 7, "output_tokens": 3}) == {
        "input_tokens": 7, "output_tokens": 3, "cached_input_tokens": None,
        "cache_write_tokens": None, "cache_write_1h_tokens": None,
    }
    # No uncached figure → no total (a cache-only number is not the prompt).
    assert anthropic_usage_fields({"cache_read_input_tokens": 5})["input_tokens"] is None


# ── API path (pydantic-ai) ──────────────────────────────────────────────────

def _scripted_model(responses: list[ModelResponse]) -> FunctionModel:
    queue = list(responses)

    def respond(messages, info: AgentInfo) -> ModelResponse:
        return queue.pop(0)

    return FunctionModel(respond, model_name="scripted")


async def test_api_path_fills_cache_fields_and_one_call_per_response():
    t1 = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 12, 16, 1, tzinfo=timezone.utc)
    history_model = _scripted_model([ModelResponse(
        parts=[TextPart("earlier")], model_name="old",
        usage=RequestUsage(input_tokens=999, output_tokens=9))])
    agent = Agent(history_model)

    @agent.tool_plain
    def lookup() -> str:
        return "42"

    earlier = await agent.run("before")

    # pydantic-ai's input_tokens is already inclusive of cache reads/writes.
    model = _scripted_model([
        ModelResponse(parts=[ToolCallPart("lookup", {})], model_name="m-1", timestamp=t1,
                      usage=RequestUsage(input_tokens=1000, cache_read_tokens=600,
                                         cache_write_tokens=300, output_tokens=20)),
        ModelResponse(parts=[TextPart("done")], model_name="m-1", timestamp=t2,
                      usage=RequestUsage(input_tokens=1100, cache_read_tokens=900,
                                         output_tokens=5)),
    ])
    result = await agent.run("now", model=model, message_history=earlier.all_messages())
    usage, tool_calls = extract_run_details(result)

    assert usage.requests == 2
    assert usage.input_tokens == 2100 and usage.output_tokens == 25
    assert usage.cached_input_tokens == 1500 and usage.cache_write_tokens == 300
    assert usage.cache_write_1h_tokens is None  # pydantic-ai does not pass the split
    # Only THIS run's responses: the history's response is not a call.
    # (FunctionModel stamps its own model_name on every response.)
    assert [c.model_dump() for c in usage.calls] == [
        {"at": t1, "model": "scripted", "input_tokens": 1000, "cached_input_tokens": 600,
         "cache_write_tokens": 300, "cache_write_1h_tokens": None, "output_tokens": 20,
         "speed": None, "inference_geo": None},
        {"at": t2, "model": "scripted", "input_tokens": 1100, "cached_input_tokens": 900,
         # No writes and no provider write key: not reported, not a claimed 0.
         "cache_write_tokens": None, "cache_write_1h_tokens": None, "output_tokens": 5,
         "speed": None, "inference_geo": None},
    ]
    assert usage.calls_truncated is False


class _FakeResult:
    """A result whose responses don't match its request count."""

    def __init__(self, usage, messages):
        self.usage = usage
        self._messages = messages

    def new_messages(self):
        return self._messages

    def all_messages(self):
        return self._messages


def test_api_path_drops_calls_when_responses_do_not_cover_every_request():
    from pydantic_ai.usage import RunUsage as PaiRunUsage

    pai = PaiRunUsage(requests=2, input_tokens=10, output_tokens=2)
    one = ModelResponse(parts=[TextPart("x")], usage=RequestUsage(input_tokens=5, output_tokens=1))
    usage, _ = extract_run_details(_FakeResult(pai, [one]))
    assert usage.calls is None and usage.input_tokens == 10


def test_api_path_reads_deepseek_cache_hits_from_details():
    from pydantic_ai.usage import RunUsage as PaiRunUsage

    pai = PaiRunUsage(requests=1, input_tokens=1000, output_tokens=50,
                      details={"prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200})
    response = ModelResponse(parts=[TextPart("x")], usage=RequestUsage(
        input_tokens=1000, output_tokens=50, details={"prompt_cache_hit_tokens": 800}))
    usage, _ = extract_run_details(_FakeResult(pai, [response]))
    assert usage.cached_input_tokens == 800 and usage.calls[0].cached_input_tokens == 800


# ── Every adapter, one convention ────────────────────────────────────────────

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_claude_code_executor_normalises_result_usage():
    from miragen.executor.base import _usage_from_payload
    from miragen.executor.claude_code import _normalize

    message = type("ResultMessage", (), {})()
    message.session_id, message.is_error = None, False
    message.usage = {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 100,
                     "cache_creation_input_tokens": 20,
                     "cache_creation": {"ephemeral_5m_input_tokens": 15,
                                        "ephemeral_1h_input_tokens": 5}}
    (payload,) = _normalize(message)
    usage = _usage_from_payload(payload["usage"])
    assert (usage.input_tokens, usage.cached_input_tokens, usage.cache_write_tokens,
            usage.cache_write_1h_tokens, usage.output_tokens) == (127, 100, 20, 5, 3)


def test_kimi_adds_cache_read_and_creation_to_input_other():
    from miragen.executor.kimi_code import _usage_from_status

    raw = _Obj(input_other=10, output=2, input_cache_read=50, input_cache_creation=5)
    assert _usage_from_status(_Obj(token_usage=raw)) == {
        "input_tokens": 65, "output_tokens": 2, "cached_input_tokens": 50,
        "cache_write_tokens": 5,
    }


def test_grok_build_headless_is_uncached_and_acp_is_full():
    from miragen.executor.grok_build import _usage_from_client

    headless = {"input_tokens": 7210, "cache_read_input_tokens": 41000,
                "cache_creation_input_tokens": 0, "output_tokens": 1893}
    assert _usage_from_client(headless) == {
        "input_tokens": 48210, "output_tokens": 1893, "cached_input_tokens": 41000,
        "cache_write_tokens": 0,
    }
    assert _usage_from_client({"inputTokens": 48210, "outputTokens": 5}) == {
        "input_tokens": 48210, "output_tokens": 5,
    }


def test_grok_harness_acp_usage_is_already_inclusive():
    from miragen.harness.grok import _usage

    acp = _usage({"inputTokens": 500, "outputTokens": 5, "cachedReadTokens": 400,
                  "cachedWriteTokens": 50})
    assert (acp.input_tokens, acp.cached_input_tokens, acp.cache_write_tokens) == (500, 400, 50)
    snake = _usage({"input_tokens": 50, "output_tokens": 5, "cache_read_input_tokens": 400})
    assert (snake.input_tokens, snake.cached_input_tokens) == (450, 400)


def test_codex_reports_cache_writes_only_when_sent():
    from miragen.executor.codex import _usage_from_turn
    from miragen.harness.codex import _usage
    from pydantic import BaseModel, ConfigDict, Field

    class TokenUsageBreakdown(BaseModel):
        # The openai-codex SDK model's shape: the cache-write count defaults
        # to 0, so only model_fields_set tells "sent" from "absent".
        model_config = ConfigDict(populate_by_name=True)
        cache_write_input_tokens: int | None = Field(0, alias="cacheWriteInputTokens")
        cached_input_tokens: int = Field(alias="cachedInputTokens")
        input_tokens: int = Field(alias="inputTokens")
        output_tokens: int = Field(alias="outputTokens")

    common = {"inputTokens": 900, "cachedInputTokens": 400, "outputTokens": 10,
              "reasoningOutputTokens": 0, "totalTokens": 910}
    absent = TokenUsageBreakdown.model_validate(common)
    sent = TokenUsageBreakdown.model_validate({**common, "cacheWriteInputTokens": 30})
    turn = lambda b: _Obj(usage=_Obj(last=b))  # noqa: E731
    assert "cache_write_tokens" not in _usage_from_turn(turn(absent))
    assert _usage_from_turn(turn(sent))["cache_write_tokens"] == 30
    assert _usage_from_turn(turn(sent))["input_tokens"] == 900  # already inclusive
    assert _usage(common).cache_write_tokens is None
    assert _usage({**common, "cacheWriteInputTokens": 30}).cache_write_tokens == 30


def test_model_turn_event_carries_the_full_usage(tmp_path):
    import json

    from miragen.events import write_model_turn_events

    path = tmp_path / "events.jsonl"
    usage = RunUsage(requests=1, input_tokens=10, cached_input_tokens=4, cache_write_tokens=2,
                     calls=[CallUsage(input_tokens=10)])
    write_model_turn_events(path, usage=usage, output="ok")
    completed = [json.loads(line) for line in path.read_text().splitlines()][-1]
    assert completed["type"] == "turn.completed"
    assert completed["usage"]["cache_write_tokens"] == 2
    assert completed["usage"]["calls"][0]["input_tokens"] == 10
    assert completed["usage"]["calls_truncated"] is False


def test_api_path_reports_cache_zero_only_when_the_provider_sent_the_key():
    from pydantic_ai.usage import RunUsage as PaiRunUsage

    def run(**kw):
        pai = PaiRunUsage(requests=1, input_tokens=100, output_tokens=5, **kw)
        response = ModelResponse(parts=[TextPart("x")], usage=RequestUsage(
            input_tokens=100, output_tokens=5, details=kw.get("details", {})))
        return extract_run_details(_FakeResult(pai, [response]))[0]

    silent = run()  # e.g. an OpenAI-compatible provider sending no cache counts
    assert silent.cached_input_tokens is None and silent.cache_write_tokens is None
    anthropic_zero = run(details={"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    assert anthropic_zero.cached_input_tokens == 0 and anthropic_zero.cache_write_tokens == 0
    assert anthropic_zero.calls[0].cache_write_tokens == 0


def test_grok_harness_usage_picks_alternative_cache_keys_and_logs_keys_once(caplog):
    import logging

    import miragen.harness.grok as grok

    grok._usage_keys_logged = False
    with caplog.at_level(logging.INFO, logger="miragen.harness.grok"):
        u = grok._usage({"inputTokens": 900, "outputTokens": 3, "cacheReadTokens": 700,
                         "cacheWriteTokens": 20}, context_tokens=900)
        grok._usage({"inputTokens": 1, "cachedTokens": 1})
    assert (u.cached_input_tokens, u.cache_write_tokens, u.context_tokens) == (700, 20, 900)
    assert grok._usage({"inputTokens": 5, "cachedTokens": 4}).cached_input_tokens == 4
    logged = [r.getMessage() for r in caplog.records if "end usage keys" in r.getMessage()]
    assert logged == ["grok harness: end usage keys: ['cacheReadTokens', 'cacheWriteTokens', "
                      "'inputTokens', 'outputTokens']"]


def test_sum_usage_keeps_the_latest_context():
    from miragen.models import RunUsage, sum_usage

    a = RunUsage(requests=1, context_tokens=100)
    b = RunUsage(requests=2, context_tokens=250)
    assert sum_usage(a, b).context_tokens == 250
    assert sum_usage(b, RunUsage(requests=1)).context_tokens == 250
