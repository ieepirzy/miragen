"""Upstream OIDC sign-in on the bridge MCP: MCP_IDP_* / MCP_CIMD_ALLOWED_CLIENTS
parsing, the fail-closed validation, and the arguments handed to origo."""

from __future__ import annotations

import pytest

from miragen.daemon.api import (
    BRIDGE_DEFAULT_REDIRECT_URIS,
    BridgeOAuthConfigError,
    bridge_idp_from_env,
    bridge_oauth_from_env,
    bridge_oauth_provider_kwargs,
)

CIMD = "https://claude.ai/oauth/mcp-oauth-client-metadata"


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / "idp-secret"
    path.write_text("upstream-secret\n")
    return path


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "seal.key"
    path.write_bytes(b"k" * 32)
    return path


@pytest.fixture
def idp_env(secret_file):
    return {
        "MCP_BASE_URL": "https://bridge.example",
        "MCP_IDP_ISSUER": "https://id.example/realms/staff",
        "MCP_IDP_CLIENT_ID": "bridge",
        "MCP_IDP_CLIENT_SECRET_FILE": str(secret_file),
        "MCP_IDP_ALLOWED_GROUPS": "engineers, ops",
        "MCP_CIMD_ALLOWED_CLIENTS": CIMD,
    }


STATIC = {"MCP_BASE_URL": "https://bridge.example", "MCP_CLIENT_ID": "claude",
          "MCP_CLIENT_SECRET": "s3"}


# ── without MCP_IDP_*: unchanged ─────────────────────────────────────────────


def test_nothing_set_is_bearer_only(tmp_path):
    assert bridge_oauth_from_env(tmp_path, {}) is None
    assert bridge_idp_from_env({}) is None


def test_empty_strings_count_as_unset(tmp_path):
    # Compose interpolates a declared-but-unset variable to "".
    env = {**STATIC, **{name: "" for name in (
        "MCP_IDP_ISSUER", "MCP_IDP_CLIENT_ID", "MCP_IDP_CLIENT_SECRET_FILE",
        "MCP_IDP_ALLOWED_GROUPS", "MCP_IDP_SCOPES", "MCP_CIMD_ALLOWED_CLIENTS")}}
    oauth = bridge_oauth_from_env(tmp_path, env)
    assert oauth["idp"] is None and oauth["cimd_allowed_clients"] == []


def test_partial_static_without_idp_still_falls_back_to_bearer(tmp_path):
    assert bridge_oauth_from_env(tmp_path, {"MCP_BASE_URL": "https://bridge.example"}) is None


def test_static_client_kwargs_are_the_previous_flow(tmp_path):
    oauth = bridge_oauth_from_env(tmp_path, {**STATIC, "MCP_AUTO_APPROVE": "true"})
    assert oauth["storage_path"] == str(tmp_path / "oauth-state.sqlite")
    kwargs = bridge_oauth_provider_kwargs(oauth)
    assert kwargs["clients"] == {"claude": "s3"}
    assert kwargs["client_redirect_uris"] == {"claude": list(BRIDGE_DEFAULT_REDIRECT_URIS)}
    assert kwargs["auto_approve"] is True
    # origo >=0.4 gates the approve page; the confidential static client
    # needs it on, explicitly, to keep working as before.
    assert kwargs["allow_unauthenticated_consent"] is True
    assert kwargs["identity_provider"] is None
    assert kwargs["public_registration"] is False
    assert kwargs["cimd_allowed_clients"] is None
    assert kwargs["managed_sign_in"] is None


def test_static_client_provider_builds(tmp_path):
    from origo import OAuthProvider

    provider = OAuthProvider(**bridge_oauth_provider_kwargs(
        bridge_oauth_from_env(tmp_path, STATIC)))
    assert provider.allow_unauthenticated_consent is True
    assert provider.identity_provider is None


# ── with MCP_IDP_* ───────────────────────────────────────────────────────────


def test_idp_env_parsed(idp_env, key_file):
    env = {**idp_env, "MCP_IDP_GROUPS_CLAIM": "realm_access.roles",
           "MCP_IDP_SCOPES": "openid email", "MCP_IDP_MAX_GRANT_AGE": "3600",
           "MCP_IDP_RECHECK_ON_REFRESH": "true", "MCP_IDP_TOKEN_KEY_FILES": f"{key_file}, "}
    idp = bridge_idp_from_env(env)
    assert idp == {
        "issuer": "https://id.example/realms/staff",
        "client_id": "bridge",
        "client_secret": "upstream-secret",
        "allowed_groups": ["engineers", "ops"],
        "groups_claim": "realm_access.roles",
        "scopes": ["openid", "email"],
        "max_grant_age": 3600,
        "recheck_on_refresh": True,
        "token_key_files": [str(key_file)],
    }


