"""Per-profile credential grants (miragen/credentials.py, docs/credential-grants.md).

An agent receives only the provider keys its profile implies or declares,
and the subscription token only when it runs Claude Code. The daemon tests
drive LifecycleCore end to end and read the compose service it wrote, so
the env forwarding and the Docker secret mount are each checked."""

from __future__ import annotations

import json

import pytest
import yaml as pyyaml
from pydantic import ValidationError

from miragen.models import AgentProfile
from miragen.profile_contract import SUPPORTED_PROFILE_CONTRACTS
from tests.test_daemon_core import DEFAULT_DIGEST, DEFAULT_IMAGE, FakeImage, _executor_kind_core

TOKEN = "sk-ant-oat-example"

# Everything a busy daemon might hold: two providers as secret files, one
# as a plain value, a tool key, and the subscription token.
DAEMON_ENV = {
    "ANTHROPIC_API_KEY_FILE": "/run/secrets/anthropic_key",
    "OPENAI_API_KEY_FILE": "/run/secrets/openai_key",
    "GROQ_API_KEY": "gsk-plain",
    "TAVILY_API_KEY_FILE": "/run/secrets/tavily_key",
    "CLAUDE_CODE_OAUTH_TOKEN": TOKEN,
}

MODEL_YAML = """\
name: {name}
mode: interactive
triggers:
  - type: http
spec:
  model: "{model}"
  instructions: "be helpful"
"""

EXECUTOR_YAML = """\
name: {name}
mode: interactive
triggers:
  - type: http
executor:
  executor: {kind}
  instructions: "operate on the workspace"
"""


def _model(name: str, model: str, extra: str = "") -> str:
    return MODEL_YAML.format(name=name, model=model) + extra


def _service(tmp_path, name):
    return pyyaml.safe_load((tmp_path / "compose.yml").read_text())["services"][name]


def _key_env(service) -> set[str]:
    return {k for k in service["environment"] if "_API_KEY" in k}


@pytest.fixture
def core(tmp_path):
    core = _executor_kind_core(tmp_path, dict(DAEMON_ENV))
    # A current runtime: `credentials:` needs contract 6. The digest is what
    # an in-place update is checked against.
    for ref in (DEFAULT_IMAGE, DEFAULT_DIGEST):
        core._docker.image_map[ref] = FakeImage(
            labels={"io.miragen.profile-contracts": "1 2 3 4 5 6"},
            repo_digests=[DEFAULT_DIGEST])
    return core


# ── no grant, no key ─────────────────────────────────────────────────────────


def test_agent_without_a_grant_sees_no_other_providers_key(core, tmp_path):
    core.create_agent("alpha", _model("alpha", "anthropic:claude-sonnet-4-6"))
    service = _service(tmp_path, "alpha")
    # Its own provider, as the secret file the daemon holds…
    assert service["environment"]["ANTHROPIC_API_KEY_FILE"] == "/run/secrets/anthropic_key"
    assert service["secrets"] == ["anthropic_key"]
    # …and nothing else: neither the env nor the mount of another provider.
    assert _key_env(service) == {"ANTHROPIC_API_KEY_FILE"}
    assert "openai_key" not in service["secrets"]
    assert "tavily_key" not in service["secrets"]


def test_a_model_with_no_known_provider_gets_no_key_at_all(core, tmp_path):
    core.create_agent("alpha", _model("alpha", "test:whatever"))
    service = _service(tmp_path, "alpha")
    assert _key_env(service) == set()
    assert service["secrets"] == []


def test_plain_value_keys_are_scoped_too(core, tmp_path):
    core.create_agent("alpha", _model("alpha", "groq:llama-3.3-70b"))
    service = _service(tmp_path, "alpha")
    assert service["environment"]["GROQ_API_KEY"] == "gsk-plain"
    assert _key_env(service) == {"GROQ_API_KEY"}
    assert service["secrets"] == []


# ── explicit and implied grants ─────────────────────────────────────────────


def test_explicit_grant_delivers_env_and_mount(core, tmp_path):
    core.create_agent("alpha", _model(
        "alpha", "anthropic:claude-sonnet-4-6",
        "credentials: [TAVILY_API_KEY, NOT_HELD_API_KEY]\n"))
    service = _service(tmp_path, "alpha")
    assert service["environment"]["TAVILY_API_KEY_FILE"] == "/run/secrets/tavily_key"
    assert sorted(service["secrets"]) == ["anthropic_key", "tavily_key"]
    # A grant for a key the daemon does not hold forwards nothing.
    assert _key_env(service) == {"ANTHROPIC_API_KEY_FILE", "TAVILY_API_KEY_FILE"}


