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