def test_idp_defaults(idp_env):
    idp = bridge_idp_from_env(idp_env)
    assert idp["groups_claim"] == "groups"
    assert idp["scopes"] is None
    assert idp["max_grant_age"] is None
    assert idp["recheck_on_refresh"] is False
    assert idp["token_key_files"] == []


def test_secret_value_and_file_together_refuse(idp_env):
    env = {**idp_env, "MCP_IDP_CLIENT_SECRET": "from-env"}
    with pytest.raises(BridgeOAuthConfigError, match="both set"):
        bridge_idp_from_env(env)


def test_secret_resolved_by_the_startup_loader(tmp_path, idp_env):
    """Regression: miragend's startup loader turns MCP_IDP_CLIENT_SECRET_FILE
    into MCP_IDP_CLIENT_SECRET and deletes the _FILE variable; the bridge
    must still start with that secret."""
    from miragen.file_secrets import load_file_secrets

    env = dict(idp_env)
    load_file_secrets(env)
    assert "MCP_IDP_CLIENT_SECRET_FILE" not in env
    assert env["MCP_IDP_CLIENT_SECRET"] == "upstream-secret"
    oauth = bridge_oauth_from_env(tmp_path, env)
    assert oauth["idp"]["client_secret"] == "upstream-secret"


def test_secret_value_alone_counts_as_idp_config(tmp_path):
    # Any one MCP_IDP_* (the resolved secret included) turns validation on.
    with pytest.raises(BridgeOAuthConfigError, match="MCP_IDP_ISSUER"):
        bridge_oauth_from_env(tmp_path, {**STATIC, "MCP_IDP_CLIENT_SECRET": "s"})


def test_empty_resolved_secret_refuses(tmp_path, idp_env, secret_file):
    from miragen.file_secrets import load_file_secrets

    secret_file.write_text("\n")
    env = dict(idp_env)
    load_file_secrets(env)
    with pytest.raises(BridgeOAuthConfigError, match="MCP_IDP_CLIENT_SECRET"):
        bridge_oauth_from_env(tmp_path, env)


def test_idp_kwargs_cimd_only(tmp_path, idp_env):
    from origo import IdentityProvider

    oauth = bridge_oauth_from_env(tmp_path, {**idp_env, "MCP_IDP_SCOPES": "openid profile"})
    assert oauth["client_id"] is None
    kwargs = bridge_oauth_provider_kwargs(oauth)
    assert kwargs["allow_unauthenticated_consent"] is False
    assert kwargs["clients"] == {} and kwargs["client_redirect_uris"] == {}
    assert kwargs["public_registration"] is True
    assert kwargs["cimd_allowed_clients"] == [CIMD]
    idp = kwargs["identity_provider"]
    assert isinstance(idp, IdentityProvider)
    assert idp.scopes == ["openid", "profile"]


def test_idp_kwargs_keep_static_client_alongside(tmp_path, idp_env, key_file):
    env = {k: v for k, v in idp_env.items() if k != "MCP_CIMD_ALLOWED_CLIENTS"}
    env.update(STATIC)
    env.update({"MCP_IDP_RECHECK_ON_REFRESH": "1", "MCP_IDP_TOKEN_KEY_FILES": str(key_file),
                "MCP_IDP_MAX_GRANT_AGE": "7200"})
    kwargs = bridge_oauth_provider_kwargs(bridge_oauth_from_env(tmp_path, env))
    assert kwargs["clients"] == {"claude": "s3"}
    assert kwargs["allow_unauthenticated_consent"] is False
    # No CIMD allowlist: registration stays off.
    assert kwargs["public_registration"] is False
    assert kwargs["cimd_allowed_clients"] is None
    idp = kwargs["identity_provider"]
    assert idp.scopes == ["openid", "email", "profile"]  # origo's default when unset


def test_idp_provider_builds(tmp_path, idp_env):
    from origo import OAuthProvider

    provider = OAuthProvider(**bridge_oauth_provider_kwargs(
        bridge_oauth_from_env(tmp_path, {**idp_env, **STATIC})))
    assert provider.allow_unauthenticated_consent is False
    assert provider.identity_provider is not None


def test_identity_provider_arguments(tmp_path, idp_env, key_file, monkeypatch):
    import origo

    seen = {}

    class Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(origo, "IdentityProvider", Recorder)
    env = {**idp_env, "MCP_IDP_SCOPES": "openid email groups", "MCP_IDP_MAX_GRANT_AGE": "900",
           "MCP_IDP_RECHECK_ON_REFRESH": "true", "MCP_IDP_TOKEN_KEY_FILES": str(key_file)}
    bridge_oauth_provider_kwargs(bridge_oauth_from_env(tmp_path, env))
    assert seen == {
        "issuer": "https://id.example/realms/staff",
        "client_id": "bridge",
        "client_secret": "upstream-secret",
        "allowed_groups": ["engineers", "ops"],
        "groups_claim": "groups",
        "scopes": ["openid", "email", "groups"],
        "max_grant_age": 900,
        "recheck_on_refresh": True,
        "token_key_files": [str(key_file)],
    }


