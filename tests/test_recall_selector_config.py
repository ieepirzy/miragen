"""Recall-selector configuration on the daemon: a pydantic-ai model re-pointed
at a `base_url` (with the key from a NAMED env var, never borrowed from the
real provider), the per-call `timeout_s`, config-load validation, the
startup credential check for `claude-code:`, and /health naming the backend
without ever naming the endpoint."""

from __future__ import annotations

import asyncio
import json

import pytest

from miragen.daemon.api import build_recall_selector
from miragen.daemon.sessions.config import SessionsRecall
from miragen.memory import claude_code, selection
from miragen.memory.claude_code import DEFAULT_TIMEOUT_S, unavailable_reason
from miragen.memory.selection import (
    KEYLESS_PLACEHOLDER,
    SelectionResult,
    SelectorError,
    build_model_selector,
    endpoint_model,
    resolve_api_key,
)

CARDS = [{"record_id": "r1", "type": "observation", "payload": {"text": "t"}}]


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
        def boom(*a, **k):
            raise AssertionError("no base_url → no endpoint model")

        monkeypatch.setattr(selection, "endpoint_model", boom)
        select = build_model_selector("test")  # pydantic-ai's built-in test model
        assert select.backend == "pydantic-ai" and select.base_url_configured is False
        assert isinstance(await select("q", CARDS), SelectionResult)


# ── timeout_s ────────────────────────────────────────────────────────────────


class TestTimeout:
    def test_claude_code_timeout_reaches_the_runner(self, monkeypatch):
        seen = []
        real = claude_code.ClaudeCodeRunner.__init__

        def spy(self, model, **kw):
            seen.append(kw.get("timeout"))
            real(self, model, **kw)

        monkeypatch.setattr(claude_code.ClaudeCodeRunner, "__init__", spy)
        build_model_selector("claude-code:haiku", timeout_s=7)
        build_model_selector("claude-code:haiku")
        assert seen == [7, DEFAULT_TIMEOUT_S]

    async def test_a_slow_pydantic_ai_selection_times_out(self, monkeypatch):
        from pydantic_ai.models.function import AgentInfo, FunctionModel

        async def slow(messages, info: AgentInfo):
            await asyncio.sleep(5)

        monkeypatch.setattr(selection, "endpoint_model", lambda *a: FunctionModel(slow))
        select = build_model_selector("openai:m", base_url="http://127.0.0.1:1/v1",
                                      timeout_s=0.2)
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(TimeoutError):
            await select("q", CARDS)
        assert loop.time() - start < 2


# ── configuration ────────────────────────────────────────────────────────────


class TestConfig:
    @pytest.mark.parametrize("fields", [
        {"model": "deepseek:deepseek-chat"},
        {"model": "claude-code:haiku"},
        {"model": "claude-code:haiku", "timeout_s": 30},
        {"model": "openai:qwen", "base_url": "http://127.0.0.1:8081/v1"},
        {"model": "anthropic:claude-haiku-4-5", "base_url": "https://proxy.test",
         "api_key_env": "PROXY_KEY"},
        {"model": None},
    ])
    def test_accepted(self, fields):
        SessionsRecall(**fields)

    @pytest.mark.parametrize("fields, match", [
        ({"model": "claude-code:haiku", "base_url": "http://x"}, "pydantic-ai models only"),
        ({"model": "claude-code:"}, "no model"),
        ({"model": "deepseek:deepseek-chat", "base_url": "http://x"}, "OpenAI- or Anthropic"),
        ({"model": "openai:m", "api_key_env": "K"}, "only with base_url"),
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


# ── startup credential check ─────────────────────────────────────────────────


class TestAvailability:
    @pytest.fixture
    def binary(self, tmp_path):
        path = tmp_path / "claude"
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return str(path)

    def test_missing_binary(self, tmp_path):
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t", "PATH": str(tmp_path)}
        assert "not on PATH" in unavailable_reason("claude-nope", env, home=tmp_path)

    def test_oauth_token_env_is_enough(self, binary, tmp_path):
        assert unavailable_reason(binary, {"CLAUDE_CODE_OAUTH_TOKEN": "t"}, home=tmp_path) is None

    def test_a_login_under_the_config_dir_is_enough(self, binary, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / ".credentials.json").write_text("{}")
        assert unavailable_reason(binary, {}, home=tmp_path) is None

    def test_an_api_key_does_not_count(self, binary, tmp_path):
        # child_env() scrubs it: it could never reach the CLI.
        reason = unavailable_reason(binary, {"ANTHROPIC_API_KEY": "sk"}, home=tmp_path)
        assert reason and "subscription" in reason

    def test_build_warns_when_unavailable(self, monkeypatch, caplog):
        monkeypatch.setattr(claude_code, "unavailable_reason", lambda *a, **k: "no creds")
        with caplog.at_level("WARNING"):
            assert build_recall_selector(SessionsRecall(model="claude-code:haiku")) is not None
        assert "no creds" in caplog.text


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
        from tests.test_sessions_plane import Harness

        on = Harness(tmp_path / "on", selector=build_model_selector("claude-code:haiku"))
        recall = on.plane.describe()["recall"]
        assert (recall["selector_backend"], recall["selector_model"]) == (
            "claude-code", "claude-code:haiku")
        off = Harness(tmp_path / "off").plane.describe()["recall"]
        assert off["selector_configured"] is False and off["selector_backend"] is None
