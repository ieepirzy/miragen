"""Per-profile credential grants (miragen/credentials.py, docs/credential-grants.md).

An agent receives only the provider keys its profile implies or declares,
and the subscription token only when it runs Claude Code. The daemon tests
drive LifecycleCore end to end and read the compose service it wrote, so
the env forwarding and the Docker secret mount are each checked."""

from __future__ import annotations

import json
import os

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

MEMORY = (
    "memory:\n  backend: ephemeral\n"
    "  scopes: {{read: [s], propose: [s], default_write: s}}\n"
    "  extraction: {{model: '{model}'}}\n"
)

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


def test_codex_executor_gets_no_metered_key_without_a_grant(core, tmp_path):
    """Codex runs on the subscription auth.json; its API-key fallback would
    replace it, so it is an explicit grant, never implied."""
    core.create_agent("worker", EXECUTOR_YAML.format(name="worker", kind="codex"))
    service = _service(tmp_path, "worker")
    assert _key_env(service) == set()
    assert service["secrets"] == []
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in service["environment"]


def test_codex_executor_gets_its_metered_fallback_when_granted(core, tmp_path):
    core.create_agent("worker", EXECUTOR_YAML.format(name="worker", kind="codex")
                      + "credentials: [OPENAI_API_KEY]\n")
    service = _service(tmp_path, "worker")
    assert _key_env(service) == {"OPENAI_API_KEY_FILE"}
    assert service["secrets"] == ["openai_key"]


def test_codex_executor_openai_memory_model_needs_the_grant_too(core, tmp_path):
    core.create_agent("worker", EXECUTOR_YAML.format(name="worker", kind="codex") + MEMORY.format(
        model="openai:gpt-5-mini"))
    assert _key_env(_service(tmp_path, "worker")) == set()


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


