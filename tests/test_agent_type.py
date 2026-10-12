"""agent_type: internal | external | hybrid — the load-time rules for
public-facing profiles (miragen/agent_type.py).

Every case goes through `load_profile`, the path `miragen run`, `miragen
validate` and miragend's `/validate` share; the miragend side is pinned in
tests/test_daemon_core.py and tests/test_daemon_api.py.
"""

import pytest
import yaml

from miragen.agent_type import ALLOWED_HOSTS_ENV, ALLOWED_PROVIDERS_ENV, parse_allowed_providers
from miragen.load import load_profile


@pytest.fixture(autouse=True)
def _no_ambient_allowlist(monkeypatch):
    # The developer's or CI's own environment must not decide a test.
    monkeypatch.delenv(ALLOWED_PROVIDERS_ENV, raising=False)
    monkeypatch.delenv(ALLOWED_HOSTS_ENV, raising=False)


def _profile(agent_type=None, **spec_overrides):
    doc = {
        "name": "support-chat",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "spec": {
            "model": "anthropic:claude-sonnet-4-6",
            "instructions": "Answer questions about the public documentation.",
            **spec_overrides,
        },
    }
    if agent_type is not None:
        doc["agent_type"] = agent_type
    return doc


EXECUTOR = {
    "name": "support-chat",
    "mode": "interactive",
    "triggers": [{"type": "http"}],
    "executor": {"executor": "claude-code", "instructions": "review"},
}

MEMORY = {
    "backend": "ephemeral",
    "scopes": {"read": ["s"], "propose": ["s"], "default_write": "s"},
}


def _write(tmp_path, doc):
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


def _load(tmp_path, doc, allowed="anthropic,openai", hosts=None):
    return load_profile(_write(tmp_path, doc), allowed_providers=allowed, allowed_hosts=hosts)


# ── internal (default) ───────────────────────────────────────────────────────


def test_internal_is_the_default(tmp_path):
    assert _load(tmp_path, _profile(), allowed=None).agent_type == "internal"


def test_internal_is_unaffected_by_any_rule(tmp_path):
    # Harness model, interpolation, memory, no allowlist: none of it
    # matters for an internal profile.
    doc = _profile("internal", model="claude-code:sonnet")
    doc["memory"] = MEMORY
    assert _load(tmp_path, doc, allowed=None).agent_type == "internal"
    assert load_profile(_write(tmp_path, EXECUTOR)).agent_type == "internal"