def test_env_reference_in_the_profile_implies_the_key(core, tmp_path):
    """The loader inside the container fails on an undefined ${VAR}: a
    referenced key that were withheld would crash-loop the agent."""
    core.create_agent("alpha", _model("alpha", "test:whatever").replace(
        'instructions: "be helpful"', 'instructions: "k=${GROQ_API_KEY:-none}"'))
    assert _key_env(_service(tmp_path, "alpha")) == {"GROQ_API_KEY"}


def test_openai_voice_implies_its_key(core, tmp_path):
    core.create_agent("alpha", _model(
        "alpha", "anthropic:claude-sonnet-4-6", "voice:\n  provider: openai\n"))
    service = _service(tmp_path, "alpha")
    assert _key_env(service) == {"ANTHROPIC_API_KEY_FILE", "OPENAI_API_KEY_FILE"}
    assert sorted(service["secrets"]) == ["anthropic_key", "openai_key"]


def test_codex_executor_gets_its_metered_fallback_only(core, tmp_path):
    core.create_agent("worker", EXECUTOR_YAML.format(name="worker", kind="codex"))
    service = _service(tmp_path, "worker")
    assert _key_env(service) == {"OPENAI_API_KEY_FILE"}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in service["environment"]


# ── the subscription token ───────────────────────────────────────────────────


