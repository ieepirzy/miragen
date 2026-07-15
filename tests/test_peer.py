import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from miragen.load import resolve_capabilities
from miragen.peer import build_peer_capability


def _mock_http(post_result=None, post_side_effect=None):
    mock_client = MagicMock()
    if post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    else:
        mock_client.post = AsyncMock(return_value=post_result)

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_client)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    return mock_http, mock_client


def _get_call_agent(capability):
    # Toolset(toolset=FunctionToolset([call_agent]), id="Peer")
    tool = capability.toolset.tools["call_agent"]
    return tool.function


class TestBuildPeerCapability:
    def test_missing_agents_raises(self):
        with pytest.raises(ValueError, match="non-empty 'agents'"):
            build_peer_capability({})

    def test_empty_agents_raises(self):
        with pytest.raises(ValueError, match="non-empty 'agents'"):
            build_peer_capability({"agents": []})

    def test_invalid_agent_name_raises(self):
        with pytest.raises(ValueError, match="invalid name"):
            build_peer_capability({"agents": ["Bad Name"]})

    def test_valid_config_builds_toolset(self):
        cap = build_peer_capability({"agents": ["researcher"]})
        assert cap.id == "Peer"
        assert "call_agent" in cap.toolset.tools

    def test_docstring_includes_allowlist(self):
        cap = build_peer_capability({"agents": ["researcher", "writer"]})
        fn = _get_call_agent(cap)
        assert "researcher" in fn.__doc__
        assert "writer" in fn.__doc__

    def test_registry_integration(self):
        caps = resolve_capabilities([{"Peer": {"agents": ["a"]}}])
        assert len(caps) == 1
        assert caps[0].id == "Peer"

    def test_registry_integration_missing_agents_raises(self):
        with pytest.raises(ValueError, match="non-empty 'agents'"):
            resolve_capabilities([{"Peer": {}}])


class TestCallAgentAllowlist:
    async def test_non_allowlisted_agent_returns_error_without_contacting(self):
        cap = build_peer_capability({"agents": ["researcher"]})
        call_agent = _get_call_agent(cap)

        with patch("miragen.peer.httpx.AsyncClient") as mock_client_cls:
            result = await call_agent("evil-agent", "do something")

        assert "ERROR" in result
        assert "evil-agent" in result
        assert "not in this agent's peer allowlist" in result
        mock_client_cls.assert_not_called()


class TestCallAgentHTTP:
    async def test_successful_call_returns_output(self):
        cap = build_peer_capability({"agents": ["researcher"]})
        call_agent = _get_call_agent(cap)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"output": "42"})
        mock_http, mock_client = _mock_http(post_result=mock_resp)

        with patch("miragen.peer.httpx.AsyncClient", return_value=mock_http):
            result = await call_agent("researcher", "what is the answer?")

        assert result == "42"
        mock_client.post.assert_awaited_once()
        args, kwargs = mock_client.post.call_args
        assert args[0] == "http://researcher:8000/run"
        assert kwargs["json"] == {"prompt": "what is the answer?"}

    async def test_connection_error_returns_explanatory_string(self):
        cap = build_peer_capability({"agents": ["researcher"]})
        call_agent = _get_call_agent(cap)
        mock_http, _ = _mock_http(post_side_effect=httpx.ConnectError("boom"))

        with patch("miragen.peer.httpx.AsyncClient", return_value=mock_http):
            result = await call_agent("researcher", "hi")

        assert "ERROR" in result
        assert "could not reach agent 'researcher'" in result

    async def test_timeout_returns_explanatory_string(self):
        cap = build_peer_capability({"agents": ["researcher"]})
        call_agent = _get_call_agent(cap)
        mock_http, _ = _mock_http(post_side_effect=httpx.TimeoutException("boom"))

        with patch("miragen.peer.httpx.AsyncClient", return_value=mock_http):
            result = await call_agent("researcher", "hi")

        assert "ERROR" in result
        assert "did not respond within" in result

    async def test_http_error_status_returns_explanatory_string(self):
        cap = build_peer_capability({"agents": ["researcher"]})
        call_agent = _get_call_agent(cap)

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=mock_resp)
        )
        mock_http, _ = _mock_http(post_result=mock_resp)

        with patch("miragen.peer.httpx.AsyncClient", return_value=mock_http):
            result = await call_agent("researcher", "hi")

        assert "ERROR" in result
        assert "HTTP 500" in result

    async def test_never_raises_into_model_loop(self):
        cap = build_peer_capability({"agents": ["researcher"]})
        call_agent = _get_call_agent(cap)
        mock_http, _ = _mock_http(post_side_effect=httpx.ConnectError("boom"))

        with patch("miragen.peer.httpx.AsyncClient", return_value=mock_http):
            # Should not raise.
            result = await call_agent("researcher", "hi")

        assert isinstance(result, str)