def test_claude_code_memory_selector_gets_the_token(core, tmp_path):
    """The recall selector runs headless `claude -p` inside the agent."""
    core.create_agent("alpha", _model(
        "alpha", "anthropic:claude-sonnet-4-6",
        "memory:\n  backend: ephemeral\n  scopes: {read: [s], propose: [s], default_write: s}\n"
        "  recall: {model: 'claude-code:haiku'}\n"))
    service = _service(tmp_path, "alpha")
    assert service["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert _key_env(service) == {"ANTHROPIC_API_KEY_FILE"}


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


# ── review round: bare and gateway model names ──────────────────────────────


@pytest.mark.parametrize("model, keys", [
    ("gpt-4o", ("OPENAI_API_KEY",)),
    ("o3-mini", ("OPENAI_API_KEY",)),
    ("o1", ("OPENAI_API_KEY",)),
    ("claude-sonnet-4-6", ("ANTHROPIC_API_KEY",)),
    ("gemini-2.5-pro", ("GOOGLE_API_KEY", "GEMINI_API_KEY")),
    ("gateway/openai:gpt-5", ("PYDANTIC_AI_GATEWAY_API_KEY", "PAIG_API_KEY")),
    ("gateway/anthropic:claude-sonnet-4-6", ("PYDANTIC_AI_GATEWAY_API_KEY", "PAIG_API_KEY")),
    ("llama-3", ()),
])
def test_bare_and_gateway_model_names(model, keys):
    from miragen.credentials import provider_credentials

    assert provider_credentials(model) == keys


def test_every_legacy_prefix_pydantic_ai_accepts_maps_to_its_providers_key():
    """Drift guard against the installed pydantic-ai: each bare-name prefix
    it still resolves maps to the key of the provider it resolves to."""
    from pydantic_ai.models import _LEGACY_MODEL_PREFIXES

    from miragen.credentials import PROVIDER_CREDENTIAL_ENV, provider_credentials

    assert _LEGACY_MODEL_PREFIXES  # the table this guard is about still exists
    for prefix, provider in _LEGACY_MODEL_PREFIXES.items():
        assert PROVIDER_CREDENTIAL_ENV.get(provider), provider
        assert provider_credentials(f"{prefix}-x") == PROVIDER_CREDENTIAL_ENV[provider]


def test_a_bare_model_name_gets_its_key_at_spawn(core, tmp_path):
    """`model: gpt-4o` runs (pydantic-ai resolves it to openai): withholding
    the key would crash-loop it at boot."""
    core.create_agent("alpha", _model("alpha", "gpt-4o"))
    service = _service(tmp_path, "alpha")
    assert _key_env(service) == {"OPENAI_API_KEY_FILE"}
    assert service["secrets"] == ["openai_key"]


def test_a_gateway_model_gets_the_gateway_key_at_spawn(tmp_path):
    core = _executor_kind_core(tmp_path, {**DAEMON_ENV, "PYDANTIC_AI_GATEWAY_API_KEY": "paig"})
    core.create_agent("alpha", _model("alpha", "gateway/openai:gpt-5"))
    assert _key_env(_service(tmp_path, "alpha")) == {"PYDANTIC_AI_GATEWAY_API_KEY"}


# ── review round: claude-code never gets ANTHROPIC_* implicitly ─────────────


def _claude_code_executor_yaml(extra: str = "") -> str:
    return EXECUTOR_YAML.format(name="worker", kind="claude-code") + extra


@pytest.mark.parametrize("route", [
    MEMORY.format(model="anthropic:claude-haiku-4-5"),
    "on_complete:\n  post_to: 'https://example.com/${ANTHROPIC_API_KEY:-x}'\n",
])
def test_claude_code_executor_gets_no_anthropic_key_through_an_implied_route(
    core, tmp_path, route
):
    core.create_agent("worker", _claude_code_executor_yaml(route))
    service = _service(tmp_path, "worker")
    assert _key_env(service) == set()
    assert service["secrets"] == []
    assert service["environment"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN


def test_claude_code_executor_bearer_token_env_does_not_imply_the_anthropic_key():
    from miragen.credentials import credential_plan

    prof = AgentProfile.model_validate({
        "name": "w", "mode": "interactive", "triggers": [{"type": "http"}],
        "executor": {"executor": "claude-code", "instructions": "x", "mcp_servers": [
            {"name": "m", "url": "https://mcp.example.com/mcp",
             "bearer_token_env": "ANTHROPIC_API_KEY"}]}})
    assert credential_plan(prof).api_keys == frozenset()


def test_claude_code_executor_with_an_explicit_grant_gets_the_anthropic_key(core, tmp_path):
    core.create_agent("worker", _claude_code_executor_yaml(
        MEMORY.format(model="anthropic:claude-haiku-4-5")
        + "credentials: [ANTHROPIC_API_KEY]\n"))
    service = _service(tmp_path, "worker")
    assert _key_env(service) == {"ANTHROPIC_API_KEY_FILE"}
    assert service["secrets"] == ["anthropic_key"]


def _executor_profile(kind: str, credentials: list[str] | None = None) -> AgentProfile:
    body = {"name": "w", "mode": "interactive", "triggers": [{"type": "http"}],
            "executor": {"executor": kind, "instructions": "x"}}
    if credentials is not None:
        body["credentials"] = credentials
    return AgentProfile.model_validate(body)


@pytest.mark.parametrize("granted", [False, True])
def test_claude_code_executor_child_env_drops_an_ungranted_metered_key(
    tmp_path, monkeypatch, granted
):
    """The Claude Code child inherits this process's environment (the SDK
    can override a variable but not unset one), so prepare() removes the
    metered key unless the profile grants it — whatever delivered it
    (passthrough, a hand-run container). ANTHROPIC_AUTH_TOKEN is never
    grantable and always goes."""
    from miragen.executor.claude_code import ClaudeCodeExecutor

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-metered")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-bearer")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    profile = _executor_profile("claude-code", ["ANTHROPIC_API_KEY"] if granted else None)
    ClaudeCodeExecutor(profile, runs_root=tmp_path / "runs").prepare()
    assert ("ANTHROPIC_API_KEY" in os.environ) is granted
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN


# ── review round: codex logs in with an API key only when granted ───────────


class _FakeCodex:
    def __init__(self):
        self.logins: list[str] = []

    async def login_api_key(self, key):
        self.logins.append(key)


@pytest.mark.parametrize("granted, expected", [
    (None, []),
    (["OPENAI_API_KEY"], ["sk-openai"]),
    (["CODEX_API_KEY", "OPENAI_API_KEY"], ["sk-codex"]),
])
async def test_codex_logs_in_with_an_api_key_only_when_granted(
    tmp_path, monkeypatch, granted, expected
):
    """A key that is merely present — OPENAI_API_KEY forwarded for an
    openai: memory model, say — never replaces the subscription auth.json."""
    from miragen.executor import CodexExecutor

    monkeypatch.setenv("CODEX_API_KEY", "sk-codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    executor = CodexExecutor(_executor_profile("codex", granted),
                             runs_root=tmp_path / "runs", session_factory=None)
    codex = _FakeCodex()
    await executor._login(codex)
    assert codex.logins == expected


@pytest.mark.parametrize("granted", [False, True])
def test_codex_prepare_withholds_ungranted_keys_from_the_app_server(
    tmp_path, monkeypatch, granted
):
    from miragen.executor import CodexExecutor

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "inherited"))
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    profile = _executor_profile("codex", ["OPENAI_API_KEY"] if granted else None)
    profile.executor.codex_home = str(tmp_path / "codex-home")
    CodexExecutor(profile, runs_root=tmp_path / "runs", session_factory=None).prepare()
    assert "CODEX_API_KEY" not in os.environ
    assert ("OPENAI_API_KEY" in os.environ) is granted


