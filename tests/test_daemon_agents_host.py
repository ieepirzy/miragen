"""miragend on a host its control plane does not share (the agents host,
mirarun ADR-041 §8): configurable agent networks, an advertised endpoint,
the lifecycle audit log, and credential stores kept off the file API."""

import json
from datetime import UTC, datetime

import pytest
import yaml
from fastapi.testclient import TestClient

from miragen.daemon.api import create_app
from miragen.daemon.audit import AuditLog, NullAuditLog, audit_log_from_env
from miragen.daemon.core import (
    InvalidAgentName,
    InvalidPath,
    LifecycleCore,
    agent_networks,
    endpoint_template,
)
from miragen.daemon.spawn.base import SpawnOperationFailed
from tests.test_daemon_core import VALID_YAML, FakeDocker, FakeNotFound, RecordingRunner

LANES = "lanes-control,lanes-egress,lanes-broker"


def _core(tmp_path, environ, docker_client=None):
    docker_client = docker_client or FakeDocker()
    runner = RecordingRunner()
    runner.on_up = lambda name: docker_client.add(name)
    return LifecycleCore(
        tmp_path,
        docker_client,
        base_image="ghcr.io/example/miragen:test",
        internal_token="shared-secret",
        environ=environ,
        runner=runner,
        not_found=FakeNotFound,
        sleep=lambda _s: None,
    )


def _yaml(name="alpha", model="test:whatever"):
    return VALID_YAML.format(name=name).replace("test:whatever", model)


def _service(tmp_path, name):
    return yaml.safe_load((tmp_path / "compose.yml").read_text())["services"][name]


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


# ── networks ─────────────────────────────────────────────────────────────────


def test_unset_networks_keep_miragen_net(tmp_path):
    core = _core(tmp_path, {})
    core.create_agent("alpha", _yaml())
    assert _service(tmp_path, "alpha")["networks"] == ["miragen-net"]


def test_agents_join_the_configured_networks_in_order(tmp_path):
    docker_client = FakeDocker()
    docker_client.networks_created.extend(LANES.split(","))
    core = _core(tmp_path, {"MIRAGEND_AGENT_NETWORKS": LANES}, docker_client)
    core.create_agent("alpha", _yaml())

    compose = yaml.safe_load((tmp_path / "compose.yml").read_text())
    assert compose["services"]["alpha"]["networks"] == LANES.split(",")
    assert compose["networks"] == {n: {"external": True} for n in LANES.split(",")}
    # Never the single-host network: an engineer must not share it.
    assert "miragen-net" not in compose["networks"]
    assert "miragen-net" not in docker_client.networks_created


def test_a_missing_configured_network_fails_instead_of_being_created(tmp_path):
    # Its options (internal, no inter-container traffic) are the isolation;
    # a plain bridge created in its place would silently drop them.
    docker_client = FakeDocker()
    core = _core(tmp_path, {"MIRAGEND_AGENT_NETWORKS": "lanes-control"}, docker_client)
    with pytest.raises(SpawnOperationFailed, match="lanes-control"):
        core.ensure_network()
    assert docker_client.networks_created == []


def test_network_names_are_parsed_and_checked():
    assert agent_networks({}) == ("miragen-net",)
    assert agent_networks({"MIRAGEND_AGENT_NETWORKS": " a , b,a,, "}) == ("a", "b")
    with pytest.raises(ValueError):
        agent_networks({"MIRAGEND_AGENT_NETWORKS": "ok,bad name"})


# ── advertised endpoint ──────────────────────────────────────────────────────


def test_the_advertised_endpoint_follows_the_template(tmp_path):
    core = _core(tmp_path, {
        "MIRAGEND_AGENT_ENDPOINT_TEMPLATE": "http://10.8.0.15:8001/a/{name}/",
    })
    core.create_agent("lane-x", _yaml("lane-x"))
    assert core.get_agent("lane-x")["endpoint"] == "http://10.8.0.15:8001/a/lane-x"
    assert core.list_agents()[0]["endpoint"] == "http://10.8.0.15:8001/a/lane-x"
    # miragend's own calls (schedule re-triggers) keep the internal address.
    assert core.endpoint("lane-x") == "http://lane-x:8000"


