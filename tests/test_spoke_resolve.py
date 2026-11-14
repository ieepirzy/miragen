"""EDF resolution inside the cluster (MiraRun ADR-050 Decision 4), against
the strict fake hub: a `resolve` is checked against the namespace's own
`edf` allowlist and only then resolved with miragen's resolver; it needs no
running agent. A launch carrying the EDF and its hash is re-resolved and
refused if either changed. The heartbeat advertises the capability."""

from __future__ import annotations

from uuid import UUID

import pytest

from miragen.daemon.spoke.config import SpokeConfigInvalid
from miragen.edf import resolve_edf
from tests.spoke_fakes import offered_delivery, offered_intent
from tests.test_kubernetes_workloads import NS
from tests.test_spoke_pull_mode import TENANT, OTHER, World, _config

EDF = {
    "apiVersion": "mirarun.io/v1alpha1",
    "kind": "Environment",
    "metadata": {"name": "analyst"},
    "spec": {
        "executor": {"kind": "codex"},
        "workspace": {"repositories": []},
        "tools": {
            "preset": "mirarun-default",
            "mcpServers": [{"name": "docs", "transport": "streamable-http",
                            "url": "https://tools.example/mcp/docs"}],
        },
    },
}
SOURCE = "f" * 64
ALLOW = {"presets": ["mirarun-default"], "tool_servers": ["https://tools.example/mcp/"]}


def _world(edf_section=ALLOW) -> World:
    namespace = {"classes": ["ephemeral", "persistent"], "organizations": [str(TENANT)]}
    if edf_section is not None:
        namespace["edf"] = edf_section
    return World(namespaces={NS: namespace})


def _resolve_delivery(world: World, *, namespace=NS, edf=EDF, organization=TENANT) -> dict:
    intent = offered_intent(organization, namespace)  # not offered: no workload runs
    delivery = offered_delivery(intent, kind="resolve", payload={
        "revision_id": "11111111-1111-4111-8111-111111111111", "edf": edf,
        "source_sha256": SOURCE, "namespace": namespace, "instructions": "be brief"})
    world.hub.deliveries.append(delivery)
    return delivery


def _settle(world: World, delivery: dict) -> dict:
    return world.hub.settled[delivery["delivery_id"]]


def test_the_heartbeat_advertises_resolve():
    world = _world()
    world.spoke.tick()
    assert world.hub.heartbeats[0]["capabilities"] == ["resolve"]


def test_an_allowed_edf_is_resolved_with_no_agent_running():
    world = _world()
    delivery = _resolve_delivery(world)
    world.spoke.tick()
    settled = _settle(world, delivery)
    assert settled["status"] == "accepted"
    result = settled["result"]
    assert result["resolution"] == "resolved"
    assert result["resolved"]["sha256"] == resolve_edf(EDF).sha256
    assert result["resolved"]["source_sha256"] == SOURCE
    assert not [w for who, w in world.timeline if who == "agent"]


@pytest.mark.parametrize("section, loc", [
    ({"presets": [], "tool_servers": ["https://tools.example/mcp/"]}, "spec.tools.preset"),
    ({"presets": ["mirarun-default"], "tool_servers": ["https://tools.example/mcp"]},
     "spec.tools.mcpServers.0.url"),
    ({"presets": ["mirarun-default"], "tool_servers": ["https://elsewhere.example/"]},
     "spec.tools.mcpServers.0.url"),
])
def test_an_edf_naming_what_the_allowlist_does_not_is_invalid_and_never_resolved(section, loc):
    world = _world(section)
    delivery = _resolve_delivery(world)
    world.spoke.tick()
    result = _settle(world, delivery)["result"]
    assert result["resolution"] == "invalid"
    assert [e["loc"] for e in result["errors"]] == [loc]
    assert "resolved" not in result


def test_a_target_outside_the_allowlist_is_invalid():
    world = _world({**ALLOW, "targets": ["staging"]})
    edf = {**EDF, "spec": {**EDF["spec"], "targets": [{"name": "production"}]}}
    delivery = _resolve_delivery(world, edf=edf)
    world.spoke.tick()
    result = _settle(world, delivery)["result"]
    assert [e["loc"] for e in result["errors"]] == ["spec.targets.0.name"]


def test_a_namespace_without_an_edf_section_resolves_nothing():
    world = _world(edf_section=None)
    delivery = _resolve_delivery(world)
    world.spoke.tick()
    result = _settle(world, delivery)["result"]
    assert result["resolution"] == "invalid"
    assert "no edf allowlist" in result["errors"][0]["message"]


def test_a_malformed_edf_is_invalid_with_the_resolvers_errors():
    world = _world()
    delivery = _resolve_delivery(world, edf={**EDF, "apiVersion": "v0"})
    world.spoke.tick()
    result = _settle(world, delivery)["result"]
    assert result["resolution"] == "invalid" and result["errors"][0]["loc"] == "apiVersion"


