"""The base tier's harness seam: selection by model prefix, and the
PydanticAI harness behaving exactly like the pre-seam inline code."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from miragen.harness import (
    PydanticAIHarness, HarnessTurn, build_model_harness, parse_harness_model,
    profile_harness, pydantic_ai_model,
)
from miragen.models import AgentProfile


def profile(model: str) -> AgentProfile:
    return AgentProfile.model_validate({
        "name": "t", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": model, "instructions": "hi"},
    })


@pytest.mark.parametrize("model,expected", [
    ("grok-build:grok-4.6", ("grok-build", "grok-4.6")),
    ("grok-build:", ("grok-build", "")),
    ("anthropic:claude-sonnet-4-6", ("pydantic-ai", "anthropic:claude-sonnet-4-6")),
    ("openai:gpt-5", ("pydantic-ai", "openai:gpt-5")),
    ("test", ("pydantic-ai", "test")),
])
def test_parse_harness_model(model, expected):
    assert parse_harness_model(model) == expected


def test_profile_harness_and_pydantic_model_fallback():
    assert profile_harness(profile("test")) == "pydantic-ai"
    assert pydantic_ai_model(profile("test")) == "test"
    grok = profile("grok-build:grok-4.6")
    assert profile_harness(grok) == "grok-build"
    # memory selector / extraction must not fall back to a harness model
    assert pydantic_ai_model(grok) is None


def _agent(output="out"):
    result = MagicMock()
    result.output = output
    result.usage.requests = 1
    result.usage.input_tokens = 3
    result.usage.output_tokens = 4
    result.all_messages.return_value = []
    agent = MagicMock()
    agent.run = AsyncMock(return_value=result)
    return agent


def harness(agent, **kw):
    built = []
    saved = []

    def build(secret_env, extra):
        built.append((secret_env, extra))
        return _agent("rebuilt"), None
    h = PydanticAIHarness(agent, None, build_run_agent=build,
                          load_history=kw.get("load", lambda i: []),
                          save_history=lambda i, m, r: saved.append((i, r)))
    return h, built, saved


async def test_startup_agent_reused_without_credentials_or_packet():
    agent = _agent()
    h, built, saved = harness(agent)
    res = await h.run(HarnessTurn(prompt="p", instance="default"))
    assert res.output == "out" and built == [] and saved == []
    assert res.usage.input_tokens == 3
    agent.run.assert_awaited_once()
    assert agent.run.call_args.kwargs["message_history"] is None


@pytest.mark.parametrize("turn_kw", [
    {"secret_env": {"TOKEN": "x"}},
    {"extra_instructions": ""},          # a memory packet, even an empty one
    {"extra_instructions": "memory"},
])
async def test_per_run_agent_for_credentials_or_memory_packet(turn_kw):
    h, built, _ = harness(_agent())
    res = await h.run(HarnessTurn(prompt="p", instance="default", **turn_kw))
    assert res.output == "rebuilt" and len(built) == 1


async def test_history_loaded_and_saved_only_with_use_history():
    loads = []
    agent = _agent()
    h, _, saved = harness(agent, load=lambda i: loads.append(i) or ["m"])
    await h.run(HarnessTurn(prompt="p", instance="inst", use_history=True, run_id="r1"))
    assert loads == ["inst"] and saved == [("inst", "r1")]
    assert agent.run.call_args.kwargs["message_history"] == ["m"]


def test_grok_build_prefix_builds_a_grok_harness_with_its_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRAGEN_GROK_HOME", str(tmp_path / "grok-home"))
    monkeypatch.setenv("MIRAGEN_GROK_WORKDIRS", str(tmp_path / "work"))
    harness, gateway = build_model_harness(profile("grok-build:grok-4.6"), runs_root=tmp_path)
    assert harness.name == "grok-build" and harness.gateway is gateway
    assert harness.model == "grok-4.6"