def test_no_template_advertises_the_internal_address(tmp_path):
    core = _core(tmp_path, {})
    core.create_agent("alpha", _yaml())
    assert core.get_agent("alpha")["endpoint"] == "http://alpha:8000"


@pytest.mark.parametrize("bad", [
    "10.8.0.15:8001/a/{name}",          # no scheme
    "http://host/a/",                    # no {name}
    "http://host/{name}/{name}",         # twice
    "http://host/{name}/{other}",        # another field
    "ftp://host/{name}",
])
def test_bad_templates_are_refused(bad):
    with pytest.raises(ValueError):
        endpoint_template({"MIRAGEND_AGENT_ENDPOINT_TEMPLATE": bad})


def test_the_gateway_name_is_reserved(tmp_path):
    with pytest.raises(InvalidAgentName):
        _core(tmp_path, {}).check_name("lanes-gateway")


# ── audit ────────────────────────────────────────────────────────────────────


def test_audit_is_off_unless_configured(tmp_path):
    assert isinstance(audit_log_from_env({}), NullAuditLog)
    core = _core(tmp_path, {})
    core.create_agent("alpha", _yaml())
    assert not list(tmp_path.glob("**/audit*.jsonl"))


def test_credential_grants_are_recorded_by_name_never_by_value(tmp_path):
    log = tmp_path / "state" / "audit.jsonl"
    core = _core(tmp_path / "ws", {
        "MIRAGEND_AUDIT_LOG": str(log),
        "OPENAI_API_KEY": "sk-the-value",
        "TAVILY_API_KEY": "tvly-not-granted",
    })
    core.create_agent("alpha", _yaml(model="openai:gpt-4o"))

    text = log.read_text()
    assert "sk-the-value" not in text and "shared-secret" not in text
    [grant] = [r for r in _records(log) if r["action"] == "agent.credentials.granted"]
    assert grant["agent"] == "alpha"
    assert grant["keys"] == ["OPENAI_API_KEY"]
    assert grant["tokens"] == ["MIRAGEN_INTERNAL_TOKEN"]
    assert grant["secrets"] == []
    assert grant["outcome"] == "succeeded"


def test_lifecycle_actions_are_audited_with_their_outcome(tmp_path):
    log = tmp_path / "audit.jsonl"
    core = _core(tmp_path / "ws", {"MIRAGEND_AUDIT_LOG": str(log)})
    client = TestClient(create_app(core, token="daemon-token"))
    client.headers["Authorization"] = "Bearer daemon-token"

    assert client.post("/agents", json={"name": "alpha", "yaml_source": _yaml()}).status_code == 201
    assert client.post("/agents/alpha/stop").status_code == 200
    assert client.post("/agents/alpha/start").status_code == 200
    assert client.post("/agents/alpha/restart").status_code == 200
    # A refusal is recorded too, with its code.
    assert client.post("/agents/ghost/start").status_code == 404
    assert client.delete("/agents/alpha").status_code == 200

    actions = [(r["action"], r["agent"], r["outcome"]) for r in _records(log)
               if r["action"] != "agent.credentials.granted"]
    assert actions == [
        ("agent.create", "alpha", "succeeded"),
        ("agent.stop", "alpha", "succeeded"),
        ("agent.start", "alpha", "succeeded"),
        ("agent.restart", "alpha", "succeeded"),
        ("agent.start", "ghost", "refused"),
        ("agent.delete", "alpha", "succeeded"),
    ]
    refused = [r for r in _records(log) if r["outcome"] == "refused"][0]
    assert refused["code"]


def test_an_unauthenticated_call_reaches_neither_the_core_nor_the_log(tmp_path):
    log = tmp_path / "audit.jsonl"
    core = _core(tmp_path / "ws", {"MIRAGEND_AUDIT_LOG": str(log)})
    client = TestClient(create_app(core, token="daemon-token"))
    assert client.post("/agents", json={"name": "alpha", "yaml_source": _yaml()}).status_code == 401
    assert not log.exists()


