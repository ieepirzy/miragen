"""memory.backend: bridge — a miragen-driven agent taking part in the hosted
session plane, tested against the REAL SessionPlane (ephemeral Loimi) served
by miragend's HTTP app."""

from __future__ import annotations

import httpx
import pytest

from miragen.daemon.api import create_app
from miragen.memory.bridge import UNAVAILABLE_NOTE, BridgeMemory
from miragen.memory.selection import Selection, SelectionResult
from miragen.models import AgentProfile, MemorySpec
from tests.test_sessions_plane import Harness as PlaneHarness

PROJECT = "github.com/ieepirzy/mira"


def spec() -> MemorySpec:
    return MemorySpec.model_validate({"backend": "bridge", "project": PROJECT,
                                      "endpoint_env": "MIRAGEND_URL",
                                      "credential_env": "MIRAGEND_TOKEN"})


def vault_selector(calls):
    async def selector(request, cards):
        calls.append(request)
        return SelectionResult(selections=[
            Selection(record_id=c["record_id"], reason="relevant")
            for c in cards if "4417" in c["payload"]["text"]])
    return selector


def bridge_for(plane_harness, token="bridge-secret") -> BridgeMemory:
    app = create_app(None, token=token, sessions=plane_harness.plane)
    return BridgeMemory(spec=spec(), agent_name="mira", base_url="http://plane",
                        token=token, transport=httpx.ASGITransport(app=app))


async def test_turns_open_a_session_get_recall_from_the_plane_and_are_captured(tmp_path):
    calls = []
    ph = PlaneHarness(tmp_path, selector=vault_selector(calls))
    bridge = bridge_for(ph)

    first = await bridge.prepare(instance="conv_1", run_id="r1", prompt="hello there, Mira")
    assert "[memory guide" in first            # the plane's guidance on session open
    key = bridge.session_key("conv_1")
    assert key == "miragen:mira-conv_1" and ph.plane.find_session(key) is not None

    # seed a memory in the project's scope, then ask about it
    scope = next(s for s in ph.plane._lifecycles if "mira" in s)
    lifecycle = ph.plane._lifecycles[scope]
    await lifecycle.remember(instance="x", run_id="seed", content="the storage code is 4417")
    await lifecycle.remember(instance="x", run_id="seed2", content="lunch was good")
    second = await bridge.prepare(instance="conv_1", run_id="r2",
                                  prompt="what was the storage code again?")
    assert "4417" in second and "lunch" not in second
    assert "[memory guide" not in second        # guidance only on open
    assert len(calls) == 1                      # the PLANE's selector ran

    await bridge.finish(instance="conv_1", run_id="r2", output="It's 4417.", status="succeeded")
    await ph.drain()
    assert any(e["content"] == "It's 4417." for e in ph.service.events.values()
               if isinstance(e.get("content"), str))


async def test_wrong_bearer_and_unreachable_plane_degrade_explicitly(tmp_path):
    ph = PlaneHarness(tmp_path)
    good = bridge_for(ph)
    bad = BridgeMemory(spec=spec(), agent_name="mira", base_url="http://plane", token="wrong",
                       transport=good.transport)
    assert await bad.prepare(instance="c", run_id="r", prompt="hi") == UNAVAILABLE_NOTE

    def down(request):
        raise httpx.ConnectError("refused", request=request)
    offline = BridgeMemory(spec=spec(), agent_name="mira", base_url="http://plane",
                           token="t", transport=httpx.MockTransport(down))
    assert await offline.prepare(instance="c", run_id="r", prompt="hi") == UNAVAILABLE_NOTE
    await offline.finish(instance="c", run_id="r", output="x", status="succeeded")  # never raises


def test_bridge_spec_validation():
    with pytest.raises(Exception, match="project"):
        MemorySpec.model_validate({"backend": "bridge"})
    with pytest.raises(Exception, match="owns scopes"):
        MemorySpec.model_validate({"backend": "bridge", "project": PROJECT, "scopes": {
            "read": ["a"], "propose": ["a"], "default_write": "a"}})
    with pytest.raises(Exception, match="needs `scopes`"):
        MemorySpec.model_validate({"backend": "loimi"})
    p = AgentProfile.model_validate({
        "name": "mira", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "grok-build:", "instructions": "i"},
        "memory": {"backend": "bridge", "project": PROJECT,
                   "endpoint_env": "MIRAGEND_URL", "credential_env": "MIRAGEND_TOKEN"}})
    assert p.memory.backend == "bridge"


def test_from_env_requires_the_plane_url():
    with pytest.raises(ValueError, match="MIRAGEND_URL"):
        BridgeMemory.from_env(spec(), "mira", {})
    b = BridgeMemory.from_env(spec(), "mira", {"MIRAGEND_URL": "http://10.8.0.4:8420/",
                                               "MIRAGEND_TOKEN": "t"})
    assert b.base_url == "http://10.8.0.4:8420" and b.token == "t"
