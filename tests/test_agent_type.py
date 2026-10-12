"""agent_type: internal | external | hybrid — the load-time rules for
public-facing profiles (miragen/agent_type.py).

Every case goes through `load_profile`, the path `miragen run`, `miragen
validate` and miragend's `/validate` share; the miragend side is pinned in
tests/test_daemon_core.py and tests/test_daemon_api.py.
"""

import pytest
import yaml

from miragen.agent_type import ALLOWED_PROVIDERS_ENV, parse_allowed_providers
from miragen.load import load_profile


@pytest.fixture(autouse=True)
def _no_ambient_allowlist(monkeypatch):
    # The developer's or CI's own environment must not decide a test.
    monkeypatch.delenv(ALLOWED_PROVIDERS_ENV, raising=False)


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


def _load(tmp_path, doc, allowed="anthropic,openai"):
    return load_profile(_write(tmp_path, doc), allowed_providers=allowed)


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


# ── external / hybrid ────────────────────────────────────────────────────────


@pytest.fixture(params=["external", "hybrid"])
def public(request):
    return request.param


def test_allowed_provider_passes(tmp_path, public):
    assert _load(tmp_path, _profile(public)).agent_type == public


def test_provider_match_is_case_and_space_insensitive(tmp_path, public):
    doc = _profile(public, model="Anthropic:claude-sonnet-4-6")
    assert _load(tmp_path, doc, allowed=" OpenAI , anthropic ,").agent_type == public


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