def test_a_failed_audit_write_never_fails_the_action(tmp_path, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    audit = AuditLog(blocker / "audit.jsonl",
                     clock=lambda: datetime(2026, 10, 18, tzinfo=UTC))
    audit.record("agent.start", agent="alpha")
    assert "could not write" in caplog.text


# ── credential stores ────────────────────────────────────────────────────────


def test_credential_stores_are_neither_served_nor_exported(tmp_path):
    core = _core(tmp_path, {})
    core.create_agent("alpha", _yaml())
    store = tmp_path / "agents" / "alpha" / "claude-home" / ".credentials.json"
    store.parent.mkdir(parents=True)
    store.write_text('{"pluginSecrets": {"x": {"token": "secret-value"}}}')

    for call in (
        lambda: core.read_file("alpha", "claude-home/.credentials.json"),
        lambda: core.write_file("alpha", "claude-home/.credentials.json", "{}"),
        lambda: core.edit_file("alpha", "claude-home/.credentials.json", "x", "y"),
    ):
        with pytest.raises(InvalidPath):
            call()

    import tarfile

    result = core.export_agent("alpha")
    assert not any(m.endswith(".credentials.json") for m in result["included"])
    with tarfile.open(result["archive_path"]) as tar:
        members = tar.getnames()
    assert not any(m.endswith(".credentials.json") for m in members)
    assert any(m.endswith("agent.yaml") for m in members)


# ── the deployable stack and the example profile ─────────────────────────────

ROOT = __import__("pathlib").Path(__file__).parent.parent
STACK = ROOT / "deploy" / "agents-host" / "compose.yaml"
EXAMPLE = ROOT / "examples" / "profiles" / "lane-engineer-agents-host.yaml"


def _stack() -> dict:
    return yaml.safe_load(STACK.read_text())


def test_the_stack_publishes_only_on_the_wireguard_address():
    services = _stack()["services"]
    for name in ("miragend", "lanes-gateway"):
        for port in services[name]["ports"]:
            assert port.startswith("${AGENTS_HOST_BIND_ADDRESS"), (name, port)


def test_agents_never_share_a_network_with_miragend_or_each_other():
    stack = _stack()
    miragend = stack["services"]["miragend"]
    env = miragend["environment"]
    assert env["MIRAGEND_ISOLATED_AGENTS"] == "on"
    assert env["MIRAGEND_AGENT_NETWORK_PEERS"] == "lanes-gateway=github-broker"
    networks = env["MIRAGEND_AGENT_NETWORKS"].split(",")
    assert networks == ["lanes-egress"]
    # miragend is only on the default network, which no agent joins.
    assert set(miragend["networks"]) == {"default"}
    egress = stack["networks"]["lanes-egress"]
    assert egress["name"] == "lanes-egress"
    assert egress["driver_opts"]["com.docker.network.bridge.enable_icc"] == "false"
    assert egress["driver_opts"]["com.docker.network.bridge.name"].startswith("lna")
    assert set(stack["networks"]) == {"lanes-egress"}
    assert set(stack["services"]["lanes-gateway"]["networks"]) == {"default"}


def test_no_image_defaults_to_a_floating_tag():
    stack = _stack()
    assert ":?" in stack["services"]["miragend"]["image"]
    assert "@sha256:" in stack["services"]["lanes-gateway"]["image"]
    assert ":?" in stack["services"]["miragend"]["environment"]["MIRAGEN_BASE_IMAGE"]


def test_the_gateway_routes_on_the_raw_uri_and_lane_names_only():
    content = _stack()["configs"]["lanes-gateway-nginx"]["content"]
    assert "map $$request_uri $$lane_agent" in content
    assert "lane-[a-z0-9_-]{1,58}" in content
    assert "proxy_pass http://$$lane_agent:8000$$lane_rest;" in content
    assert "$$uri" not in content.replace("$$request_uri", "")


def test_the_stack_holds_no_secret_values():
    env = _stack()["services"]["miragend"]["environment"]
    for key, value in env.items():
        if any(word in key for word in ("TOKEN", "API_KEY", "SECRET")):
            assert key.endswith("_FILE") and str(value).startswith("/run/agents-host-secrets/"), key
    assert "MIRAGEND_AUDIT_LOG" in env
    assert "{name}" in env["MIRAGEND_AGENT_ENDPOINT_TEMPLATE"]
    assert endpoint_template({"MIRAGEND_AGENT_ENDPOINT_TEMPLATE": env[
        "MIRAGEND_AGENT_ENDPOINT_TEMPLATE"].replace(
            "${AGENTS_HOST_BIND_ADDRESS:?set AGENTS_HOST_BIND_ADDRESS (this host's WireGuard address)}",
            "10.0.0.1").replace("${GATEWAY_PUBLISH_PORT:-8001}", "8001")})


def test_the_example_engineer_profile_validates():
    from miragen.daemon.core import validate_profile_text

    summary = validate_profile_text(EXAMPLE.read_text(), environ={})
    assert summary["model"].startswith("claude-code:")
    assert summary["name"].startswith("lane-")
    profile = yaml.safe_load(EXAMPLE.read_text())
    assert profile["spec"]["harness"]["posture"] == "engineer"
    assert profile["spec"]["harness"]["git_broker"]["credential_file"].startswith("/run/engineer/")


# ── isolated agents (MIRAGEND_ISOLATED_AGENTS, the agents host) ──────────────

import hashlib as _hashlib  # noqa: E402

from miragen.daemon.core import derive_agent_token  # noqa: E402
from miragen.daemon.spawn.docker_compose import private_bridge  # noqa: E402

# Shared with mirarun's tests (tests/unit/test_lanes_miragend_routing.py):
# both sides must derive the same token, or every lane turn is a 401.
GOLDEN_MASTER = "golden-master-secret"
GOLDEN_NAME = "lane-example"
GOLDEN_DIGEST = "fbf6e59573900be8c15296dd15eebb47f241481fe4cc168baba816d54e50c69a"  # gitleaks:allow (a published test vector, not a credential)


class FakeNetwork:
    def __init__(self, name, **kw):
        self.name = name
        self.kw = kw
        self.members: dict[str, list[str]] = {}
        self.removed = False

    def connect(self, container, aliases=None):
        if container in self.members:
            raise RuntimeError(f"endpoint with name {container} already exists in network")
        self.members[container] = list(aliases or [])

    def disconnect(self, container, force=False):
        self.members.pop(container, None)

    def remove(self):
        self.removed = True


class IsolationDocker(FakeDocker):
    def __init__(self):
        super().__init__()
        self.networks_created.append("lanes-egress")
        self.net_objects: dict[str, FakeNetwork] = {}
        outer = self
        base = self.networks

        class _Networks:
            def get(self, name):
                if name in outer.net_objects and not outer.net_objects[name].removed:
                    return outer.net_objects[name]
                return base.get(name)

            def create(self, name, **kw):
                outer.networks_created.append(name)
                net = FakeNetwork(name, **kw)
                outer.net_objects[name] = net
                return net

            def list(self, filters=None):
                return [n for n in outer.net_objects.values() if not n.removed]

        self.networks = _Networks()


ISOLATED = {
    "MIRAGEND_ISOLATED_AGENTS": "on",
    "MIRAGEND_AGENT_NETWORKS": "lanes-egress",
    "MIRAGEND_AGENT_NETWORK_PEERS": "lanes-gateway=github-broker",
}


def _isolated_core(tmp_path, extra=None):
    docker_client = IsolationDocker()
    core = _core(tmp_path, {**ISOLATED, **(extra or {})}, docker_client)
    return core, docker_client


def test_the_derived_agent_token_matches_the_golden_vector():
    assert derive_agent_token(GOLDEN_MASTER, GOLDEN_NAME) == GOLDEN_DIGEST


def test_an_isolated_agent_holds_its_own_token_never_the_daemons(tmp_path):
    core, _ = _isolated_core(tmp_path)
    core.create_agent("lane-a", _yaml("lane-a"))
    core.create_agent("lane-b", _yaml("lane-b"))
    files = {n: tmp_path / "agent-secrets" / n / "miragen_internal_token"
             for n in ("lane-a", "lane-b")}
    a, b = (files[n].read_text() for n in ("lane-a", "lane-b"))
    assert a == derive_agent_token("shared-secret", "lane-a")
    assert b == derive_agent_token("shared-secret", "lane-b")
    assert len({a, b, "shared-secret"}) == 3


def test_isolated_credentials_are_files_not_environment_values(tmp_path):
    core, _ = _isolated_core(tmp_path, {"OPENAI_API_KEY": "sk-the-value",
                                         "CLAUDE_CODE_OAUTH_TOKEN": "oauth-value"})
    core.create_agent("lane-a", _yaml("lane-a", model="openai:gpt-4o"))
    compose_text = (tmp_path / "compose.yml").read_text()
    for value in ("sk-the-value", "shared-secret", derive_agent_token("shared-secret", "lane-a")):
        assert value not in compose_text
    service = _service(tmp_path, "lane-a")
    env = service["environment"]
    assert env["OPENAI_API_KEY_FILE"] == "/run/miragen-secrets/openai_api_key"
    assert env["MIRAGEN_INTERNAL_TOKEN_FILE"] == "/run/miragen-secrets/miragen_internal_token"
    assert "OPENAI_API_KEY" not in env and "MIRAGEN_INTERNAL_TOKEN" not in env
    secret = tmp_path / "agent-secrets" / "lane-a" / "openai_api_key"
    assert secret.read_text() == "sk-the-value"
    assert secret.stat().st_mode & 0o777 == 0o400
    assert "./agent-secrets/lane-a:/run/miragen-secrets:ro" in service["volumes"]
    # Never under the agent's own workspace (file API, export).
    assert not list((tmp_path / "agents" / "lane-a").rglob("openai_api_key"))


def test_a_revoked_grant_leaves_no_file_and_an_unchanged_update_no_recreate(tmp_path):
    core, _ = _isolated_core(tmp_path, {"OPENAI_API_KEY": "sk"})
    core.create_agent("lane-a", _yaml("lane-a", model="openai:gpt-4o"))
    spec = core._current_spec("lane-a")
    again = core._build_service_spec("lane-a", spec.image,
                                     profile_text=_yaml("lane-a", model="openai:gpt-4o"))
    assert core._delivered(spec) == core._delivered(again)
    core._define_service("lane-a", core._build_service_spec(
        "lane-a", spec.image, profile_text=_yaml("lane-a")))
    assert not (tmp_path / "agent-secrets" / "lane-a" / "openai_api_key").exists()


def test_each_isolated_agent_gets_a_private_network_shared_only_with_peers(tmp_path):
    core, docker_client = _isolated_core(tmp_path)
    core.create_agent("lane-a", _yaml("lane-a"))
    core.create_agent("lane-b", _yaml("lane-b"))
    a = _service(tmp_path, "lane-a")
    b = _service(tmp_path, "lane-b")
    assert a["networks"] == ["miragen-agent-lane-a", "lanes-egress"]
    assert b["networks"] == ["miragen-agent-lane-b", "lanes-egress"]
    net = docker_client.net_objects["miragen-agent-lane-a"]
    assert net.kw["internal"] is True
    assert net.kw["options"]["com.docker.network.bridge.name"] == private_bridge("lane-a")
    assert len(private_bridge("lane-a")) <= 15 and private_bridge("lane-a").startswith("lna")
    assert net.members == {"lanes-gateway": ["github-broker"]}  # never another agent


def test_a_recreated_gateway_is_reattached_on_agent_start(tmp_path):
    core, docker_client = _isolated_core(tmp_path)
    core.create_agent("lane-a", _yaml("lane-a"))
    net = docker_client.net_objects["miragen-agent-lane-a"]
    net.members.clear()  # the gateway container was recreated
    core.start_agent("lane-a")
    assert "lanes-gateway" in net.members
    net.members.clear()
    core.ensure_network()  # daemon start
    assert "lanes-gateway" in net.members


def test_deleting_an_isolated_agent_removes_its_network_and_secrets(tmp_path):
    core, docker_client = _isolated_core(tmp_path)
    core.create_agent("lane-a", _yaml("lane-a"))
    core.delete_agent("lane-a")
    assert docker_client.net_objects["miragen-agent-lane-a"].removed
    assert not (tmp_path / "agent-secrets" / "lane-a").exists()


def test_isolated_units_are_hardened(tmp_path):
    core, _ = _isolated_core(tmp_path)
    core.create_agent("lane-a", _yaml("lane-a"))
    service = _service(tmp_path, "lane-a")
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cap_drop"] == ["ALL"]
    assert service["pids_limit"] == 512
    assert service["read_only"] is True
    assert any(t.startswith("/run/engineer:uid=1000") for t in service["tmpfs"])


def test_isolation_needs_a_peer():
    from miragen.daemon.core import agent_isolation

    with pytest.raises(ValueError):
        agent_isolation({"MIRAGEND_ISOLATED_AGENTS": "on"})
    assert agent_isolation({}) is None


def test_an_isolated_daemon_serves_no_contract_and_no_schedules(tmp_path):
    from miragen.daemon.schedules import ScheduleStore
    from tests.test_daemon_api import FakeScheduler

    core, _ = _isolated_core(tmp_path / "ws")
    app = create_app(core, ScheduleStore(FakeScheduler()), token="daemon-token",
                     internal_token="shared-secret")
    client = TestClient(app)
    caps = client.get("/health").json()["capabilities"]
    assert "schedules/v1" not in caps and "executor-launch/v1" not in caps
    for method, path in (("post", "/executor-runs"), ("post", "/profiles/resolve"),
                         ("get", "/runs/x"), ("post", "/runs/x/resume")):
        response = client.request(method.upper(), path,
                                  headers={"X-Miragen-Token": "shared-secret"}, json={})
        assert response.status_code in (404, 405), (path, response.status_code)
    authed = {"Authorization": "Bearer daemon-token"}
    assert client.get("/schedules", headers=authed).status_code in (404, 405)


def test_a_normal_daemon_still_serves_the_contract(tmp_path):
    core = _core(tmp_path, {})
    client = TestClient(create_app(core, token="daemon-token", internal_token="shared-secret"))
    assert "executor-launch/v1" in client.get("/health").json()["capabilities"]
    assert client.post("/profiles/resolve", json={}).status_code == 401


def test_contract_calls_and_agent_reads_are_audited(tmp_path):
    log = tmp_path / "audit.jsonl"
    core = _core(tmp_path / "ws", {"MIRAGEND_AUDIT_LOG": str(log)})
    client = TestClient(create_app(core, token="daemon-token", internal_token="shared-secret"))
    client.headers["Authorization"] = "Bearer daemon-token"
    client.post("/agents", json={"name": "alpha", "yaml_source": _yaml()})
    client.post("/profiles/resolve", json={})
    client.get("/agents/alpha/files", params={"path": "agent.yaml"})
    client.get("/agents/alpha/logs")
    client.post("/agents/alpha/export")
    actions = [(r["action"], r["outcome"]) for r in _records(log)]
    assert ("contract.call", "refused") in actions
    assert ("agent.file.read", "succeeded") in actions
    assert ("agent.logs.read", "succeeded") in actions
    assert ("agent.export", "succeeded") in actions


def test_hard_links_are_neither_served_nor_exported(tmp_path):
    import os as _os

    core = _core(tmp_path, {})
    core.create_agent("alpha", _yaml())
    outside = tmp_path / "outside-secret"
    outside.write_text("host secret")
    _os.link(outside, tmp_path / "agents" / "alpha" / "innocent.txt")
    with pytest.raises(InvalidPath):
        core.read_file("alpha", "innocent.txt")
    result = core.export_agent("alpha")
    assert "innocent.txt" not in result["included"]