# ── review round: updates compare against the unit as it runs ──────────────


def _ups(core) -> list[list[str]]:
    return [c for c in core._spawn_driver._run.calls if c[:3] == ["docker", "compose", "up"]]


def test_an_unchanged_resubmit_recreates_when_the_running_unit_holds_more(core, tmp_path):
    """After an upgrade narrows what the daemon forwards, re-submitting the
    SAME profile must recreate: the running container still holds the old,
    wider environment. Simulated by widening its stored definition the way
    the pre-grants daemon wrote it."""
    text = _model("alpha", "anthropic:claude-sonnet-4-6")
    core.create_agent("alpha", text)
    compose = pyyaml.safe_load((tmp_path / "compose.yml").read_text())
    compose["services"]["alpha"]["environment"]["TAVILY_API_KEY_FILE"] = "/run/secrets/tavily_key"
    compose["services"]["alpha"]["secrets"].append("tavily_key")
    (tmp_path / "compose.yml").write_text(pyyaml.safe_dump(compose))
    container = core._docker.container_map["alpha"]
    ups = len(_ups(core))

    core.update_agent_config("alpha", text)
    service = _service(tmp_path, "alpha")
    assert "TAVILY_API_KEY_FILE" not in service["environment"]
    assert service["secrets"] == ["anthropic_key"]
    assert len(_ups(core)) == ups + 1  # recreated…
    assert container.restarted == 0  # …not merely restarted
    assert service["image"] == DEFAULT_DIGEST


def test_a_true_no_op_update_only_restarts(core, tmp_path):
    text = _model("alpha", "anthropic:claude-sonnet-4-6")
    core.create_agent("alpha", text)
    ups = len(_ups(core))
    core.update_agent_config("alpha", text)
    assert len(_ups(core)) == ups
    assert core._docker.container_map["alpha"].restarted == 1


def test_a_driver_that_cannot_report_its_definition_recreates(core, tmp_path, monkeypatch):
    text = _model("alpha", "anthropic:claude-sonnet-4-6")
    core.create_agent("alpha", text)
    monkeypatch.setattr(core._spawn_driver, "current_spec", lambda name: None)
    ups = len(_ups(core))
    core.update_agent_config("alpha", text)
    assert len(_ups(core)) == ups + 1