@pytest.mark.parametrize("raw", ["${AT:-internal}", "${AT}", "inter${X:-}nal"])
def test_interpolated_agent_type_fails_for_every_profile(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("AT", "internal")
    doc = _profile()
    doc["agent_type"] = raw
    with pytest.raises(ValueError, match=r"agent_type: may not use \$\{VAR\} interpolation"):
        _load(tmp_path, doc, allowed=None)


# ── external / hybrid ────────────────────────────────────────────────────────


@pytest.fixture(params=["external", "hybrid"])
def public(request):
    return request.param


def test_allowed_provider_passes(tmp_path, public):
    assert _load(tmp_path, _profile(public)).agent_type == public


def test_allowlist_entries_are_normalized(tmp_path, public):
    assert _load(tmp_path, _profile(public), allowed=" OpenAI , ANTHROPIC ,").agent_type == public


@pytest.mark.parametrize("model", [
    "ANTHROPIC:claude-sonnet-4-6",
    "Anthropic:claude-sonnet-4-6",
    " anthropic :claude-sonnet-4-6",
    "anthropic :claude-sonnet-4-6",
])
def test_profile_provider_is_compared_exactly_as_written(tmp_path, public, model):
    # pydantic-ai would fail on these at build; refuse them at validation.
    with pytest.raises(ValueError, match=r"spec\.model: provider .* is not in"):
        _load(tmp_path, _profile(public, model=model))


@pytest.mark.parametrize("model", ["CLAUDE-CODE:sonnet", " codex:gpt-5"])
def test_harness_lookalikes_are_not_providers_either(tmp_path, public, model):
    # Not the harness prefix as written, so not a harness — and not an
    # allowed provider either, even with the harness names listed.
    with pytest.raises(ValueError, match=r"spec\.model: provider .* is not in"):
        _load(tmp_path, _profile(public, model=model), allowed="anthropic,claude-code,codex")


def test_disallowed_provider_fails(tmp_path, public):
    with pytest.raises(ValueError, match=r"spec\.model: provider 'google-gla' is not in "
                                         + ALLOWED_PROVIDERS_ENV):
        _load(tmp_path, _profile(public, model="google-gla:gemini-2.5-pro"))


def test_provider_must_match_exactly_not_by_prefix(tmp_path, public):
    with pytest.raises(ValueError, match="'openai-responses' is not in"):
        _load(tmp_path, _profile(public, model="openai-responses:gpt-5"), allowed="openai")


def test_model_without_provider_fails(tmp_path, public):
    with pytest.raises(ValueError, match=r"spec\.model: .*'provider:model'"):
        _load(tmp_path, _profile(public, model="gpt-5"))


@pytest.mark.parametrize("model", ["claude-code:sonnet", "codex:gpt-5", "grok-build:grok-4"])
def test_harness_model_fails_even_when_listed(tmp_path, public, model):
    # The allowlist names the harness too, so only the harness rule can
    # refuse this — the provider check alone would let it through.
    allowed = "anthropic,claude-code,codex,grok-build"
    with pytest.raises(ValueError, match=r"spec\.model: .*subscription harness"):
        _load(tmp_path, _profile(public, model=model), allowed=allowed)


def test_executor_profile_fails(tmp_path, public):
    with pytest.raises(ValueError, match=r"executor: .*base tier"):
        _load(tmp_path, {**EXECUTOR, "agent_type": public})


def test_memory_block_fails(tmp_path, public):
    doc = _profile(public)
    doc["memory"] = MEMORY
    with pytest.raises(ValueError, match=r"memory: .*`memory:` block"):
        _load(tmp_path, doc)


def test_interpolated_model_fails(tmp_path, public, monkeypatch):
    # Set, and to an allowed provider: the refusal is about not being able
    # to check the value at validation time, not about what it resolves to.
    monkeypatch.setenv("SUPPORT_MODEL", "anthropic:claude-sonnet-4-6")
    with pytest.raises(ValueError, match=r"spec\.model: .*\$\{VAR\} interpolation"):
        _load(tmp_path, _profile(public, model="${SUPPORT_MODEL}"))


def test_interpolated_model_with_default_fails(tmp_path, public):
    with pytest.raises(ValueError, match="interpolation"):
        _load(tmp_path, _profile(public, model="${SUPPORT_MODEL:-anthropic:claude-sonnet-4-6}"))


def test_image_generation_fallback_model_is_checked(tmp_path, public):
    ok = _profile(public, capabilities=[{"ImageGeneration": {"fallback_model": "openai-responses:gpt-5.4"}}])
    assert _load(tmp_path, ok, allowed="anthropic,openai-responses").agent_type == public

    bad = _profile(public, capabilities=[
        "WebSearch",
        {"ImageGeneration": {"fallback_model": "google-gla:gemini-2.5-flash"}},
    ])
    with pytest.raises(ValueError, match=r"spec\.capabilities\[1\]\.ImageGeneration"
                                         r"\.fallback_model: provider 'google-gla'"):
        _load(tmp_path, bad)


def test_image_generation_fallback_model_interpolation_fails(tmp_path, public, monkeypatch):
    monkeypatch.setenv("IMAGE_MODEL", "openai-responses:gpt-5.4")
    doc = _profile(public, capabilities=[{"ImageGeneration": {"fallback_model": "${IMAGE_MODEL}"}}])
    with pytest.raises(ValueError, match=r"fallback_model: .*interpolation"):
        _load(tmp_path, doc)


def test_cloud_voice_provider_must_be_allowed(tmp_path, public):
    doc = _profile(public)
    doc["voice"] = {"provider": "openai"}
    assert _load(tmp_path, doc, allowed="anthropic,openai").agent_type == public
    with pytest.raises(ValueError, match=r"voice\.provider: speech provider 'openai' is not in"):
        _load(tmp_path, doc, allowed="anthropic")


def test_http_voice_endpoint_host_must_be_allowed(tmp_path, public):
    doc = _profile(public)
    doc["voice"] = {"provider": "http", "url": "https://tts.example.com/speak"}
    # Not a provider: only the host list matters.
    assert _load(tmp_path, doc, allowed="anthropic", hosts="tts.example.com").agent_type == public
    with pytest.raises(ValueError, match=r"voice\.url: host 'tts\.example\.com' is not in "
                                         + ALLOWED_HOSTS_ENV):
        _load(tmp_path, doc, hosts="mcp.example.com")
    with pytest.raises(ValueError, match=rf"voice\.url: .*set {ALLOWED_HOSTS_ENV}"):
        _load(tmp_path, doc, hosts=None)


def test_interpolated_voice_url_fails(tmp_path, public, monkeypatch):
    monkeypatch.setenv("TTS_URL", "https://tts.example.com/speak")
    doc = _profile(public)
    doc["voice"] = {"provider": "http", "url": "${TTS_URL}"}
    with pytest.raises(ValueError, match=r"voice\.url: .*interpolation"):
        _load(tmp_path, doc, hosts="tts.example.com")


def _mcp(url):
    return {"MCP": {"url": url, "name": "docs"}}


def test_mcp_host_on_the_allowlist_passes(tmp_path, public):
    doc = _profile(public, capabilities=[_mcp("https://mcp.example.com/mcp")])
    assert _load(tmp_path, doc, hosts="MCP.example.com, other.example.org").agent_type == public


@pytest.mark.parametrize("url, host", [
    ("https://internal.example.net/mcp", "internal.example.net"),
    ("https://sub.mcp.example.com/mcp", "sub.mcp.example.com"),       # no implied wildcard
    ("https://mcp.example.com@evil.example.net/mcp", "evil.example.net"),  # userinfo trick
])
def test_mcp_host_not_on_the_allowlist_fails(tmp_path, public, url, host):
    doc = _profile(public, capabilities=["Thinking", _mcp(url)])
    with pytest.raises(ValueError, match=rf"spec\.capabilities\[1\]\.MCP\.url: host '{host}' is not in"):
        _load(tmp_path, doc, hosts="mcp.example.com")


@pytest.mark.parametrize("hosts", [None, "", " , "])
def test_mcp_without_a_host_allowlist_fails_closed(tmp_path, public, hosts):
    doc = _profile(public, capabilities=[_mcp("https://mcp.example.com/mcp")])
    with pytest.raises(ValueError, match=rf"MCP\.url: .*set {ALLOWED_HOSTS_ENV}"):
        _load(tmp_path, doc, hosts=hosts)


def test_no_mcp_needs_no_host_allowlist(tmp_path, public):
    assert _load(tmp_path, _profile(public), hosts=None).agent_type == public


def test_interpolated_mcp_url_fails(tmp_path, public, monkeypatch):
    monkeypatch.setenv("DOCS_MCP_URL", "https://mcp.example.com/mcp")
    doc = _profile(public, capabilities=[_mcp("${DOCS_MCP_URL}")])
    with pytest.raises(ValueError, match=r"MCP\.url: .*interpolation"):
        _load(tmp_path, doc, hosts="mcp.example.com")


def test_host_allowlist_is_read_from_the_environment(tmp_path, public, monkeypatch):
    doc = _profile(public, capabilities=[_mcp("https://mcp.example.com/mcp")])
    monkeypatch.setenv(ALLOWED_PROVIDERS_ENV, "anthropic")
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "mcp.example.com")
    assert load_profile(_write(tmp_path, doc)).agent_type == public
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "other.example.com")
    with pytest.raises(ValueError, match="'mcp.example.com' is not in"):
        load_profile(_write(tmp_path, doc))


