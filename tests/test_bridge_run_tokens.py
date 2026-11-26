"""Run tokens on the bridge's /mcp (MCP_RUN_TOKEN_*): a short-lived ES256
JWT an agent run presents. Verified against an injected JWKS and clock with
real keys; the mount is driven over HTTP."""

from __future__ import annotations

import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from miragen.daemon.api import BridgeOAuthConfigError, bridge_run_tokens_from_env, create_app
from miragen.daemon.sessions.run_tokens import (
    JWKS_MAX_STALE_SECONDS,
    RunTokenSettings,
    RunTokenVerifier,
)
from tests.test_sessions_plane import Harness

NOW = 1_800_000_000
ISSUER = "urn:example:workload"
AUDIENCE = "memory"
TENANT = "acme"
AGENT = "agent.acme.profile.engineer"
JWKS_URL = "https://issuer.example/workload-jwks.json"
KID = "k1"

ACCEPT = {"Accept": "application/json, text/event-stream"}
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}


def _key():
    return ec.generate_private_key(ec.SECP256R1())


SIGNING_KEY = _key()


def _jwks(key=SIGNING_KEY, kid=KID, *, private=False) -> dict:
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key if private else key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
    return {"keys": [jwk]}


def _token(*, key=SIGNING_KEY, kid=KID, alg="ES256", drop=(), **overrides) -> str:
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "iat": NOW - 10, "exp": NOW + 890, "jti": "j1",
        "sub": AGENT, "act": {"sub": AGENT}, "tenant": TENANT, "workload": "w-1",
        "run_intent_id": "run-123", "routine_revision_id": "rr-1",
    }
    claims.update(overrides)
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def _settings(**overrides) -> RunTokenSettings:
    base = dict(jwks_url=JWKS_URL, issuer=ISSUER, audience=AUDIENCE, tenant=TENANT,
                agents=frozenset({AGENT}))
    base.update(overrides)
    return RunTokenSettings(**base)


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _verifier(document=None, *, clock=None, settings=None, calls=None):
    def fetch(url):
        assert url == JWKS_URL
        if calls is not None:
            calls.append(url)
        if isinstance(document, Exception):
            raise document
        return _jwks() if document is None else document
    return RunTokenVerifier(settings or _settings(), fetch=fetch, clock=clock or Clock())


# ── verification ─────────────────────────────────────────────────────────────


def test_a_valid_token_proves_the_agent_and_its_run():
    caller = _verifier().caller(_token(orig="oidc:person-1", obo="oidc:person-1"))
    assert caller is not None
    assert caller.principal == AGENT
    assert caller.run_id == "run-123"
    assert caller.originator == "oidc:person-1" and caller.root == "oidc:person-1"
    # Without the optional chain claims the caller is still the agent.
    assert _verifier().caller(_token()).originator is None


@pytest.mark.parametrize("token", [
    pytest.param(_token(key=_key()), id="forged-signature"),
    pytest.param(_token(exp=NOW - 31, iat=NOW - 600), id="expired"),
    pytest.param(_token(aud="design"), id="wrong-audience"),
    pytest.param(_token(iss="urn:other:workload"), id="wrong-issuer"),
    pytest.param(_token(tenant="other"), id="wrong-tenant"),
    pytest.param(_token(sub="agent.acme.profile.other"), id="sub-not-act-sub"),
    pytest.param(_token(act={"sub": "agent.acme.profile.other"}, sub="agent.acme.profile.other"),
                 id="agent-not-admitted"),
    pytest.param(_token(act="agent.acme.profile.engineer"), id="act-not-an-object"),
    pytest.param(_token(sub="agent.other.profile.engineer", act={"sub": "agent.other.profile.engineer"}),
                 id="principal-of-another-tenant"),
    pytest.param(_token(exp=NOW + 2000), id="lifetime-over-900s"),
    pytest.param(_token(iat=NOW + 120, exp=NOW + 600), id="issued-in-the-future"),
    pytest.param(_token(nbf=NOW + 120), id="not-yet-valid"),
    pytest.param(_token(drop=("exp",)), id="no-exp"),
    pytest.param(_token(drop=("iat",)), id="no-iat"),
    pytest.param(_token(kid="unknown"), id="unknown-kid"),
    pytest.param(jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": AGENT, "act": {"sub": AGENT},
                             "tenant": TENANT, "iat": NOW, "exp": NOW + 60},
                            "s" * 32, algorithm="HS256", headers={"kid": KID}), id="hs256"),
    pytest.param("not-a-jwt", id="opaque"),
    pytest.param("", id="empty"),
])
def test_refused(token):
    assert _verifier().caller(token) is None


