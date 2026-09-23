"""Recall-selector backends: `claude-code:<model>` (headless Claude Code via
claude-agent-sdk, driven here through a fake query factory — CI installs
`.[dev]` only, so nothing may import the SDK), and the pydantic-ai path
re-pointed at a `base_url`. The contract these guard: a selection is a
validated SelectionResult or an exception, and an exception (bad JSON,
error result, timeout) makes the lane inject NOTHING optional."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

import pytest

from miragen.daemon.api import build_recall_selector
from miragen.daemon.sessions.config import SessionsRecall
from miragen.memory import selection
from miragen.memory.selection import (
    KEYLESS_PLACEHOLDER,
    SELECTOR_INSTRUCTIONS,
    SelectionResult,
    SelectorError,
    build_model_selector,
    endpoint_model,
    resolve_api_key,
)
from tests.test_memory_recall_lane import _card, _lifecycle, _seed_record, memory_env, service  # noqa: F401


class ResultMessage:
    """Stand-in for claude_agent_sdk.ResultMessage (matched by class name)."""

    def __init__(self, *, structured_output=None, result=None, subtype="success", is_error=False):
        self.structured_output = structured_output
        self.result = result
        self.subtype = subtype
        self.is_error = is_error


class AssistantMessage:
    content: list = []


def _factory(*messages, delay: float = 0.0, seen: list | None = None, closed: list | None = None,
             teardown: float = 0.0):
    """A query_factory yielding `messages`; records (prompt, options).
    `teardown` models the SDK's graceful CLI shutdown after a cancel."""

    def factory(prompt, options):
        if seen is not None:
            seen.append((prompt, options))

        async def gen():
            try:
                if delay:
                    await asyncio.sleep(delay)
                for message in messages:
                    yield message
            finally:
                if teardown:
                    await asyncio.sleep(teardown)
                if closed is not None:
                    closed.append(True)

        return gen()

    return factory


def _cc(*messages, timeout_s=5.0, **kw):
    return build_model_selector("claude-code:haiku", timeout_s=timeout_s,
                                query_factory=_factory(*messages, **kw))


CARDS = [{"record_id": "r1", "type": "fact", "payload": {"text": "tests need postgres"}},
         {"record_id": "r2", "type": "fact", "payload": {"text": "likes coffee"}}]


# ── claude-code backend ──────────────────────────────────────────────────────