def test_internal_mcp_needs_no_host_allowlist(tmp_path):
    doc = _profile("internal", capabilities=[_mcp("https://internal.example.net/mcp")])
    assert _load(tmp_path, doc, allowed=None, hosts=None).agent_type == "internal"


def test_interpolated_voice_provider_fails(tmp_path, public, monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "openai")
    doc = _profile(public)
    doc["voice"] = {"provider": "${TTS_PROVIDER}"}
    with pytest.raises(ValueError, match=r"voice\.provider: .*interpolation"):
        _load(tmp_path, doc, allowed="anthropic,openai")


def test_peer_capability_fails(tmp_path, public):
    doc = _profile(public, capabilities=["Thinking", {"Peer": {"agents": ["researcher"]}}])
    with pytest.raises(ValueError, match=r"spec\.capabilities\[1\]\.Peer: .*Peer capability"):
        _load(tmp_path, doc)


def test_safe_builtin_capabilities_pass(tmp_path, public):
    doc = _profile(public, capabilities=[
        "Thinking", "ImageGeneration", {"WebFetch": {"local": False}},
    ])
    assert _load(tmp_path, doc).agent_type == public


@pytest.mark.parametrize("cap", ["WebSearch", "WebFetch", {"WebFetch": {"local": True}}])
def test_network_reaching_capabilities_fail(tmp_path, public, cap):
    with pytest.raises(ValueError, match=r"spec\.capabilities\[0\]\.Web"):
        _load(tmp_path, _profile(public, capabilities=[cap]))


