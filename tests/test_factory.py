import pytest
from unittest.mock import MagicMock, patch

from miragen.factory import (
    register,
    registered_tools,
    register_handler,
    registered_handlers,
    build_agent,
    _inject_tools,
)
from miragen.models import AgentProfile


def _profile(**kw):
    return AgentProfile.model_validate({
        "name": "test",
        "mode": "autonomous",
        "triggers": [{"type": "cron", "schedule": "0 * * * *"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
        **kw,
    })


@pytest.fixture(autouse=True)
def clean_tool_registry():
    with patch.dict("miragen.factory._TOOL_REGISTRY", {}, clear=True):
        yield


@pytest.fixture(autouse=True)
def clean_handler_registry():
    with patch.dict("miragen.factory._HANDLER_REGISTRY", {}, clear=True):
        yield


class TestRegister:
    def test_bare_decorator_registers_by_name(self):
        @register
        def my_tool():
            pass

        assert registered_tools()["my_tool"] is my_tool

    def test_named_decorator_registers_by_custom_name(self):
        @register("custom_name")
        def my_tool():
            pass

        assert registered_tools()["custom_name"] is my_tool

    def test_bare_returns_original_function(self):
        def my_tool():
            pass

        result = register(my_tool)
        assert result is my_tool

    def test_named_returns_original_function(self):
        def my_tool():
            pass

        result = register("named")(my_tool)
        assert result is my_tool

    def test_registered_tools_returns_copy(self):
        @register
        def tool_a():
            pass

        snapshot = registered_tools()
        snapshot["injected"] = "bad"
        assert "injected" not in registered_tools()

    def test_multiple_tools(self):
        @register
        def tool_a():
            pass

        @register("b")
        def tool_b():
            pass

        tools = registered_tools()
        assert "tool_a" in tools
        assert "b" in tools


class TestRegisterHandler:
    def test_registers_by_name(self):
        async def my_handler(name, output):
            pass

        register_handler("miradb")(my_handler)
        assert registered_handlers()["miradb"] is my_handler

    def test_as_decorator(self):
        @register_handler("telegram")
        async def handler(name, output):
            pass

        assert registered_handlers()["telegram"] is handler

    def test_returns_original_function(self):
        async def handler(name, output):
            pass

        result = register_handler("test")(handler)
        assert result is handler

    def test_registered_handlers_returns_copy(self):
        @register_handler("h1")
        async def h1(name, output):
            pass

        snapshot = registered_handlers()
        snapshot["injected"] = "bad"
        assert "injected" not in registered_handlers()


class TestBuildAgent:
    def test_creates_agent(self):
        profile = _profile()
        with patch("miragen.factory.Agent") as MockAgent, \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            build_agent(profile)
            MockAgent.assert_called_once()

    def test_passes_model_and_instructions(self):
        profile = _profile()
        with patch("miragen.factory.Agent") as MockAgent, \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            build_agent(profile)
            kw = MockAgent.call_args.kwargs
            assert kw["model"] == "anthropic:claude-haiku-4-5"
            assert kw["instructions"] == "Test."

    def test_applies_usage_limits(self):
        profile = _profile(spec={"model": "m", "instructions": "i", "max_steps": 5})
        with patch("miragen.factory.Agent"), \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            _, limits = build_agent(profile)
            assert limits.request_limit == 5

    def test_no_usage_limits_when_unset(self):
        profile = _profile()
        with patch("miragen.factory.Agent"), \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            _, limits = build_agent(profile)
            assert limits is None

    def test_model_settings_max_tokens_and_temperature(self):
        profile = _profile(spec={
            "model": "m", "instructions": "i",
            "model_settings": {"max_tokens": 200, "temperature": 0.7},
        })
        with patch("miragen.factory.Agent") as MockAgent, \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            build_agent(profile)
            kw = MockAgent.call_args.kwargs
            assert kw["model_settings"]["max_tokens"] == 200
            assert kw["model_settings"]["temperature"] == 0.7

    def test_model_settings_none_when_unset(self):
        profile = _profile()
        with patch("miragen.factory.Agent") as MockAgent, \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            build_agent(profile)
            kw = MockAgent.call_args.kwargs
            assert kw["model_settings"] is None

    def test_model_settings_temperature_zero_included(self):
        profile = _profile(spec={"model": "m", "instructions": "i",
                                  "model_settings": {"temperature": 0.0}})
        with patch("miragen.factory.Agent") as MockAgent, \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            build_agent(profile)
            kw = MockAgent.call_args.kwargs
            assert kw["model_settings"]["temperature"] == 0.0

    def test_capabilities_resolved_and_passed(self):
        fake_cap = MagicMock()
        profile = _profile(spec={"model": "m", "instructions": "i", "capabilities": ["WebSearch"]})
        with patch("miragen.factory.Agent") as MockAgent, \
             patch("miragen.factory.resolve_capabilities", return_value=[fake_cap]) as mock_resolve:
            build_agent(profile)
            mock_resolve.assert_called_once_with(["WebSearch"])
            kw = MockAgent.call_args.kwargs
            assert kw["capabilities"] == [fake_cap]

    def test_unknown_tool_raises_value_error(self):
        profile = _profile(tools=["ghost_tool"])
        with patch("miragen.factory.Agent"), \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            with pytest.raises(ValueError, match="unknown tools"):
                build_agent(profile)

    def test_registered_tool_injected_onto_agent(self):
        @register
        def my_tool():
            pass

        profile = _profile(tools=["my_tool"])
        mock_agent = MagicMock()
        with patch("miragen.factory.Agent", return_value=mock_agent), \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            build_agent(profile)
            mock_agent.tool.assert_called_once_with(my_tool)


class TestInjectTools:
    def test_no_tools_field_skips(self):
        profile = _profile()
        mock_agent = MagicMock()
        _inject_tools(mock_agent, profile)
        mock_agent.tool.assert_not_called()

    def test_empty_tools_list_skips(self):
        profile = _profile(tools=[])
        mock_agent = MagicMock()
        _inject_tools(mock_agent, profile)
        mock_agent.tool.assert_not_called()

    def test_unknown_tool_error_lists_available(self):
        @register
        def known_tool():
            pass

        profile = _profile(tools=["unknown"])
        mock_agent = MagicMock()
        with pytest.raises(ValueError, match="known_tool"):
            _inject_tools(mock_agent, profile)

    def test_injects_multiple_tools_in_order(self):
        @register
        def alpha():
            pass

        @register
        def beta():
            pass

        profile = _profile(tools=["alpha", "beta"])
        mock_agent = MagicMock()
        _inject_tools(mock_agent, profile)
        assert mock_agent.tool.call_count == 2
        calls = [c.args[0] for c in mock_agent.tool.call_args_list]
        assert calls == [alpha, beta]