def test_claude_code_model_profile_gets_the_token(core, tmp_path):
    core.create_agent("lane", _model("lane", "claude-code:claude-opus-4-8"))
    service = _service(tmp_path, "lane")
    assert service["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    # The harness runs on the subscription: no metered key that would
    # outrank the token, and no other provider's key.
    assert _key_env(service) == set()
    assert service["secrets"] == []


def test_claude_code_executor_gets_the_token_but_no_metered_key(core, tmp_path):
    core.create_agent("worker", EXECUTOR_YAML.format(name="worker", kind="claude-code"))
    service = _service(tmp_path, "worker")
    assert service["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert _key_env(service) == set()


def test_interpolated_claude_code_model_gets_the_token(core, tmp_path, monkeypatch):
    monkeypatch.delenv("LANE_MODEL", raising=False)
    core.create_agent("lane", _model("lane", "${LANE_MODEL:-claude-code:claude-opus-4-8}"))
    assert _service(tmp_path, "lane")["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN


@pytest.mark.parametrize("model", ["anthropic:claude-sonnet-4-6", "openai:gpt-5",
                                   "codex:gpt-5", "test:whatever"])
def test_non_claude_code_profile_does_not_get_the_token(core, tmp_path, model):
    core.create_agent("alpha", _model("alpha", model))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in _service(tmp_path, "alpha")["environment"]


@pytest.mark.parametrize("kind", ["codex", "kimi-code", "grok-build"])
def test_non_claude_code_executor_does_not_get_the_token(core, tmp_path, kind):
    core.create_agent("worker", EXECUTOR_YAML.format(name="worker", kind=kind))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in _service(tmp_path, "worker")["environment"]


def test_a_reference_cannot_reach_beyond_the_api_key_family(tmp_path, monkeypatch):
    """${VAR} references imply grants only inside *_API_KEY: naming the
    subscription token or the daemon's internal token gets nothing."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    core = _executor_kind_core(tmp_path, {**DAEMON_ENV, "OTHER_SECRET": "s3cret"})
    core.create_agent("alpha", _model("alpha", "test:whatever").replace(
        'instructions: "be helpful"',
        'instructions: "${CLAUDE_CODE_OAUTH_TOKEN:-a} ${OTHER_SECRET:-b}"'))
    env = _service(tmp_path, "alpha")["environment"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "OTHER_SECRET" not in env


# ── updates recreate when the plan changes ──────────────────────────────────


def test_update_to_a_claude_code_model_delivers_the_token_and_back_revokes_it(core, tmp_path):
    """Both are model tier (no executor kind), so only the credential plan
    can tell the daemon the container must be recreated."""
    core.create_agent("lane", _model("lane", "anthropic:claude-sonnet-4-6"))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in _service(tmp_path, "lane")["environment"]

    core.update_agent_config("lane", _model("lane", "claude-code:claude-opus-4-8"))
    service = _service(tmp_path, "lane")
    assert service["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert _key_env(service) == set() and service["secrets"] == []
    # Recreated in place: the validated digest pin survives.
    assert service["image"] == DEFAULT_DIGEST

    core.update_agent_config("lane", _model("lane", "anthropic:claude-sonnet-4-6"))
    service = _service(tmp_path, "lane")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in service["environment"]
    assert service["secrets"] == ["anthropic_key"]


def test_update_dropping_a_grant_revokes_the_key(core, tmp_path):
    granted = _model("alpha", "anthropic:claude-sonnet-4-6",
                     "credentials: [TAVILY_API_KEY]\n")
    core.create_agent("alpha", granted)
    assert "tavily_key" in _service(tmp_path, "alpha")["secrets"]

    core.update_agent_config("alpha", _model("alpha", "anthropic:claude-sonnet-4-6"))
    service = _service(tmp_path, "alpha")
    assert "tavily_key" not in service["secrets"]
    assert "TAVILY_API_KEY_FILE" not in service["environment"]


def test_unloadable_profile_on_disk_gets_no_credentials(core, tmp_path):
    core.create_agent("alpha", _model("alpha", "anthropic:claude-sonnet-4-6"))
    (tmp_path / "agents" / "alpha" / "agent.yaml").write_text("not: [a, profile")
    spec = core._build_service_spec("alpha")
    assert spec.secret_names == ()
    assert not {k for k in spec.environment if "_API_KEY" in k or k.startswith("CLAUDE_")}


# ── the profile field ───────────────────────────────────────────────────────

BASE = {"name": "a", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-sonnet-4-6", "instructions": "x"}}


@pytest.mark.parametrize("name", ["MIRAGEN_INTERNAL_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                                  "openai_api_key", "OPENAI_API_KEY_FILE", "_API_KEY", ""])
def test_only_api_key_names_are_grantable(name):
    with pytest.raises(ValidationError):
        AgentProfile.model_validate({**BASE, "credentials": [name]})


def test_declaring_credentials_implies_contract_6():
    assert 6 in SUPPORTED_PROFILE_CONTRACTS
    for grants in ([], ["TAVILY_API_KEY"]):
        assert AgentProfile.model_validate({**BASE, "credentials": grants}).required_contract() == 6
    with pytest.raises(ValidationError, match="v6"):
        AgentProfile.model_validate({**BASE, "credentials": [], "apiVersion": "miragen/v5"})


def test_an_empty_grant_list_dumps_back_to_the_original_contract():
    prof = AgentProfile.model_validate({**BASE, "credentials": []})
    for dumped in (prof.model_dump(by_alias=True), json.loads(prof.model_dump_json(by_alias=True))):
        assert "credentials" not in dumped
        assert AgentProfile.model_validate(dumped).required_contract() == 1
    plain = AgentProfile.model_validate(BASE)
    assert "credentials" not in plain.model_dump(by_alias=True)
    assert plain.required_contract() == 1


def test_a_grant_survives_the_dump():
    prof = AgentProfile.model_validate({**BASE, "credentials": ["TAVILY_API_KEY"]})
    dumped = prof.model_dump(mode="json", by_alias=True)
    assert dumped["credentials"] == ["TAVILY_API_KEY"]
    assert AgentProfile.model_validate(dumped).required_contract() == 6


# ── the plan, unit level ─────────────────────────────────────────────────────


@pytest.mark.parametrize("model, keys", [
    ("openai:gpt-5", ("OPENAI_API_KEY",)),
    ("openai-responses:gpt-5", ("OPENAI_API_KEY",)),
    ("anthropic:claude-sonnet-4-6", ("ANTHROPIC_API_KEY",)),
    ("google-gla:gemini-2.5-pro", ("GEMINI_API_KEY",)),
    ("claude-code:claude-opus-4-8", ()),
    ("codex:gpt-5", ()),
    ("grok-build:grok-4", ()),
    ("bedrock:anthropic.claude", ()),
    ("no-provider", ()),
])
def test_provider_credentials(model, keys):
    from miragen.credentials import provider_credentials

    assert provider_credentials(model) == keys


def test_memory_and_fallback_models_imply_their_keys():
    from miragen.credentials import credential_plan

    prof = AgentProfile.model_validate({
        **BASE,
        "spec": {**BASE["spec"], "capabilities": [
            {"ImageGeneration": {"fallback_model": "openai-responses:gpt-5"}}]},
        "memory": {"backend": "ephemeral", "scopes": {"read": ["s"], "propose": ["s"], "default_write": "s"},
                   "recall": {"model": "groq:llama-3.3-70b"}},
    })
    assert credential_plan(prof).api_keys == {
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"}


def test_grok_subscription_auth_gets_no_metered_key():
    from miragen.credentials import credential_plan

    prof = AgentProfile.model_validate({
        "name": "g", "mode": "interactive", "triggers": [{"type": "http"}],
        "executor": {"executor": "grok-build", "instructions": "x",
                     "grok_auth": "subscription"}})
    assert credential_plan(prof).api_keys == frozenset()
