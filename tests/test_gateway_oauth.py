"""Gateway OAuth to origo-style upstreams (auto-approve), bridge session
binding, and backoff for unreachable upstreams."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from miragen.harness.gateway import GatewayConfigError, ToolGateway
from miragen.harness.upstream_auth import OAuthUpstream, UpstreamAuthError
from miragen.models import AgentProfile
from tests.test_tool_gateway import memory_client_factory, text, upstream_server

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


class FakeOrigo:
    """Enough of origo: RFC 8414 metadata, auto-approving /authorize with
    PKCE S256, /token with client_secret_post and refresh, open /register."""

    def __init__(self, *, auto_approve=True, secret="s3cret"):
        self.auto_approve, self.secret = auto_approve, secret
        self.codes: dict[str, str] = {}
        self.issued: list[str] = []
        self.registered: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={
                "issuer": "https://db.example", "authorization_endpoint": "https://db.example/authorize",
                "token_endpoint": "https://db.example/token", "registration_endpoint": "https://db.example/register"})
        if path == "/authorize":
            q = dict(request.url.params)
            if not self.auto_approve:
                return httpx.Response(200, text="<html>consent</html>")
            if q["redirect_uri"] != REDIRECT or q["code_challenge_method"] != "S256":
                return httpx.Response(400, json={"error": "invalid_request"})
            code = f"code{len(self.codes)}"
            self.codes[code] = q["code_challenge"]
            return httpx.Response(302, headers={"location": f"{REDIRECT}?code={code}&state={q['state']}"})
        if path == "/token":
            form = parse_qs(request.content.decode())
            f = {k: v[0] for k, v in form.items()}
            if f.get("client_secret") != self.secret:
                return httpx.Response(401, json={"error": "invalid_client"})
            if f["grant_type"] == "authorization_code":
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(f["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                if self.codes.pop(f["code"], None) != challenge:
                    return httpx.Response(400, json={"error": "invalid_grant"})
            elif f["grant_type"] == "refresh_token" and f["refresh_token"] != "refresh-1":
                return httpx.Response(400, json={"error": "invalid_grant"})
            token = f"access-{len(self.issued)}"
            self.issued.append(token)
            return httpx.Response(200, json={"access_token": token, "refresh_token": "refresh-1",
                                             "expires_in": 3600, "token_type": "bearer"})
        if path == "/register":
            body = json.loads(request.content)
            self.registered.append(body)
            return httpx.Response(201, json={"client_id": "dyn-1", "client_secret": self.secret})
        return httpx.Response(404)


def auth(origo, **cfg) -> OAuthUpstream:
    return OAuthUpstream.from_config("https://db.example/mcp", {"redirect_uri": REDIRECT, **cfg},
                                     {"DB_SECRET": "s3cret"}, transport=httpx.MockTransport(origo.handler))


async def test_auto_approved_code_flow_with_pkce_then_cache_then_refresh():
    origo = FakeOrigo()
    a = auth(origo, client_id="mira", client_secret_env="DB_SECRET")
    assert await a.token() == "access-0"
    assert await a.token() == "access-0"          # cached
    a._expires_at = 0                              # expire it
    assert await a.token() == "access-1" and origo.codes == {}   # refreshed, no new code


async def test_consent_page_is_refused_not_filled_in():
    a = auth(FakeOrigo(auto_approve=False), client_id="mira", client_secret_env="DB_SECRET")
    with pytest.raises(UpstreamAuthError, match="auto-approve"):
        await a.token()


async def test_dynamic_registration_for_public_servers():
    origo = FakeOrigo()
    a = auth(origo, register=True)
    assert await a.token() == "access-0"
    assert origo.registered[0]["redirect_uris"] == [REDIRECT] and a.client_id == "dyn-1"


def test_config_errors():
    origo = FakeOrigo()
    with pytest.raises(ValueError, match="redirect_uri"):
        OAuthUpstream.from_config("https://x/mcp", {"client_id": "a"}, {})
    with pytest.raises(ValueError, match="not set"):
        auth(origo, client_id="mira", client_secret_env="MISSING")
    with pytest.raises(ValueError, match="client_id"):
        auth(origo)


def profile(**cap) -> AgentProfile:
    return AgentProfile.model_validate({
        "name": "g", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "grok-build:x", "instructions": "i",
                 "capabilities": [{"MCP": {"url": "https://db.example/mcp", "name": "db", **cap}}]}})


async def test_gateway_uses_the_oauth_token_and_the_bridge_session_header():
    origo = FakeOrigo()
    seen = []
    gw = ToolGateway(
        profile(oauth={"client_id": "mira", "client_secret_env": "DB_SECRET", "redirect_uri": REDIRECT},
                bridge_session=True),
        env={"DB_SECRET": "s3cret"}, upstream_client=memory_client_factory(upstream_server(), seen),
        oauth_transport=httpx.MockTransport(origo.handler),
        session_key_for=lambda inst: f"miragen:mira-{inst}")
    with gw.turn("conv_1", "r1"):
        res = await gw.call_tool("db_lookup", {"key": "k"}, instance="conv_1")
    assert text(res) == "value-of-k"
    headers = seen[-1][1]
    assert headers["Authorization"] == "Bearer access-0"
    assert headers["X-Harness-Session"] == "miragen:mira-conv_1"


def test_gateway_rejects_bearer_plus_oauth():
    with pytest.raises(GatewayConfigError, match="not both"):
        ToolGateway(profile(bearer_token_env="T", oauth={"client_id": "a", "redirect_uri": REDIRECT}),
                    env={"T": "x"})


def test_gateway_only_keys_are_refused_on_pydantic_models():
    from miragen.load import resolve_capabilities
    with pytest.raises(ValueError, match="tool gateway"):
        resolve_capabilities([{"MCP": {"url": "https://x/mcp", "name": "x", "bridge_session": True}}])


async def test_unreachable_upstream_backs_off_instead_of_slowing_every_turn():
    import contextlib
    attempts = []

    @contextlib.asynccontextmanager
    async def down(url, headers=None):
        attempts.append(url)
        raise httpx.ConnectError("refused")
        yield  # pragma: no cover

    gw = ToolGateway(profile(), env={}, upstream_client=down)
    await gw._list_tools()
    await gw._list_tools()
    assert len(attempts) == 1   # second listing skipped during backoff


async def test_a_redirect_with_a_foreign_state_is_refused():
    origo = FakeOrigo()
    real = origo.handler

    def tampered(request):
        resp = real(request)
        if request.url.path == "/authorize" and resp.status_code == 302:
            loc = resp.headers["location"].split("&state=")[0] + "&state=attacker"
            return httpx.Response(302, headers={"location": loc})
        return resp
    a = OAuthUpstream.from_config("https://db.example/mcp",
                                  {"redirect_uri": REDIRECT, "client_id": "mira",
                                   "client_secret_env": "DB_SECRET"},
                                  {"DB_SECRET": "s3cret"}, transport=httpx.MockTransport(tampered))
    with pytest.raises(UpstreamAuthError, match="state"):
        await a.token()


def test_optional_servers_without_credentials_are_skipped_required_ones_fail():
    gw = ToolGateway(profile(bearer_token_env="MISSING", optional=True), env={})
    assert gw._upstreams == {}
    gw = ToolGateway(profile(oauth={"client_id": "a", "client_secret_env": "MISSING",
                                    "redirect_uri": REDIRECT}, optional=True), env={})
    assert gw._upstreams == {}
    with pytest.raises(GatewayConfigError, match="not set"):
        ToolGateway(profile(oauth={"client_id": "a", "client_secret_env": "MISSING",
                                   "redirect_uri": REDIRECT}), env={})