def test_custom_capability_fails(tmp_path, public):
    from miragen.load import _CAPABILITY_REGISTRY, register_capability

    register_capability("CustomThing")(lambda cfg: None)
    try:
        doc = _profile(public, capabilities=[{"CustomThing": {}}])
        with pytest.raises(ValueError, match=r"capabilities\[0\]\.CustomThing: .*CustomThing capability"):
            _load(tmp_path, doc)
        # The same capability on an internal profile is fine.
        assert _load(tmp_path, _profile("internal", capabilities=[{"CustomThing": {}}]))
    finally:
        _CAPABILITY_REGISTRY.pop("CustomThing", None)


def test_local_tools_fail(tmp_path, public):
    doc = _profile(public)
    doc["tools"] = ["lookup_order"]
    with pytest.raises(ValueError, match=r"tools: .*local tools"):
        _load(tmp_path, doc)


@pytest.mark.parametrize("allowed", [None, "", " , ,"])
def test_unset_or_empty_allowlist_fails_closed(tmp_path, public, allowed):
    # None = read the environment, which the autouse fixture cleared.
    with pytest.raises(ValueError, match=f"set {ALLOWED_PROVIDERS_ENV}"):
        _load(tmp_path, _profile(public), allowed=allowed)


def test_allowlist_is_read_from_the_environment(tmp_path, public, monkeypatch):
    monkeypatch.setenv(ALLOWED_PROVIDERS_ENV, "anthropic")
    assert load_profile(_write(tmp_path, _profile(public))).agent_type == public
    monkeypatch.setenv(ALLOWED_PROVIDERS_ENV, "openai")
    with pytest.raises(ValueError, match="'anthropic' is not in"):
        load_profile(_write(tmp_path, _profile(public)))


def test_parse_allowed_providers():
    assert parse_allowed_providers(" Anthropic, openai ,,") == {"anthropic", "openai"}
    assert parse_allowed_providers(None) == frozenset()
    assert parse_allowed_providers(["OpenAI "]) == {"openai"}