def test_a_jwks_entry_with_private_material_is_ignored():
    assert _verifier(_jwks(private=True)).caller(_token()) is None


def test_keys_go_stale_after_an_hour_of_failed_fetches():
    clock = Clock()
    state = {"fail": False}

    def fetch(url):
        if state["fail"]:
            raise OSError("down")
        return _jwks()

    verifier = RunTokenVerifier(_settings(), fetch=fetch, clock=clock)
    assert verifier.caller(_token()) is not None
    state["fail"] = True
    clock.now = NOW + 600  # past the 5-minute TTL: refetch fails, last good set kept
    assert verifier.caller(_token(iat=int(clock.now) - 10, exp=int(clock.now) + 800)) is not None
    clock.now = NOW + JWKS_MAX_STALE_SECONDS + 1
    assert verifier.caller(_token(iat=int(clock.now) - 10, exp=int(clock.now) + 800)) is None


def test_an_unknown_kid_refetches_at_most_once_a_minute():
    calls: list[str] = []
    verifier = _verifier(calls=calls)
    assert verifier.caller(_token()) is not None
    for _ in range(5):
        assert verifier.caller(_token(kid="rotated")) is None
    assert len(calls) == 1


# ── settings ─────────────────────────────────────────────────────────────────


FULL_ENV = {
    "MCP_RUN_TOKEN_JWKS_URL": JWKS_URL,
    "MCP_RUN_TOKEN_ISSUER": ISSUER,
    "MCP_RUN_TOKEN_AUDIENCE": AUDIENCE,
    "MCP_RUN_TOKEN_TENANT": TENANT,
    "MCP_RUN_TOKEN_AGENTS": f"{AGENT}, agent.acme.profile.reviewer",
}


def test_settings_off_when_nothing_is_set():
    assert bridge_run_tokens_from_env({}) is None
    assert bridge_run_tokens_from_env({"MCP_RUN_TOKEN_ISSUER": ""}) is None


def test_settings_full():
    settings = bridge_run_tokens_from_env(FULL_ENV)
    assert settings == RunTokenSettings(
        jwks_url=JWKS_URL, issuer=ISSUER, audience=AUDIENCE, tenant=TENANT,
        agents=frozenset({AGENT, "agent.acme.profile.reviewer"}))


@pytest.mark.parametrize("missing", sorted(FULL_ENV))
def test_half_configuration_refuses_to_start(missing):
    env = {k: v for k, v in FULL_ENV.items() if k != missing}
    with pytest.raises(BridgeOAuthConfigError, match="MCP_RUN_TOKEN"):
        bridge_run_tokens_from_env(env)


@pytest.mark.parametrize("override", [
    {"MCP_RUN_TOKEN_JWKS_URL": "http://issuer.svc.cluster.local:8090/workload-jwks.json"},
    {"MCP_RUN_TOKEN_JWKS_URL": "ftp://issuer.example/jwks"},
    {"MCP_RUN_TOKEN_JWKS_URL": "https://user:pw@issuer.example/jwks"},
    {"MCP_RUN_TOKEN_TENANT": "Not A Tenant"},
    {"MCP_RUN_TOKEN_AGENTS": "agent.other.profile.engineer"},
    {"MCP_RUN_TOKEN_AGENTS": "agent.acme.engineer"},
    {"MCP_RUN_TOKEN_AGENTS": "*"},
    {"MCP_RUN_TOKEN_JWKS_ALLOW_HTTP": "maybe"},
])
def test_bad_settings_refuse_to_start(override):
    with pytest.raises(BridgeOAuthConfigError):
        bridge_run_tokens_from_env({**FULL_ENV, **override})


def test_plain_http_jwks_only_with_the_explicit_opt_in():
    url = "http://issuer.svc.cluster.local:8090/workload-jwks.json"
    settings = bridge_run_tokens_from_env(
        {**FULL_ENV, "MCP_RUN_TOKEN_JWKS_URL": url, "MCP_RUN_TOKEN_JWKS_ALLOW_HTTP": "true"})
    assert settings is not None and settings.jwks_url == url