# ── fail closed ──────────────────────────────────────────────────────────────


def _drop(env, *names):
    return {k: v for k, v in env.items() if k not in names}


@pytest.mark.parametrize("mutate, message", [
    (lambda e: _drop(e, "MCP_IDP_ISSUER"), "MCP_IDP_ISSUER must be an https URL"),
    (lambda e: {**e, "MCP_IDP_ISSUER": "http://id.example/realms/staff"}, "https URL"),
    (lambda e: _drop(e, "MCP_IDP_CLIENT_ID"), "MCP_IDP_CLIENT_ID"),
    (lambda e: _drop(e, "MCP_IDP_CLIENT_SECRET_FILE"), "MCP_IDP_CLIENT_SECRET_FILE"),
    (lambda e: {**e, "MCP_IDP_CLIENT_SECRET_FILE": "/nonexistent/secret"}, "unreadable"),
    (lambda e: _drop(e, "MCP_IDP_ALLOWED_GROUPS"), "MCP_IDP_ALLOWED_GROUPS"),
    (lambda e: {**e, "MCP_IDP_ALLOWED_GROUPS": " , "}, "MCP_IDP_ALLOWED_GROUPS"),
    (lambda e: {**e, "MCP_IDP_SCOPES": "email profile"}, "must include openid"),
    (lambda e: {**e, "MCP_IDP_MAX_GRANT_AGE": "0"}, "positive"),
    (lambda e: {**e, "MCP_IDP_MAX_GRANT_AGE": "-5"}, "positive"),
    (lambda e: {**e, "MCP_IDP_MAX_GRANT_AGE": "1h"}, "whole seconds"),
    (lambda e: {**e, "MCP_IDP_RECHECK_ON_REFRESH": "maybe"}, "true or false"),
    (lambda e: {**e, "MCP_IDP_RECHECK_ON_REFRESH": "true"}, "MCP_IDP_TOKEN_KEY_FILES"),
    (lambda e: _drop(e, "MCP_BASE_URL"), "MCP_BASE_URL"),
    (lambda e: _drop(e, "MCP_CIMD_ALLOWED_CLIENTS"), "needs a client to sign in with"),
    (lambda e: {**e, "MCP_CLIENT_ID": "claude"}, "required together"),
    (lambda e: {**e, "MCP_CIMD_ALLOWED_CLIENTS": "claude.ai"}, "exact https client_id URL"),
    (lambda e: {**e, "MCP_CIMD_ALLOWED_CLIENTS": "https://claude.ai"}, "exact https client_id URL"),
    (lambda e: {**e, "MCP_CIMD_ALLOWED_CLIENTS": "http://claude.ai/x"}, "exact https client_id URL"),
])
def test_half_configured_idp_refuses(tmp_path, idp_env, mutate, message):
    with pytest.raises(BridgeOAuthConfigError, match=message):
        bridge_oauth_from_env(tmp_path, mutate(idp_env))


def test_any_single_idp_var_triggers_validation(tmp_path):
    with pytest.raises(BridgeOAuthConfigError):
        bridge_oauth_from_env(tmp_path, {**STATIC, "MCP_IDP_GROUPS_CLAIM": "roles"})


def test_empty_secret_file_refuses(tmp_path, idp_env, secret_file):
    secret_file.write_text("  \n")
    with pytest.raises(BridgeOAuthConfigError, match="is empty"):
        bridge_oauth_from_env(tmp_path, idp_env)


def test_cimd_without_idp_refuses(tmp_path):
    with pytest.raises(BridgeOAuthConfigError, match="without MCP_IDP_"):
        bridge_oauth_from_env(tmp_path, {**STATIC, "MCP_CIMD_ALLOWED_CLIENTS": CIMD})


# ── managed sign-in (MCP_MANAGED_SIGNIN_*) ───────────────────────────────────

ASSERTION_ISSUER = "https://issuer.example/issuers/iss_123"


def test_managed_unset_is_none(tmp_path, idp_env):
    oauth = bridge_oauth_from_env(tmp_path, idp_env)
    assert oauth["managed_sign_in"] is None
    assert bridge_oauth_provider_kwargs(oauth)["managed_sign_in"] is None


