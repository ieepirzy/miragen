"""The spoke's heartbeat (MiraRun ADR-050 Decision 3): what the spoke says
about itself, at start and every `heartbeat.interval_seconds`, validated
against the hub's published contract by the strict fake hub. A hub from
before the route (404) is not asked again, and nothing about the heartbeat
ever stops the pull loop."""

from __future__ import annotations

import pytest

from miragen.daemon.spoke.config import SpokeConfigInvalid
from tests.test_spoke_pull_mode import NS, World, _config


def test_the_first_tick_sends_a_heartbeat_the_contract_accepts():
    world = World()
    world.spoke.host_name = lambda: "spoke-pod-1"
    world.spoke.version = "miragen 0.2.0 (abc123def456)"
    world.spoke.tick()
    assert world.hub.heartbeats == [{
        "version": "miragen 0.2.0 (abc123def456)",
        "address": "spoke-pod-1",
        "namespaces": [{"namespace": NS, "classes": ["ephemeral", "persistent"]}],
        "capabilities": [],
        "started_at": world.spoke.started_at.isoformat(),
    }]
    # Before the claims: a restarted spoke says what it is first.
    hub_calls = [what for who, what in world.timeline if who == "hub"]
    assert hub_calls[0] == "heartbeat"


def test_the_configured_address_wins_over_the_host_name():
    world = World(heartbeat={"address": "spoke.cluster.example:8080"})
    world.spoke.tick()
    assert world.hub.heartbeats[0]["address"] == "spoke.cluster.example:8080"


def test_it_repeats_every_interval_and_not_more_often():
    world = World(heartbeat={"interval_seconds": 30})
    now = {"t": 0.0}
    world.spoke.monotonic = lambda: now["t"]
    world.spoke.tick()
    now["t"] = 29.0
    world.spoke.tick()
    assert len(world.hub.heartbeats) == 1
    now["t"] = 30.0
    world.spoke.tick()
    assert len(world.hub.heartbeats) == 2


def test_a_hub_without_the_route_is_not_asked_again_and_the_loop_goes_on():
    world = World()
    world.hub.heartbeat_route = False
    now = {"t": 0.0}
    world.spoke.monotonic = lambda: now["t"]
    intent = world.offer()
    world.spoke.tick()
    now["t"] = 120.0
    world.spoke.tick()
    heartbeats = [what for who, what in world.timeline if who == "hub" and what == "heartbeat"]
    assert heartbeats == ["heartbeat"]
    assert world.hub.acks and world.hub.acks[0][0].endswith(f"{intent['intent_id']}/ack")


@pytest.mark.parametrize("status", [422, 500, 503])
def test_a_failed_heartbeat_never_stops_the_tick(status):
    world = World()
    world.hub.fail["heartbeat"] = status
    world.offer()
    world.spoke.tick()
    assert world.hub.acks, "the intent was still claimed and applied"
    assert world.spoke._heartbeat_supported


def test_a_heartbeat_refused_for_the_token_ends_the_tick_like_any_call():
    from miragen.daemon.spoke.hub import HubRefused

    world = World()
    world.hub.fail["heartbeat"] = 401
    with pytest.raises(HubRefused):
        world.spoke.tick()


def test_heartbeat_config_is_checked():
    assert _config().heartbeat_seconds == 30.0
    assert _config(heartbeat={"interval_seconds": 1}).heartbeat_seconds == 5.0
    with pytest.raises(SpokeConfigInvalid):
        _config(heartbeat={"address": ""})
    with pytest.raises(SpokeConfigInvalid):
        _config(heartbeat="every 30s")
