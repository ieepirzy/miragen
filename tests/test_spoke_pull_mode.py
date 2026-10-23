"""The spoke's pull loop against a fake hub, a fake API server and a fake
agent (httpx.MockTransport everywhere): no cluster, no hub, no agent."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from miragen.daemon.spawn.kubernetes_workloads import KubernetesWorkloadDriver
from miragen.daemon.spoke.agents import AgentClient
from miragen.daemon.spoke.config import SpokeConfigInvalid, parse_config
from miragen.daemon.spoke.hub import HubClient, HubUnavailable
from miragen.daemon.spoke.loop import Spoke, workload_name
from tests.test_kubernetes_workloads import NS, FakeKubernetes

TENANT = uuid4()
OTHER = uuid4()
IMAGE = "registry.invalid/miragen@sha256:" + "a" * 64
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _config(**changes):
    raw = {
        "hub": {"url": "https://hub.invalid", "token_file": "/nonexistent"},
        "images": [IMAGE],
        "classes": {
            "ephemeral": {"image": IMAGE, "service_account_name": "agent-sandbox",
                          "runtime_class_name": "gvisor"},
            "persistent": {"image": IMAGE, "service_account_name": "agent-sandbox",
                           "storage_size": "10Gi"},
        },
        "namespaces": {NS: {"classes": ["ephemeral", "persistent"],
                            "organizations": [str(TENANT)]}},
        "network_label_keys": ["access.mirarun.io/loimi"],
    }
    raw.update(changes)
    return parse_config(raw)


class Timeline(list):
    """Every hub, API-server and agent call, in order."""


class FakeHub:
    def __init__(self, timeline: Timeline) -> None:
        self.timeline = timeline
        self.intents: list[dict] = []
        self.deliveries: list[dict] = []
        self.acks: list[tuple[str, dict]] = []
        self.reports: list[tuple[str, dict]] = []
        self.settles: list[tuple[str, dict]] = []
        self.pages: list[tuple[str, dict]] = []
        self.waits: list[tuple[str, int]] = []
        self.fail: dict[str, int] = {}  # route kind -> status to answer once

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/spoke/v1/")
        body = json.loads(request.content or b"{}")
        kind = path.rsplit("/", 1)[-1] if "/" in path else path
        if path.startswith("runs/"):
            kind = "events"
        self.timeline.append(("hub", kind))
        if kind in self.fail:
            return httpx.Response(self.fail.pop(kind), json={"detail": "injected"})
        if path.endswith("/claim"):
            self.waits.append((path, body.get("wait_seconds", -1)))
        if path == "intents/claim":
            out, self.intents = self.intents, []
            return httpx.Response(200, json={"intents": out})
        if path == "deliveries/claim":
            out, self.deliveries = self.deliveries, []
            return httpx.Response(200, json={"deliveries": out})
        if kind == "ack":
            self.acks.append((path, body))
            return httpx.Response(204)
        if kind == "observations":
            self.reports.append((path, body))
            return httpx.Response(204)
        if kind == "settle":
            self.settles.append((path, body))
            return httpx.Response(200, json={"status": body["status"]})
        if kind == "events":
            assert set(body) == {"run", "events"}, body
            assert body["run"]["run_id"] == body["events"]["run_id"]
            self.pages.append((path, body))
            return httpx.Response(204)
        raise AssertionError(path)


class FakeAgent:
    def __init__(self, timeline: Timeline) -> None:
        self.timeline = timeline
        self.turns: dict[str, str] = {}
        self.forwarded: list[tuple[str, str]] = []
        self.events: list[dict] = []
        self.status = "running"
        self.raise_on: dict[str, type[Exception]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.timeline.append(("agent", f"{request.method} {path}"))
        assert request.headers["X-Miragen-Token"] == "internal-1"
        for fragment, error in list(self.raise_on.items()):
            if fragment in path:
                raise error("injected", request=request)
        if request.method == "POST" and path.endswith("/turns"):
            body = json.loads(request.content)
            key = body["idempotency_key"]
            duplicate = key in self.turns
            self.turns.setdefault(key, f"turn-{len(self.turns) + 1}")
            self.forwarded.append(("turn", key))
            return httpx.Response(200 if duplicate else 202, json={
                "turn_id": self.turns[key], "status": "running", "duplicate": duplicate})
        if request.method == "DELETE" and path.startswith("/instances/"):
            self.forwarded.append(("discard", path))
            return httpx.Response(200, json={})
        if request.method == "POST" and path.endswith(("/resume", "/abandon")):
            run_id = path.split("/")[2]
            self.forwarded.append((path.rsplit("/", 1)[-1], run_id))
            return httpx.Response(200, json={"run_id": run_id, "status": "running"})
        if request.method == "GET" and path.endswith("/events"):
            after = int(request.url.params["after"])
            events = [e for e in self.events if e["seq"] > after]
            return httpx.Response(200, json={
                "run_id": path.split("/")[2], "count": len(events), "events": events,
                "next_after": events[-1]["seq"] if events else after, "has_more": False})
        if request.method == "GET" and path.startswith("/runs/"):
            return httpx.Response(200, json={"run_id": path.split("/")[2], "status": self.status})
        raise AssertionError(path)


class World:
    def __init__(self, **config_changes) -> None:
        self.timeline = Timeline()
        self.hub = FakeHub(self.timeline)
        self.k8s = FakeKubernetes()
        self.agent = FakeAgent(self.timeline)
        original = self.k8s.handler

        def k8s_handler(request: httpx.Request) -> httpx.Response:
            if request.method != "GET":
                self.timeline.append(("k8s", f"{request.method} {request.url.path}"))
            return original(request)

        self.spoke = Spoke(
            config=_config(**config_changes),
            hub=HubClient("https://hub.invalid", "mrsp_x",
                          transport=httpx.MockTransport(self.hub.handler)),
            driver=KubernetesWorkloadDriver(base_url="https://k8s.invalid", token="t",
                                            transport=httpx.MockTransport(k8s_handler)),
            agents=AgentClient(transport=httpx.MockTransport(self.agent.handler)),
            clock=lambda: NOW,
            new_token=lambda: "internal-1",
        )

    def offer(self, **changes) -> dict:
        intent = {
            "organization_id": str(TENANT), "intent_id": str(uuid4()), "product": "apogee",
            "namespace": NS, "workload_class": "ephemeral", "principal": "agent.profile.analyst",
            "generation": 1, "desired_state": "running", "instance_count": 1,
            "profile_name": "analyst", "profile_sha256": "b" * 64,
            "profile_yaml": "name: analyst\n", "grant_set": {}, "network_labels": {},
            "deadline_at": (NOW + timedelta(hours=1)).isoformat(), "trace_id": None,
            "attempt": 1, "lease_expires_at": (NOW + timedelta(minutes=1)).isoformat(),
        }
        intent.update(changes)
        self.hub.intents.append(intent)
        return intent

    def deliver(self, intent: dict, **changes) -> dict:
        delivery = {
            "organization_id": intent["organization_id"], "delivery_id": str(uuid4()),
            "intent_id": intent["intent_id"], "kind": "turn", "idempotency_key": "k-1",
            "at_most_once": False, "payload": {"instance": "main", "prompt": "analyse it"},
            "run_intent_id": str(uuid4()), "trace_id": None, "attempt": 1,
            "lease_expires_at": None,
        }
        delivery.update(changes)
        self.hub.deliveries.append(delivery)
        return delivery

    def run_pod(self, intent: dict) -> str:
        name = workload_name(UUID(intent["intent_id"]))
        self.k8s.add_pod(name, phase="Running", state={"running": {"startedAt": "t0"}})
        return name


def _kinds(timeline, source):
    return [what for who, what in timeline if who == source]


def test_an_intent_is_applied_then_acked_then_reported():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    order = [(who, what) for who, what in world.timeline
             if who == "k8s" and "/jobs" in what or who == "hub" and what in ("ack", "observations")]
    assert order[0][0] == "k8s" and "POST" in order[0][1]
    assert [what for _, what in order[1:]] == ["ack", "observations"]
    assert world.hub.acks[0][1] == {"generation": 1, "attempt": 1}
    assert world.hub.reports[0][1]["phase"] == "pending"
    job = world.k8s.objects["jobs"][workload_name(UUID(intent["intent_id"]))]
    assert job["spec"]["activeDeadlineSeconds"] == 3600


def test_a_namespace_outside_the_allowlist_is_refused_and_never_applied():
    world = World()
    world.offer(namespace="kube-system")
    world.spoke.tick()
    assert world.k8s.objects["jobs"] == {}
    assert world.hub.reports[0][1]["phase"] == "refused"
    assert len(world.hub.acks) == 1


def test_another_tenants_intent_is_refused():
    world = World()
    world.offer(organization_id=str(OTHER))
    world.spoke.tick()
    assert world.k8s.objects["jobs"] == {}
    assert "organization" in world.hub.reports[0][1]["reason"]


def test_a_network_label_the_spoke_does_not_allow_is_refused():
    world = World()
    world.offer(network_labels={"access.mirarun.io/everything": "true"})
    world.spoke.tick()
    assert world.k8s.objects["jobs"] == {}
    assert world.hub.reports[0][1]["phase"] == "refused"


def test_a_reoffered_lease_is_applied_once():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    world.hub.intents.append(dict(intent, attempt=2))
    world.spoke.tick()
    creates = [w for w in _kinds(world.timeline, "k8s") if w.startswith("POST") and "/jobs" in w]
    assert len(creates) == 1
    assert [a[1]["attempt"] for a in world.hub.acks] == [1, 2]


def test_an_ended_job_is_deleted_only_after_the_hub_takes_the_final_report():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    name = workload_name(UUID(intent["intent_id"]))
    world.k8s.objects["jobs"][name]["status"] = {"failed": 1}
    world.k8s.add_pod(name, phase="Failed", state={"terminated": {"exitCode": 1}})
    world.hub.fail["observations"] = 503
    with pytest.raises(HubUnavailable):
        world.spoke.tick()
    assert name in world.k8s.objects["jobs"]  # the report did not land: keep it
    world.spoke.tick()
    assert world.hub.reports[-1][1]["phase"] == "failed"
    assert world.hub.reports[-1][1]["exit_code"] == 1
    assert name not in world.k8s.objects["jobs"]


def test_a_deleted_intent_removes_everything_including_the_volume():
    world = World()
    intent = world.offer(workload_class="persistent", deadline_at=None)
    world.spoke.tick()
    name = workload_name(UUID(intent["intent_id"]))
    world.k8s.objects["persistentvolumeclaims"][f"state-{name}-0"] = {"metadata": {"name": "x"}}
    world.hub.intents.append(dict(intent, generation=2, desired_state="deleted", attempt=2))
    world.spoke.tick()
    assert world.k8s.objects["statefulsets"] == {}
    assert world.k8s.objects["persistentvolumeclaims"] == {}
    assert world.hub.reports[-1][1]["phase"] == "deleted"


def test_a_turn_is_forwarded_settled_and_its_events_relayed():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    world.run_pod(intent)
    delivery = world.deliver(intent)
    world.agent.events = [{"seq": 1, "type": "turn.started"}, {"seq": 2, "type": "turn.done"}]
    world.spoke.tick()
    assert world.agent.forwarded == [("turn", "k-1")]
    assert world.hub.settles[0][1]["status"] == "accepted"
    assert world.hub.pages[0][0] == f"runs/{delivery['run_intent_id']}/events"
    assert [e["seq"] for e in world.hub.pages[0][1]["events"]["events"]] == [1, 2]
    assert world.hub.pages[0][1]["run"]["status"] == "running"
    assert "result" not in world.hub.settles[0][1]  # only a launch carries one
    world.agent.status = "succeeded"
    world.spoke.tick()
    assert world.spoke.runs == {}
    # One more push: the terminal record with an empty page, no event again.
    assert len(world.hub.pages) == 2
    assert world.hub.pages[1][1]["run"]["status"] == "succeeded"
    assert world.hub.pages[1][1]["events"]["events"] == []


def test_a_turn_whose_outcome_is_unknown_is_left_for_the_hub_to_offer_again():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    world.run_pod(intent)
    world.deliver(intent)
    world.agent.raise_on["/turns"] = httpx.ReadTimeout
    world.spoke.tick()
    assert world.hub.settles == []


def test_a_resume_with_an_unknown_outcome_is_uncertain_and_never_sent_again():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    world.run_pod(intent)
    delivery = world.deliver(intent, kind="resume", at_most_once=True,
                             idempotency_key="r-1", payload={"run_id": "run-9", "prompt": "go"})
    world.agent.raise_on["/resume"] = httpx.ReadTimeout
    world.hub.fail["settle"] = 503
    with pytest.raises(HubUnavailable):
        world.spoke.tick()
    sends = [w for w in _kinds(world.timeline, "agent") if w.endswith("/resume")]
    assert len(sends) == 1
    world.hub.deliveries.append(delivery)  # even if it were offered again
    world.spoke.tick()
    sends = [w for w in _kinds(world.timeline, "agent") if w.endswith("/resume")]
    assert len(sends) == 1
    assert world.hub.settles[-1][1]["status"] == "uncertain"


def test_a_resume_that_never_left_is_rejected_not_uncertain():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    world.run_pod(intent)
    world.deliver(intent, kind="abandon", at_most_once=True, idempotency_key="a-1",
                  payload={"run_id": "run-9"})
    world.agent.raise_on["/abandon"] = httpx.ConnectError
    world.spoke.tick()
    assert world.hub.settles[0][1]["status"] == "rejected"


def test_a_delivery_for_an_intent_this_spoke_does_not_run_is_rejected():
    world = World()
    world.deliver({"organization_id": str(TENANT), "intent_id": str(uuid4())})
    world.spoke.tick()
    assert world.hub.settles[0][1]["status"] == "rejected"
    assert world.agent.forwarded == []


def test_recover_picks_up_running_workloads_from_their_labels():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    fresh = World()
    fresh.k8s = world.k8s
    fresh.spoke.driver = world.spoke.driver
    fresh.spoke.recover()
    tracked = fresh.spoke.workloads[UUID(intent["intent_id"])]
    assert tracked.organization_id == TENANT and tracked.generation == 1


def test_config_refuses_an_inline_token_and_unlisted_images():
    with pytest.raises(SpokeConfigInvalid):
        _config(hub={"url": "https://hub.invalid"})
    with pytest.raises(SpokeConfigInvalid):
        _config(classes={"ephemeral": {"image": "elsewhere/img:latest",
                                       "service_account_name": "sa"}})


def test_an_abandon_the_agent_refused_is_not_sent_again():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    world.run_pod(intent)
    delivery = world.deliver(intent, kind="abandon", at_most_once=True, idempotency_key="a-2",
                             payload={"run_id": "run-9"})
    world.spoke.tick()
    world.hub.deliveries.append(delivery)
    world.spoke.tick()
    sends = [w for w in _kinds(world.timeline, "agent") if w.endswith("/abandon")]
    assert len(sends) == 1


def test_events_written_just_before_the_run_stops_are_still_relayed():
    """The race: an empty final page, then the agent writes its last events
    and stops, then the status read says stopped. Those events must go up."""
    world = World()
    intent = world.offer()
    world.spoke.tick()
    world.run_pod(intent)
    world.deliver(intent)
    world.spoke.tick()  # the turn is forwarded and tracked; no events yet
    assert world.hub.pages == []
    original = world.agent.handler

    def late_events(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/runs/") \
                and not request.url.path.endswith("/events"):
            # Between the empty page and this status read, the run finished.
            world.agent.events = [{"seq": 1, "type": "turn.done"}]
            world.agent.status = "succeeded"
        return original(request)

    world.spoke.agents = AgentClient(transport=httpx.MockTransport(late_events))
    world.spoke.tick()
    assert [e["seq"] for page in world.hub.pages for e in page[1]["events"]["events"]] == [1]
    world.spoke.tick()
    assert world.spoke.runs == {}


def test_an_accepted_launch_tells_the_hub_which_agent_run_it_started():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    world.run_pod(intent)
    world.deliver(intent, kind="launch", idempotency_key="launch-1",
                  payload={"prompt": "go", "provenance": {}, "mirarun": {}})
    world.spoke.tick()
    settle = world.hub.settles[0][1]
    assert settle["status"] == "accepted"
    assert settle["result"] == {"run_id": "turn-1", "status": "running", "duplicate": False}


def test_claims_long_poll_when_idle_and_return_at_once_while_relaying():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    assert ("intents/claim", 5) in world.hub.waits
    assert ("deliveries/claim", 25) in world.hub.waits
    world.run_pod(intent)
    world.deliver(intent)
    world.spoke.tick()  # the turn is now tracked for relay
    world.hub.waits.clear()
    world.spoke.tick()
    assert world.hub.waits == [("intents/claim", 0), ("deliveries/claim", 0)]


def test_a_launchs_minted_credentials_are_forwarded_into_the_turn():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    world.run_pod(intent)
    bodies = []
    original = world.agent.handler

    def capture(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/turns"):
            bodies.append(json.loads(request.content))
        return original(request)

    world.spoke.agents = AgentClient(transport=httpx.MockTransport(capture))
    world.deliver(intent, kind="launch", idempotency_key="launch-2",
                  payload={"prompt": "go", "provenance": {}, "mirarun": {}},
                  credentials={"secret_env": {"MIRARUN_MCP_TOKEN": "minted"},
                               "repositories": {}})
    world.spoke.tick()
    assert bodies[0]["credentials"] == {"secret_env": {"MIRARUN_MCP_TOKEN": "minted"}}
    assert "minted" not in repr(world.spoke.workloads)


def test_credentials_keyed_by_binding_name_are_refused_not_dropped():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    world.run_pod(intent)
    world.deliver(intent, kind="launch", idempotency_key="launch-3",
                  payload={"prompt": "go", "provenance": {}, "mirarun": {}},
                  credentials={"secrets": {"mirarun-mcp": "minted"}, "repositories": {}})
    world.spoke.tick()
    assert world.hub.settles[0][1]["status"] == "rejected"
    assert "secret_env" in world.hub.settles[0][1]["detail"]
    assert world.agent.forwarded == []