@pytest.mark.parametrize("namespace, organization", [("not-served", TENANT), (NS, OTHER)])
def test_a_namespace_this_spoke_does_not_serve_for_that_tenant_is_rejected(namespace, organization):
    world = _world()
    delivery = _resolve_delivery(world, namespace=namespace, organization=organization)
    world.spoke.tick()
    settled = _settle(world, delivery)
    assert settled["status"] == "rejected" and "result" not in settled


# -- the launch re-check ------------------------------------------------------

def _launch(world: World, environment) -> dict:
    intent = world.running()
    payload = {"instance": "main", "prompt": "go", "provenance": {}, "mirarun": {}}
    if environment is not None:
        payload["environment"] = environment
    delivery = world.deliver(intent, kind="launch", payload=payload)
    world.spoke.tick()
    return delivery


def _turns(world: World) -> list[str]:
    return [w for who, w in world.timeline if who == "agent" and w.startswith("POST")
            and "/turns" in w]


def test_a_launch_with_the_hash_it_resolves_to_goes_out_with_its_snapshot():
    world = _world()
    delivery = _launch(world, {"edf": EDF, "resolved_sha256": resolve_edf(EDF).sha256})
    settled = _settle(world, delivery)
    assert settled["status"] == "accepted"
    assert settled["result"]["snapshot_sha256"] == resolve_edf(EDF).sha256
    assert _turns(world)


def test_a_launch_whose_hash_differs_is_refused_and_never_forwarded():
    world = _world()
    delivery = _launch(world, {"edf": EDF, "resolved_sha256": "0" * 64})
    settled = _settle(world, delivery)
    assert settled["status"] == "rejected" and "different hash" in settled["detail"]
    assert not _turns(world)


def test_a_launch_whose_edf_is_no_longer_allowed_is_refused():
    world = _world({"presets": [], "tool_servers": []})
    delivery = _launch(world, {"edf": EDF, "resolved_sha256": resolve_edf(EDF).sha256})
    settled = _settle(world, delivery)
    assert settled["status"] == "rejected" and "no longer allowed" in settled["detail"]
    assert not _turns(world)


def test_a_launch_from_an_older_hub_carries_no_environment_and_is_not_checked():
    world = _world(edf_section=None)
    delivery = _launch(world, None)
    settled = _settle(world, delivery)
    assert settled["status"] == "accepted" and "snapshot_sha256" not in settled["result"]


def test_the_edf_section_is_checked():
    base = {"classes": ["ephemeral"], "organizations": [str(TENANT)]}
    for bad in ("all", {"presets": "mirarun-default"}, {"tool_servers": ["ftp://x"]}):
        with pytest.raises(SpokeConfigInvalid):
            _config(namespaces={NS: {**base, "edf": bad}})
    policy = _config(namespaces={NS: {**base, "edf": ALLOW}}).namespaces[NS].edf
    assert policy.allows_tool_server("https://tools.example/mcp/docs")
    assert not policy.allows_tool_server("https://tools.example/mcpx")
    assert _config(namespaces={NS: base}).namespaces[NS].edf is None
    assert isinstance(TENANT, UUID)


# -- the namespace's required RuntimeClass -----------------------------------

def test_a_namespace_pins_the_runtime_class_its_pods_run_under():
    base = {"classes": ["ephemeral"], "organizations": [str(TENANT)]}
    config = _config(namespaces={NS: {**base, "runtime_class": "gvisor"}})
    assert config.namespaces[NS].runtime_class == "gvisor"
    assert config.allows(NS, "ephemeral", TENANT) is None
    assert config.served_namespaces() == [
        {"namespace": NS, "classes": ["ephemeral"], "runtime_class": "gvisor"}]
    # The persistent template in _config sets no RuntimeClass: a namespace
    # pinning one and allowing that class is refused at start.
    with pytest.raises(SpokeConfigInvalid, match="runtime_class"):
        _config(namespaces={NS: {**base, "classes": ["persistent"], "runtime_class": "gvisor"}})
    with pytest.raises(SpokeConfigInvalid, match="RuntimeClass name"):
        _config(namespaces={NS: {**base, "runtime_class": "Not_A_Name"}})


def test_an_intent_is_refused_when_its_template_does_not_run_under_the_pinned_class():
    from dataclasses import replace

    config = _config(namespaces={NS: {"classes": ["ephemeral"], "organizations": [str(TENANT)],
                                      "runtime_class": "gvisor"}})
    template = replace(config.classes["ephemeral"], runtime_class_name="runc")
    loose = replace(config, classes={**config.classes, "ephemeral": template})
    assert "RuntimeClass 'gvisor'" in loose.allows(NS, "ephemeral", TENANT)