# ── the mount ────────────────────────────────────────────────────────────────


OAUTH = {"base_url": "https://memory.example", "client_id": "claude", "client_secret": "s3",
         "auto_approve": True, "storage_path": None,
         "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}


def _app(tmp_path, *, oauth=OAUTH, verifier=None):
    h = Harness(tmp_path)
    return create_app(None, token="secret", sessions=h.plane, bridge_oauth=oauth,
                      bridge_run_tokens=verifier or _verifier())


def _bearer(token: str) -> dict:
    return {**ACCEPT, "Authorization": f"Bearer {token}"}


def test_a_run_token_opens_mcp_and_nothing_else(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").json()["sessions"]["mcp"]["run_tokens"] is True
        token = _token()
        assert client.post("/mcp", headers=_bearer(token), json=PING).status_code == 200
        assert client.post("/mcp/", headers=_bearer(token), json=PING).status_code == 200
        # The session API keeps the daemon bearer: a run token is refused there.
        assert client.get("/sessions/v1/stats",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert client.get("/sessions/v1/stats",
                          headers={"Authorization": "Bearer secret"}).status_code == 200


def test_bridge_status_names_the_verified_caller(tmp_path):
    app = _app(tmp_path)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "bridge_status", "arguments": {}}}
    with TestClient(app) as client:
        res = client.post("/mcp", headers=_bearer(_token(orig="oidc:person-1")), json=call)
        assert res.status_code == 200
        status = json.loads(res.json()["result"]["content"][0]["text"])
        assert status["caller"] == {"kind": "run_token", "principal": AGENT, "run_id": "run-123",
                                    "originator": "oidc:person-1", "root": None}
        assert status["principal"] == "assistant"  # the daemon's memory principal


@pytest.mark.parametrize("bad", [
    pytest.param(_token(key=_key()), id="forged"),
    pytest.param(_token(exp=NOW - 31, iat=NOW - 600), id="expired"),
    pytest.param(_token(aud="other"), id="wrong-audience"),
])
def test_bad_run_tokens_get_the_oauth_challenge(tmp_path, bad):
    app = _app(tmp_path)
    with TestClient(app) as client:
        refused = client.post("/mcp", headers=_bearer(bad), json=PING)
        assert refused.status_code == 401
        assert "resource_metadata=" in refused.headers["www-authenticate"]


def test_the_daemon_bearer_is_still_refused_on_mcp_with_oauth(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/mcp", headers=_bearer("secret"), json=PING).status_code == 401
        # Positive control in the same app.
        assert client.post("/mcp", headers=_bearer(_token()), json=PING).status_code == 200


def test_without_oauth_the_bearer_and_run_tokens_both_open_mcp(tmp_path):
    app = _app(tmp_path, oauth=None)
    with TestClient(app) as client:
        assert client.post("/mcp", headers=_bearer("secret"), json=PING).status_code == 200
        assert client.post("/mcp", headers=_bearer(_token()), json=PING).status_code == 200
        assert client.post("/mcp", headers=_bearer(_token(aud="other")), json=PING).status_code == 401
        assert client.post("/mcp", json=PING, headers=ACCEPT).status_code == 401


def test_run_tokens_off_by_default(tmp_path):
    h = Harness(tmp_path)
    app = create_app(None, token="secret", sessions=h.plane, bridge_oauth=OAUTH)
    with TestClient(app) as client:
        assert client.get("/health").json()["sessions"]["mcp"]["run_tokens"] is False
        assert client.post("/mcp", headers=_bearer(_token()), json=PING).status_code == 401


def test_every_daemon_start_path_reads_the_bridge_settings():
    """main() builds the app in two places (session plane alone, and with
    the lifecycle plane). Both must read MCP_RUN_TOKEN_* as they read the
    OAuth settings, or one kind of daemon silently ignores them."""
    import ast
    import inspect

    from miragen.daemon import api

    tree = ast.parse(inspect.getsource(api.main).lstrip())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "create_app"]
    assert len(calls) == 2
    for call in calls:
        assert {"bridge_oauth", "bridge_run_tokens"} <= {k.arg for k in call.keywords}