class TestClaudeCodeSelector:
    async def test_structured_output_becomes_a_selection(self):
        seen = []
        select = build_model_selector("claude-code:haiku", query_factory=_factory(
            AssistantMessage(),
            ResultMessage(structured_output={"selections": [{"record_id": "r1", "reason": "setup"}]}),
            seen=seen,
        ))
        result = await select("how do I run the tests", CARDS)
        assert result == SelectionResult.model_validate(
            {"selections": [{"record_id": "r1", "reason": "setup"}]})
        assert select.backend == "claude-code" and select.model == "claude-code:haiku"

        prompt, options = seen[0]
        assert "record_id=r1" in prompt and "how do I run the tests" in prompt
        # One bounded, tool-less, settings-less call with the SAME instructions.
        assert options["model"] == "haiku"
        assert options["system_prompt"] == SELECTOR_INSTRUCTIONS
        assert options["tools"] == []
        assert options["max_turns"] == 1
        assert options["setting_sources"] == []
        assert options["strict_mcp_config"] is True
        assert "no-session-persistence" in options["extra_args"]
        assert options["output_format"] == {
            "type": "json_schema", "schema": SelectionResult.model_json_schema()}
        assert os.path.isdir(options["cwd"]) and os.listdir(options["cwd"]) == []
        assert "can_use_tool" not in options and "mcp_servers" not in options

    async def test_cwd_is_reused_not_leaked_per_prompt(self):
        seen = []
        select = build_model_selector("claude-code:haiku", query_factory=_factory(
            ResultMessage(structured_output={"selections": []}), seen=seen))
        await select("q one", CARDS)
        await select("q two", CARDS)
        assert seen[0][1]["cwd"] == seen[1][1]["cwd"]

    async def test_json_text_is_the_fallback_when_no_structured_output(self):
        select = _cc(ResultMessage(result=json.dumps(
            {"selections": [{"record_id": "r2", "reason": "x"}]})))
        result = await select("q", CARDS)
        assert [s.record_id for s in result.selections] == ["r2"]

    async def test_empty_selection_is_valid(self):
        select = _cc(ResultMessage(structured_output={"selections": []}))
        assert (await select("q", CARDS)).selections == []

    @pytest.mark.parametrize("message", [
        ResultMessage(result="Sure! I'd pick r1."),                       # prose, not JSON
        ResultMessage(result='{"selections": [{"record_id": "r1"}]}'),   # reason missing
        ResultMessage(structured_output={"selections": [{"record_id": "r1", "reason": ""}]}),
        ResultMessage(structured_output={"selections": [], "extra": 1}),  # extra=forbid
        ResultMessage(structured_output={"picked": ["r1"]}),
        ResultMessage(result=""),
    ])
    async def test_invalid_output_raises(self, message):
        with pytest.raises(SelectorError):
            await _cc(message)("q", CARDS)

    @pytest.mark.parametrize("message", [
        ResultMessage(is_error=True, subtype="success", result="auth failed"),
        ResultMessage(subtype="error_max_turns",
                      structured_output={"selections": [{"record_id": "r1", "reason": "x"}]}),
    ])
    async def test_error_results_raise_even_with_output(self, message):
        with pytest.raises(SelectorError, match="claude-code selector failed"):
            await _cc(message)("q", CARDS)

    async def test_no_result_message_raises(self):
        with pytest.raises(SelectorError, match="no result"):
            await _cc(AssistantMessage())("q", CARDS)

    async def test_timeout_is_a_selector_failure_and_cancels_the_query(self):
        closed = []
        select = _cc(ResultMessage(structured_output={"selections": []}),
                     delay=5.0, timeout_s=0.05, closed=closed)
        with pytest.raises(SelectorError, match="timed out after 0.05s"):
            await asyncio.wait_for(select("q", CARDS), timeout=2.0)
        for _ in range(50):
            if closed:
                break
            await asyncio.sleep(0.01)
        assert closed == [True]  # the stream was torn down, not left running

    async def test_timeout_does_not_wait_for_the_cli_teardown(self):
        # The SDK gives a cancelled CLI up to 5 s to exit gracefully; the
        # hook must get its failure at the deadline, not after that.
        closed = []
        select = _cc(ResultMessage(structured_output={"selections": []}),
                     delay=5.0, timeout_s=0.05, teardown=0.6, closed=closed)
        started = asyncio.get_running_loop().time()
        with pytest.raises(SelectorError, match="timed out"):
            await select("q", CARDS)
        assert asyncio.get_running_loop().time() - started < 0.4
        assert closed == []          # teardown still running in the background
        await asyncio.sleep(0.8)
        assert closed == [True]      # ...and it completes

    def test_building_imports_nothing(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", None)
        # Building (boot time) must not import the SDK; only the default
        # factory does, on the first selection.
        select = build_model_selector("claude-code:haiku")
        assert select.backend == "claude-code"

    def test_startup_warns_without_any_credential(self, monkeypatch, tmp_path, caplog):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        with caplog.at_level(logging.WARNING, logger="miragen.memory.selection"):
            build_model_selector("claude-code:haiku")
        assert "claude setup-token" in caplog.text

    def test_no_credential_warning_when_oauth_token_is_set(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        monkeypatch.setenv("HOME", str(tmp_path))
        with caplog.at_level(logging.WARNING, logger="miragen.memory.selection"):
            build_model_selector("claude-code:haiku")
        assert "claude setup-token" not in caplog.text


# ── through the lane: canonical re-resolution + fail-closed ──────────────────


class TestClaudeCodeInTheLane:
    async def test_unknown_ids_are_still_filtered(self, service, tmp_path):
        card = _card("legit candidate")
        service.search_results = [card]
        _seed_record(service, card)
        select = _cc(ResultMessage(structured_output={"selections": [
            {"record_id": str(uuid.uuid4()), "reason": "invented"},
            {"record_id": card["record_id"], "reason": "applies"},
        ]}))
        packet = await _lifecycle(service, tmp_path, select).prepare_context(
            instance="ops", run_id="r1", trigger="http", prompt_hint="q")
        assert [i["record_id"] for i in packet.items if i["kind"] == "recalled"] == [card["record_id"]]
        assert "legit candidate" in packet.text
        injected = [i for m in service.manifests for i in m.get("items", [])]
        assert [i["revision_id"] for i in injected] == [card["revision_id"]]  # manifest shape the failure test relies on

    @pytest.mark.parametrize("kw", [
        {"messages": (ResultMessage(result="not json"),)},
        {"messages": (ResultMessage(structured_output={"selections": []}),),
         "delay": 5.0, "timeout_s": 0.05},
    ], ids=["invalid-json", "timeout"])
    async def test_failure_injects_nothing(self, service, tmp_path, kw):
        card = _card("would be relevant")
        service.search_results = [card]
        _seed_record(service, card)
        select = _cc(*kw["messages"], delay=kw.get("delay", 0.0), timeout_s=kw.get("timeout_s", 5.0))
        lifecycle = _lifecycle(service, tmp_path, select)
        section, status = await lifecycle.recall_section(instance="ops", prompt_hint="q")
        assert status.startswith("degraded: selector")
        assert "would be relevant" not in (section or "")
        # A manifest records what was injected (items = revision_id + reason);
        # a failed selection injected nothing, so none may name an item.
        assert not [m for m in service.manifests if m.get("items")]


# ── pydantic-ai path + base_url ──────────────────────────────────────────────


class TestBaseUrl:
    def test_endpoint_model_targets_the_base_url_with_the_named_key(self):
        model = endpoint_model("openai:qwen2.5-7b", "http://127.0.0.1:11434/v1", "k-123")
        assert type(model).__name__ == "OpenAIChatModel"
        assert model.model_name == "qwen2.5-7b"
        assert str(model.client.base_url).rstrip("/") == "http://127.0.0.1:11434/v1"
        assert model.client.api_key == "k-123"

    def test_responses_flavour(self):
        model = endpoint_model("openai-responses:m", "http://proxy.test/v1", "k")
        assert type(model).__name__ == "OpenAIResponsesModel"

    async def test_selector_wires_base_url_and_env_key_into_the_model(self, monkeypatch):
        from pydantic_ai.models.test import TestModel

        built = []

        def fake_endpoint_model(model, base_url, api_key):
            built.append((model, base_url, api_key))
            return TestModel(custom_output_args={"selections": [{"record_id": "r1", "reason": "x"}]})

        monkeypatch.setattr(selection, "endpoint_model", fake_endpoint_model)
        monkeypatch.setenv("SELECTOR_KEY", "from-env")
        select = build_model_selector("openai:local", base_url="http://127.0.0.1:8081/v1",
                                      api_key_env="SELECTOR_KEY")
        result = await select("q", CARDS)
        assert [s.record_id for s in result.selections] == ["r1"]
        assert built == [("openai:local", "http://127.0.0.1:8081/v1", "from-env")]
        assert select.backend == "pydantic-ai" and select.base_url_configured is True

    def test_key_from_file(self, monkeypatch, tmp_path):
        secret = tmp_path / "key"
        secret.write_text("from-file\n")
        monkeypatch.delenv("SELECTOR_KEY", raising=False)
        monkeypatch.setenv("SELECTOR_KEY_FILE", str(secret))
        assert resolve_api_key("SELECTOR_KEY") == "from-file"

    def test_named_but_missing_key_fails(self, monkeypatch):
        monkeypatch.delenv("SELECTOR_KEY", raising=False)
        monkeypatch.delenv("SELECTOR_KEY_FILE", raising=False)
        with pytest.raises(SelectorError, match="SELECTOR_KEY"):
            resolve_api_key("SELECTOR_KEY")

    def test_keyless_endpoint_never_borrows_the_provider_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
        assert resolve_api_key(None) == KEYLESS_PLACEHOLDER
        model = endpoint_model("openai:m", "http://127.0.0.1:1/v1", resolve_api_key(None))
        assert model.client.api_key == KEYLESS_PLACEHOLDER

    async def test_plain_model_strings_are_unchanged(self, monkeypatch):
        from pydantic_ai.models.test import TestModel

        def boom(*a, **k):
            raise AssertionError("no base_url → no endpoint model")

        monkeypatch.setattr(selection, "endpoint_model", boom)
        select = build_model_selector("test")  # pydantic-ai's built-in test model
        assert select.backend == "pydantic-ai" and select.base_url_configured is False
        assert isinstance(await select("q", CARDS), SelectionResult)
        assert TestModel  # imported: pydantic-ai is a core dependency


# ── configuration ────────────────────────────────────────────────────────────


class TestConfig:
    @pytest.mark.parametrize("fields", [
        {"model": "deepseek:deepseek-chat"},
        {"model": "claude-code:haiku"},
        {"model": "openai:qwen", "base_url": "http://127.0.0.1:8081/v1"},
        {"model": "anthropic:claude-haiku-4-5", "base_url": "https://proxy.test",
         "api_key_env": "PROXY_KEY"},
        {"model": None},
    ])
    def test_accepted(self, fields):
        SessionsRecall(**fields)

    @pytest.mark.parametrize("fields, match", [
        ({"model": "claude-code:haiku", "base_url": "http://x"}, "pydantic-ai models only"),
        ({"model": "claude-code:"}, "names no model"),
        ({"model": "deepseek:deepseek-chat", "base_url": "http://x"}, "OpenAI- or Anthropic"),
        ({"model": "openai:m", "api_key_env": "K"}, "only with recall.base_url"),
        ({"model": "openai:m", "base_url": "http://x", "api_key_env": "not a name"}, "pattern"),
        ({"model": "openai:m", "timeout_s": 0}, "greater than"),
    ])
    def test_rejected(self, fields, match):
        with pytest.raises(ValueError, match=match):
            SessionsRecall(**fields)

    def test_build_recall_selector(self):
        assert build_recall_selector(SessionsRecall(model=None)) is None
        assert build_recall_selector(SessionsRecall(enabled=False, model="claude-code:haiku")) is None
        select = build_recall_selector(SessionsRecall(model="claude-code:haiku", timeout_s=3))
        assert select.backend == "claude-code" and select.model == "claude-code:haiku"


# ── /health ──────────────────────────────────────────────────────────────────


class TestHealth:
    def test_health_names_the_backend_but_never_the_endpoint(self, tmp_path):
        from fastapi.testclient import TestClient

        from miragen.daemon.api import create_app
        from tests.test_sessions_plane import Harness

        select = build_model_selector("openai:m", base_url="http://private-host.internal:9/v1",
                                      api_key_env="SELECTOR_KEY")
        h = Harness(tmp_path, selector=select)
        with TestClient(create_app(None, token="", sessions=h.plane)) as client:
            raw = client.get("/health").text
        recall = json.loads(raw)["sessions"]["recall"]
        assert recall["selector_configured"] is True
        assert recall["selector_backend"] == "pydantic-ai"
        assert recall["selector_model"] == "openai:m"
        assert recall["selector_base_url"] is True
        assert "private-host" not in raw and "SELECTOR_KEY" not in raw

    def test_health_labels_claude_code_and_off(self, tmp_path):
        from miragen.daemon.api import create_app  # noqa: F401
        from tests.test_sessions_plane import Harness

        on = Harness(tmp_path / "on", selector=build_model_selector("claude-code:haiku"))
        recall = on.plane.describe()["recall"]
        assert (recall["selector_backend"], recall["selector_model"]) == ("claude-code", "claude-code:haiku")
        off = Harness(tmp_path / "off").plane.describe()["recall"]
        assert off["selector_configured"] is False and off["selector_backend"] is None