def _fail_next_boot(core):
    docker = core._docker

    def on_up(name):
        docker.add(name).status = "restarting"
    core._spawn_driver._run.on_up = on_up


def test_rollback_restores_the_last_good_profile_and_unit_not_a_tampered_file(core, tmp_path):
    """agents/<name>/ is the agent's rw /agent mount. An agent that rewrote
    its own agent.yaml (granting itself a key, switching its executor) must
    not get that profile — or its credentials — back from a failed update's
    rollback."""
    from miragen.daemon.core import RestartFailed

    good = _model("alpha", "anthropic:claude-sonnet-4-6")
    core.create_agent("alpha", good)
    before = _service(tmp_path, "alpha")
    yaml_path = tmp_path / "agents" / "alpha" / "agent.yaml"
    yaml_path.write_text(_model("alpha", "claude-code:claude-opus-4-8",
                                "credentials: [TAVILY_API_KEY, OPENAI_API_KEY]\n"))

    _fail_next_boot(core)
    with pytest.raises(RestartFailed):
        core.update_agent_config("alpha", _model("alpha", "openai:gpt-5"))

    assert yaml_path.read_text() == good
    after = _service(tmp_path, "alpha")
    assert after == before  # the previous unit, verbatim
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in after["environment"]
    assert "tavily_key" not in after["secrets"]


def test_a_tampered_executor_kind_does_not_drive_the_update(core, tmp_path):
    """The previous kind comes from the daemon's copy: a profile rewritten
    in place to claim another executor must not flip the update onto the
    base-image recreate path (which would drop the digest pin)."""
    good = _model("alpha", "anthropic:claude-sonnet-4-6")
    core.create_agent("alpha", good)
    (tmp_path / "agents" / "alpha" / "agent.yaml").write_text(
        EXECUTOR_YAML.format(name="alpha", kind="codex"))
    core.update_agent_config("alpha", good)
    # Unchanged unit, same kind as last applied: a plain restart.
    assert core._docker.container_map["alpha"].restarted == 1


def test_rollback_without_a_last_good_copy_keeps_the_previous_unit(core, tmp_path):
    """An agent applied before the daemon kept its copy: the file as found
    is written back, but the unit is still the previous definition, so its
    credentials are never recomputed from that file."""
    from miragen.daemon.core import RestartFailed

    core.create_agent("alpha", _model("alpha", "anthropic:claude-sonnet-4-6"))
    core._last_good_path("alpha").unlink()
    before = _service(tmp_path, "alpha")
    tampered = _model("alpha", "anthropic:claude-sonnet-4-6", "credentials: [TAVILY_API_KEY]\n")
    (tmp_path / "agents" / "alpha" / "agent.yaml").write_text(tampered)

    _fail_next_boot(core)
    with pytest.raises(RestartFailed):
        core.update_agent_config("alpha", _model("alpha", "openai:gpt-5"))
    assert _service(tmp_path, "alpha") == before


def test_last_good_copy_follows_create_update_and_delete(core, tmp_path):
    first = _model("alpha", "anthropic:claude-sonnet-4-6")
    core.create_agent("alpha", first)
    path = core._last_good_path("alpha")
    assert path.read_text() == first
    # Outside the agent's own mount.
    assert (tmp_path / "agents" / "alpha") not in path.parents
    second = _model("alpha", "openai:gpt-5")
    core.update_agent_config("alpha", second)
    assert path.read_text() == second
    core.delete_agent("alpha")
    assert not path.exists()