def test_managed_env_parsed(idp_env):
    from miragen.daemon.api import bridge_managed_sign_in_from_env

    assert bridge_managed_sign_in_from_env({"MCP_MANAGED_SIGNIN_ISSUER": ASSERTION_ISSUER}) == {
        "issuer": ASSERTION_ISSUER, "tenant": None, "directory_url": None}
    assert bridge_managed_sign_in_from_env({
        "MCP_MANAGED_SIGNIN_ISSUER": ASSERTION_ISSUER, "MCP_MANAGED_SIGNIN_TENANT": " t-1 ",
        "MCP_MANAGED_SIGNIN_DIRECTORY_URL": "http://idp.idp.svc.cluster.local:8080",
    }) == {"issuer": ASSERTION_ISSUER, "tenant": "t-1",
           "directory_url": "http://idp.idp.svc.cluster.local:8080"}


def test_managed_reaches_oauth_provider(tmp_path, idp_env, monkeypatch):
    from origo import ManagedSignIn, OAuthProvider

    monkeypatch.delenv("ORIGO_MANAGED_SIGNIN_DIRECTORY_URL", raising=False)
    monkeypatch.delenv("ORIGO_MANAGED_SIGNIN_DIRECTORY_CLIENT_ID", raising=False)
    monkeypatch.delenv("ORIGO_MANAGED_SIGNIN_DIRECTORY_CLIENT_SECRET_FILE", raising=False)
    env = {**idp_env, "MCP_MANAGED_SIGNIN_ISSUER": ASSERTION_ISSUER,
           "MCP_MANAGED_SIGNIN_TENANT": "t-1",
           "MCP_MANAGED_SIGNIN_DIRECTORY_URL": "http://idp.idp.svc.cluster.local:8080"}
    kwargs = bridge_oauth_provider_kwargs(bridge_oauth_from_env(tmp_path, env))
    managed = kwargs["managed_sign_in"]
    assert isinstance(managed, ManagedSignIn)
    assert managed.issuer == ASSERTION_ISSUER
    assert managed.tenant == "t-1"
    assert managed.directory_url == "http://idp.idp.svc.cluster.local:8080"
    assert managed.group_paths is False
    provider = OAuthProvider(**kwargs)
    assert provider.managed_sign_in is managed


def test_managed_arguments(tmp_path, idp_env, monkeypatch):
    import origo

    seen = {}

    class Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(origo, "ManagedSignIn", Recorder)
    env = {**idp_env, "MCP_MANAGED_SIGNIN_ISSUER": ASSERTION_ISSUER}
    bridge_oauth_provider_kwargs(bridge_oauth_from_env(tmp_path, env))
    assert seen == {"issuer": ASSERTION_ISSUER, "tenant": None, "directory_url": None}


@pytest.mark.parametrize("mutate, message", [
    (lambda e: {**e, "MCP_MANAGED_SIGNIN_ISSUER": "http://issuer.example/x"}, "https URL"),
    (lambda e: {**e, "MCP_MANAGED_SIGNIN_ISSUER": "issuer.example"}, "https URL"),
    (lambda e: {**_drop(e, "MCP_MANAGED_SIGNIN_ISSUER"), "MCP_MANAGED_SIGNIN_TENANT": "t-1"},
     "without MCP_MANAGED_SIGNIN_ISSUER"),
    (lambda e: {**_drop(e, "MCP_MANAGED_SIGNIN_ISSUER"),
                "MCP_MANAGED_SIGNIN_DIRECTORY_URL": "https://idp.example"},
     "without MCP_MANAGED_SIGNIN_ISSUER"),
    (lambda e: {**_drop(e, "MCP_CIMD_ALLOWED_CLIENTS"), **STATIC}, "MCP_CIMD_ALLOWED_CLIENTS"),
])
def test_half_configured_managed_refuses(tmp_path, idp_env, mutate, message):
    env = {**idp_env, "MCP_MANAGED_SIGNIN_ISSUER": ASSERTION_ISSUER}
    with pytest.raises(BridgeOAuthConfigError, match=message):
        bridge_oauth_from_env(tmp_path, mutate(env))


def test_managed_without_idp_refuses(tmp_path):
    with pytest.raises(BridgeOAuthConfigError, match="needs MCP_IDP_"):
        bridge_oauth_from_env(tmp_path, {**STATIC, "MCP_MANAGED_SIGNIN_ISSUER": ASSERTION_ISSUER})


def test_managed_bad_directory_url_refuses_at_startup(tmp_path, idp_env):
    # origo's own rule: plain http only to an in-cluster service name.
    env = {**idp_env, "MCP_MANAGED_SIGNIN_ISSUER": ASSERTION_ISSUER,
           "MCP_MANAGED_SIGNIN_DIRECTORY_URL": "http://idp.example:8080"}
    oauth = bridge_oauth_from_env(tmp_path, env)
    with pytest.raises(ValueError):
        bridge_oauth_provider_kwargs(oauth)
