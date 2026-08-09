"""The run-control contract on miragend: native resolve, launch routed by the
EDF's metadata.name, run-scoped reads proxied to the owning agent. One
control plane, one address, N agents."""

import json

import httpx
from fastapi.testclient import TestClient

from miragen.daemon.api import DAEMON_CAPABILITIES, create_app
from miragen.daemon.contract import CONTRACT_CAPABILITIES

from tests.test_daemon_api import _make_core
from tests.test_daemon_core import VALID_YAML

INTERNAL = "shared-internal-token"


def _model_edf(agent_name: str = "scribe") -> dict:
    return {
        "apiVersion": "mirarun.io/v1alpha1",
        "kind": "Environment",
        "metadata": {"name": agent_name},
        "spec": {
            "executor": {"kind": "model", "model": "test:whatever"},
            "workspace": {"repositories": []},
        },
    }


class AgentTransport(httpx.MockTransport):
    """Simulates the managed agents' HTTP surface, recording every call."""

    def __init__(self, runs_by_agent: dict[str, set[str]]):
        self.calls: list[tuple[str, str]] = []
        self.runs_by_agent = runs_by_agent
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        agent = request.url.host
        path = request.url.path
        self.calls.append((agent, path))
        assert request.headers["X-Miragen-Token"] == INTERNAL
        owned = self.runs_by_agent.get(agent, set())
        if path == "/executor-runs":
            return httpx.Response(
                202,
                json={
                    "run_id": "r-new",
                    "status": "running",
                    "snapshot_sha256": "0" * 64,
                    "duplicate": False,
                },
            )
        parts = path.strip("/").split("/")
        if parts[0] == "runs" and parts[1] in owned:
            if len(parts) == 2:
                return httpx.Response(200, json={"run_id": parts[1], "status": "succeeded"})
            return httpx.Response(200, json={"run_id": parts[1], "count": 0, "events": []})
        return httpx.Response(404, json={"error": "unknown run"})


def _client(tmp_path, *, internal_token: str = INTERNAL, agents=("scribe",), transport=None):
    core = _make_core(tmp_path)
    for name in agents:
        core.create_agent(name, VALID_YAML.format(name=name))
    app = create_app(
        core,
        token="daemon-token",
        internal_token=internal_token,
        contract_transport=transport,
    )
    return TestClient(app)


def test_contract_capabilities_advertised_only_when_configured(tmp_path) -> None:
    """A capability string is a promise: without the shared token the routes
    fail closed, so they must not be advertised."""
    with_token = _client(tmp_path, agents=())
    without = _client(tmp_path / "b", internal_token="", agents=())

    advertised = with_token.get("/health").json()["capabilities"]
    bare = without.get("/health").json()["capabilities"]

    assert set(CONTRACT_CAPABILITIES) <= set(advertised)
    assert set(CONTRACT_CAPABILITIES).isdisjoint(set(bare))
    assert set(DAEMON_CAPABILITIES) <= set(bare)


def test_contract_routes_fail_closed_without_the_token(tmp_path) -> None:
    """503 unconfigured, not 404 — a client can tell 'daemon too old' from
    'daemon missing its token'."""
    client = _client(tmp_path, internal_token="", agents=())

    response = client.post("/profiles/resolve", json={"edf": _model_edf()})

    assert response.status_code == 503
    assert response.json()["code"] == "contract_unconfigured"


def test_contract_routes_reject_the_wrong_credential(tmp_path) -> None:
    """These routes take the contract's X-Miragen-Token — the daemon's own
    bearer must NOT work here, or the two credential classes blur."""
    client = _client(tmp_path, agents=())

    wrong = client.post(
        "/profiles/resolve",
        json={"edf": _model_edf()},
        headers={"Authorization": "Bearer daemon-token"},
    )

    assert wrong.status_code == 401