def test_import_plans_from_the_imported_profile_and_keeps_a_copy(core, tmp_path):
    core.create_agent("alpha", _model("alpha", "openai:gpt-5"))
    archive = core.export_agent("alpha")["archive_path"]
    core.import_agent("beta", archive)
    service = _service(tmp_path, "beta")
    assert _key_env(service) == {"OPENAI_API_KEY_FILE"}
    assert service["secrets"] == ["openai_key"]
    assert "name: beta" in core._last_good_path("beta").read_text()


# ── review round: interpolation as the container resolves it ────────────────


@pytest.mark.parametrize("forwarded", [False, True])
def test_interpolated_model_resolves_as_the_container_will(tmp_path, monkeypatch, forwarded):
    """LANE_MODEL is set in the daemon. Not forwarded, the container takes
    the default (anthropic) — so must the plan; forwarded by passthrough,
    the container sees openai — so must the plan. os.environ holds a decoy
    to prove the injected environment is what is read."""
    monkeypatch.setenv("LANE_MODEL", "groq:llama-3.3-70b")
    environ = {**DAEMON_ENV, "LANE_MODEL": "openai:gpt-5"}
    if forwarded:
        environ["MIRAGEND_AGENT_ENV_PASSTHROUGH"] = "LANE_MODEL"
    core = _executor_kind_core(tmp_path, environ)
    core.create_agent("alpha", _model("alpha", "${LANE_MODEL:-anthropic:claude-sonnet-4-6}"))
    expected = {"OPENAI_API_KEY_FILE"} if forwarded else {"ANTHROPIC_API_KEY_FILE"}
    assert _key_env(_service(tmp_path, "alpha")) == expected


def test_interpolate_env_reads_the_mapping_it_is_given(monkeypatch):
    from miragen.load import interpolate_env

    monkeypatch.setenv("X", "process")
    assert interpolate_env({"a": ["${X}"]}, environ={"X": "given"}) == {"a": ["given"]}
    assert interpolate_env("${X:-dflt}", environ={}) == "dflt"
    assert interpolate_env("${X}") == "process"


# ── review round: passthrough broadcast warning ─────────────────────────────


@pytest.mark.parametrize("listed, warned", [
    ("CLAUDE_CODE_OAUTH_TOKEN", True),
    ("FOO, OPENAI_API_KEY", True),
    ("OPENAI_API_KEY_FILE", True),
    ("LANE_MODEL,MIRAGEN_SOMETHING", False),
])
def test_passthrough_of_a_credential_warns_at_startup(tmp_path, caplog, listed, warned):
    import logging

    with caplog.at_level(logging.WARNING, logger="miragen.daemon"):
        _executor_kind_core(tmp_path, {"MIRAGEND_AGENT_ENV_PASSTHROUGH": listed})
    hits = [r for r in caplog.records if "MIRAGEND_AGENT_ENV_PASSTHROUGH" in r.getMessage()]
    assert bool(hits) is warned


def test_spawn_executor_gets_no_key_unless_granted():
    from miragen.credentials import credential_plan

    body = {"name": "s", "mode": "interactive", "triggers": [{"type": "http"}],
            "executor": {"executor": "spawn", "instructions": "x", "command": ["true"]}}
    assert credential_plan(AgentProfile.model_validate(body)).api_keys == frozenset()
    granted = AgentProfile.model_validate({**body, "credentials": ["TAVILY_API_KEY"]})
    assert credential_plan(granted).api_keys == {"TAVILY_API_KEY"}


def test_a_failed_last_good_save_does_not_fail_a_booted_create_or_update(
    core, tmp_path, monkeypatch, caplog
):
    from pathlib import Path

    real = Path.write_text

    def flaky(self, *a, **kw):
        if ".miragend" in self.parts:
            raise OSError("No space left on device")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", flaky)
    core.create_agent("alpha", _model("alpha", "anthropic:claude-sonnet-4-6"))
    core.update_agent_config("alpha", _model("alpha", "openai:gpt-5"))
    assert core._docker.container_map["alpha"].status == "running"
    assert not core._last_good_path("alpha").exists()
    assert "last-known-good" in caplog.text