def test_resolve_is_served_natively_with_per_agent_compatibility(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/profiles/resolve",
        json={"edf": _model_edf("scribe")},
        headers={"X-Miragen-Token": INTERNAL},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["sha256"]) == 64
    assert body["resolved_profile"]["spec"]["model"] == "test:whatever"
    # VALID_YAML configures the same model, so the named agent matches.
    assert body["agent_compatibility"]["compatible"] is True


def test_resolve_reports_an_unmanaged_agent_honestly(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/profiles/resolve",
        json={"edf": _model_edf("ghost")},
        headers={"X-Miragen-Token": INTERNAL},
    )

    compatibility = response.json()["agent_compatibility"]
    assert compatibility["compatible"] is False
    assert any("not managed" in issue for issue in compatibility["issues"])


def test_launch_routes_by_the_edfs_metadata_name(tmp_path) -> None:
    transport = AgentTransport({"scribe": set()})
    client = _client(tmp_path, transport=transport)

    response = client.post(
        "/executor-runs",
        json={
            "prompt": "do the thing",
            "idempotency_key": "k-1",
            "edf": _model_edf("scribe"),
            "expected_sha256": "0" * 64,
            "provenance": {},
        },
        headers={"X-Miragen-Token": INTERNAL},
    )

    assert response.status_code == 202, response.text
    assert response.json()["run_id"] == "r-new"
    assert ("scribe", "/executor-runs") in transport.calls


def test_launch_without_an_edf_is_refused_with_the_reason(tmp_path) -> None:
    """EDF-less launches have no routing information; the daemon says so
    rather than guessing an agent."""
    client = _client(tmp_path)

    response = client.post(
        "/executor-runs",
        json={"prompt": "x", "idempotency_key": "k"},
        headers={"X-Miragen-Token": INTERNAL},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "edf_required"


def test_launch_to_an_unmanaged_agent_is_a_404(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/executor-runs",
        json={
            "prompt": "x",
            "idempotency_key": "k",
            "edf": _model_edf("ghost"),
        },
        headers={"X-Miragen-Token": INTERNAL},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "agent_not_found"


def test_run_reads_route_to_the_launching_agent_without_probing(tmp_path) -> None:
    """The launch response indexes run→agent, so the events poll that
    follows goes straight to the owner."""
    transport = AgentTransport({"scribe": {"r-new"}})
    client = _client(tmp_path, transport=transport)
    client.post(
        "/executor-runs",
        json={"prompt": "x", "idempotency_key": "k", "edf": _model_edf("scribe")},
        headers={"X-Miragen-Token": INTERNAL},
    )
    transport.calls.clear()

    response = client.get(
        "/runs/r-new/events",
        params={"after": 0, "limit": 10},
        headers={"X-Miragen-Token": INTERNAL},
    )

    assert response.status_code == 200
    assert transport.calls == [("scribe", "/runs/r-new/events")]


def test_run_reads_discover_the_owner_when_the_index_is_cold(tmp_path) -> None:
    """The index is an optimization, never a source of truth: after a daemon
    restart the owner is found by asking the managed agents."""
    transport = AgentTransport({"scribe": {"r-old"}, "quill": set()})
    client = _client(tmp_path, agents=("quill", "scribe"), transport=transport)

    response = client.get(
        "/runs/r-old", headers={"X-Miragen-Token": INTERNAL}
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "r-old"
    probed = [agent for agent, path in transport.calls if path == "/runs/r-old"]
    assert "scribe" in probed


def test_unknown_runs_are_a_404_not_a_hang(tmp_path) -> None:
    transport = AgentTransport({"scribe": set()})
    client = _client(tmp_path, transport=transport)

    response = client.get(
        "/runs/r-ghost", headers={"X-Miragen-Token": INTERNAL}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_only_contract_subpaths_are_proxied(tmp_path) -> None:
    """The daemon forwards exactly what it promised — an arbitrary agent
    route does not become reachable through it."""
    transport = AgentTransport({"scribe": {"r-1"}})
    client = _client(tmp_path, transport=transport)

    response = client.get(
        "/runs/r-1/workspace-files", headers={"X-Miragen-Token": INTERNAL}
    )

    assert response.status_code == 404
    assert transport.calls == []
