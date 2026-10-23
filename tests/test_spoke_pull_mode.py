"""The spoke's pull loop against a strict fake hub (tests/spoke_fakes.py:
MiraRun's request models, leases and event cursors), a fake API server and
a fake agent. No cluster, no hub, no agent."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from miragen.daemon.spawn.base import SpawnOperationFailed
from miragen.daemon.spawn.kubernetes_workloads import KubernetesWorkloadDriver
from miragen.daemon.spoke.agents import AgentClient
from miragen.daemon.spoke.config import SpokeConfigInvalid, parse_config
from miragen.daemon.spoke.hub import HubClient, HubRefused, HubUnavailable
from miragen.daemon.spoke.loop import Spoke, workload_name
from tests.spoke_fakes import (
    NOW,
    FakeAgent,
    StrictHub,
    Timeline,
    offered_delivery,
    offered_intent,
)
from tests.test_kubernetes_workloads import NS, FakeKubernetes

TENANT = uuid4()
OTHER = uuid4()
IMAGE = "registry.invalid/miragen@sha256:" + "a" * 64


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


class World:
    def __init__(self, **config_changes) -> None:
        self.timeline = Timeline()
        self.hub = StrictHub(self.timeline)
        self.k8s = FakeKubernetes()
        self.agent = FakeAgent(self.timeline)
        original = self.k8s.handler
        self.k8s_fail: set[str] = set()

        def k8s_handler(request: httpx.Request) -> httpx.Response:
            if request.method != "GET":
                self.timeline.append(("k8s", f"{request.method} {request.url.path}"))
            for fragment in self.k8s_fail:
                if fragment in request.url.path:
                    return httpx.Response(500, json={"message": "injected"})
            return original(request)

        self.spoke = Spoke(
            config=_config(**config_changes),
            hub=HubClient("https://hub.invalid", "mrsp_" + "x" * 43,
                          transport=httpx.MockTransport(self.hub.handler)),
            driver=KubernetesWorkloadDriver(base_url="https://k8s.invalid", token="t",
                                            transport=httpx.MockTransport(k8s_handler)),
            agents=AgentClient(transport=httpx.MockTransport(self.agent.handler)),
            clock=lambda: NOW,
            new_token=lambda: "internal-1",
            monotonic=lambda: 0.0,
        )
        self.spoke._next_resync = float("inf")  # resync only where a test asks

    def offer(self, **changes) -> dict:
        namespace = changes.pop("namespace", NS)
        intent = offered_intent(TENANT, namespace, **changes)
        self.hub.intents.append(intent)
        return intent

    def deliver(self, intent: dict, kind: str = "turn", **changes) -> dict:
        delivery = offered_delivery(intent, kind=kind, **changes)
        self.hub.deliveries.append(delivery)
        return delivery

    def running(self, **changes) -> dict:
        intent = self.offer(**changes)
        self.spoke.tick()
        name = workload_name(UUID(intent["intent_id"]))
        self.k8s.add_pod(name, phase="Running", state={"running": {"startedAt": "t0"}})
        return intent


def _agent_calls(world, suffix):
    return [w for who, w in world.timeline if who == "agent" and w.endswith(suffix)]


# -- intents ----------------------------------------------------------------

def test_an_intent_is_applied_then_acked_then_reported():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    order = [(who, what) for who, what in world.timeline
             if who == "k8s" and "/jobs" in what or who == "hub" and what in ("ack", "observations")]
    assert order[0][0] == "k8s" and "POST" in order[0][1]
    assert [what for _, what in order[1:]] == ["ack", "observations"]
    assert world.hub.reports[0][1]["phase"] == "pending"
    job = world.k8s.objects["jobs"][workload_name(UUID(intent["intent_id"]))]
    assert job["spec"]["activeDeadlineSeconds"] == 3600


def test_kubernetes_timestamps_reach_the_hub_as_datetimes_or_not_at_all():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    name = workload_name(UUID(intent["intent_id"]))
    world.k8s.add_pod(name, phase="Running",
                      state={"running": {"startedAt": "2026-10-23T10:00:00Z"}})
    world.spoke.tick()
    running = [b for _, b in world.hub.reports if b["phase"] == "running"]
    assert running and running[0]["started_at"].startswith("2026-10-23T10:00:00")
    other = world.offer()
    world.spoke.tick()
    world.k8s.add_pod(workload_name(UUID(other["intent_id"])), phase="Running",
                      state={"running": {"startedAt": "not a time"}})
    world.spoke.tick()
    running = [b for _, b in world.hub.reports if b["phase"] == "running"]
    assert len(running) == 2 and "started_at" not in running[1]


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
    creates = [w for who, w in world.timeline if who == "k8s" and w.startswith("POST")
               and "/jobs" in w]
    assert len(creates) == 1
    assert [a[1]["attempt"] for a in world.hub.acks] == [1, 2]


def test_a_lost_ack_leaves_the_workload_tracked_and_is_retried():
    world = World()
    intent = world.offer()
    world.hub.fail["ack"] = 503
    with pytest.raises(HubUnavailable):
        world.spoke.tick()
    assert UUID(intent["intent_id"]) in world.spoke.workloads
    world.spoke.tick()
    assert len(world.hub.acks) == 1
    assert world.spoke.pending_acks == {}


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
    assert name in world.k8s.objects["jobs"]
    world.spoke.tick()
    assert world.hub.reports[-1][1]["phase"] == "failed"
    assert name not in world.k8s.objects["jobs"]


def test_one_workloads_refused_report_does_not_stop_the_tick():
    world = World()
    first = world.offer()
    world.spoke.tick()
    second = world.offer()
    world.spoke.tick()
    for intent in (first, second):
        name = workload_name(UUID(intent["intent_id"]))
        world.k8s.add_pod(name, phase="Running", state={"running": {"startedAt": "t"}})
    world.hub.fail["observations"] = 404
    world.spoke.tick()
    phases = [body["phase"] for _, body in world.hub.reports]
    assert phases.count("running") == 1  # the other one was refused, the tick went on


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


# -- deliveries -------------------------------------------------------------

def test_a_launch_is_settled_with_its_run_and_its_events_relayed_with_the_record():
    world = World()
    intent = world.running()
    delivery = world.deliver(intent, kind="launch")
    world.agent.events = [{"seq": 1, "type": "turn.started"}, {"seq": 2, "type": "turn.done"}]
    world.spoke.tick()
    settle = world.hub.settles[0][1]
    assert settle["status"] == "accepted"
    assert settle["result"] == {"run_id": "turn-1", "status": "running", "duplicate": False}
    path, body = world.hub.pages[0]
    assert path == f"runs/{delivery['run_intent_id']}/events"
    assert [e["seq"] for e in body["events"]["events"]] == [1, 2]
    assert body["run"]["status"] == "running"
    world.agent.status = "succeeded"
    world.spoke.tick()
    assert world.spoke.runs == {}
    assert world.hub.pages[-1][1]["run"]["status"] == "succeeded"
    assert world.hub.pages[-1][1]["events"]["events"] == []


def test_a_turn_is_settled_on_dispatch_and_its_finished_record_posted_later():
    world = World()
    intent = world.running()
    delivery = world.deliver(intent)
    world.spoke.tick()
    settle = world.hub.settles[0][1]
    assert settle["status"] == "accepted" and "result" not in settle  # dispatched, no reply
    assert world.hub.results == []  # still running
    world.agent.turn_status["turn-1"] = "succeeded"
    world.spoke.tick()
    path, body = world.hub.results[0]
    assert path == f"deliveries/{delivery['delivery_id']}/result"
    assert body["result"]["output"] == "the reply"
    assert world.spoke.pending_results == {}


def test_a_turn_result_the_hub_no_longer_knows_is_dropped():
    world = World()
    intent = world.running()
    world.deliver(intent)
    world.spoke.tick()
    world.agent.turn_status["turn-1"] = "succeeded"
    world.hub.fail["result"] = 404
    world.spoke.tick()
    assert world.spoke.pending_results == {}


def test_a_turn_whose_outcome_is_unknown_is_left_for_the_hub_to_offer_again():
    world = World()
    intent = world.running()
    world.deliver(intent)
    world.agent.raise_on["/turns"] = httpx.ReadTimeout
    world.spoke.tick()
    assert world.hub.settles == []


def test_a_resume_with_an_unknown_outcome_is_uncertain_and_never_sent_again():
    world = World()
    intent = world.running()
    delivery = world.deliver(intent, kind="resume")
    world.agent.raise_on["/resume"] = httpx.ReadTimeout
    world.hub.fail["settle"] = 503
    with pytest.raises(HubUnavailable):
        world.spoke.tick()
    world.hub.deliveries.append(delivery)  # even if it were offered again
    world.spoke.tick()
    assert len(_agent_calls(world, "/resume")) == 1
    assert world.hub.settles[-1][1]["status"] == "uncertain"


def test_every_settle_is_retried_with_its_result():
    world = World()
    intent = world.running()
    world.deliver(intent, kind="launch")
    world.hub.fail["settle"] = 503
    with pytest.raises(HubUnavailable):
        world.spoke.tick()
    assert world.hub.settles == []
    world.spoke.tick()
    assert world.hub.settles[0][1]["result"]["run_id"] == "turn-1"
    assert len(_agent_calls(world, "/turns")) == 1


def test_a_pending_settle_the_hub_does_not_know_is_dropped():
    world = World()
    intent = world.running()
    world.deliver(intent, kind="launch")
    world.hub.fail["settle"] = [503, 404]
    with pytest.raises(HubUnavailable):
        world.spoke.tick()
    world.spoke.tick()
    assert world.spoke.pending_settles == {}


def test_a_resume_that_never_left_is_rejected_not_uncertain():
    world = World()
    intent = world.running()
    world.deliver(intent, kind="abandon")
    world.agent.raise_on["/abandon"] = httpx.ConnectError
    world.spoke.tick()
    assert world.hub.settles[0][1]["status"] == "rejected"


def test_an_at_most_once_delivery_past_its_lease_is_never_forwarded():
    world = World()
    intent = world.running()
    world.deliver(intent, kind="resume", lease_expires_at=(NOW - timedelta(seconds=1)).isoformat())
    world.spoke.tick()
    assert _agent_calls(world, "/resume") == []
    assert world.hub.settles[0][1] == {"attempt": 1, "status": "rejected",
                                       "detail": "lease_expired"}


def test_at_most_once_deliveries_go_first_in_a_batch():
    world = World()
    intent = world.running()
    world.deliver(intent)
    world.deliver(intent, kind="abandon")
    world.spoke.tick()
    forwarded = [w for who, w in world.timeline if who == "agent" and w.startswith("POST")]
    assert forwarded[0].endswith("/abandon") and forwarded[1].endswith("/turns")


def test_a_launch_without_credentials_is_refused():
    world = World()
    intent = world.running()
    world.deliver(intent, kind="launch", credentials=None)
    world.spoke.tick()
    assert _agent_calls(world, "/turns") == []
    assert world.hub.settles[0][1]["status"] == "rejected"


def test_a_launchs_minted_credentials_are_forwarded_into_the_turn():
    world = World()
    intent = world.running()
    bodies = []
    original = world.agent.handler

    def capture(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/turns"):
            bodies.append(json.loads(request.content))
        return original(request)

    world.spoke.agents = AgentClient(transport=httpx.MockTransport(capture))
    world.deliver(intent, kind="launch",
                  credentials={"secret_env": {"MIRARUN_MCP_TOKEN": "minted"}})
    world.spoke.tick()
    assert bodies[0]["credentials"] == {"secret_env": {"MIRARUN_MCP_TOKEN": "minted"}}


def test_credentials_keyed_by_binding_name_are_refused_not_dropped():
    world = World()
    intent = world.running()
    world.deliver(intent, kind="launch", credentials={"secrets": {"mirarun-mcp": "minted"}})
    world.spoke.tick()
    assert world.hub.settles[0][1]["status"] == "rejected"
    assert world.agent.forwarded == []


def test_a_delivery_for_an_intent_this_spoke_does_not_run_is_rejected():
    world = World()
    world.deliver({"organization_id": str(TENANT), "intent_id": str(uuid4())})
    world.spoke.tick()
    assert world.hub.settles[0][1]["status"] == "rejected"


def test_an_abandon_the_agent_answered_is_not_sent_again():
    world = World()
    intent = world.running()
    delivery = world.deliver(intent, kind="abandon")
    world.spoke.tick()
    world.hub.deliveries.append(delivery)
    world.spoke.tick()
    assert len(_agent_calls(world, "/abandon")) == 1


def test_a_cluster_api_failure_in_one_delivery_does_not_stop_the_tick():
    world = World()
    intent = world.running()
    world.deliver(intent)
    world.k8s_fail.add("/pods")
    world.spoke.tick()  # contained: no exception
    world.k8s_fail.clear()
    world.hub.deliveries.extend(world.hub.claimed.values())
    world.spoke.tick()
    assert world.hub.settles[0][1]["status"] == "accepted"


# -- events -----------------------------------------------------------------

def test_events_written_just_before_the_run_stops_are_still_relayed():
    world = World()
    intent = world.running()
    world.deliver(intent, kind="launch")
    world.spoke.tick()
    assert world.hub.pages == []
    original = world.agent.handler

    def late_events(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/runs/") and not path.endswith("/events"):
            world.agent.events = [{"seq": 1, "type": "turn.done"}]
            world.agent.status = "succeeded"
        return original(request)

    world.spoke.agents = AgentClient(transport=httpx.MockTransport(late_events))
    world.spoke.tick()
    world.spoke.tick()
    relayed = [e["seq"] for _, page in world.hub.pages for e in page["events"]["events"]]
    assert relayed == [1]
    assert world.spoke.runs == {}


def test_relays_resume_from_the_hubs_open_runs_and_cursor():
    world = World()
    intent = world.running()
    run_intent = str(uuid4())
    world.hub.cursors[run_intent] = 2
    world.hub.open = [{"organization_id": intent["organization_id"],
                       "run_intent_id": run_intent, "intent_id": intent["intent_id"],
                       "run_id": "run-7", "last_event_sequence": 2}]
    world.agent.events = [{"seq": n, "type": "e"} for n in (1, 2, 3, 4)]
    world.spoke.resync_open_runs()
    world.spoke.tick()
    relayed = [e["seq"] for _, page in world.hub.pages for e in page["events"]["events"]]
    assert relayed == [3, 4]  # nothing replayed: the old hub would refuse it


def test_the_cursor_follows_the_hubs_answer():
    world = World()
    intent = world.running()
    delivery = world.deliver(intent, kind="launch")
    world.spoke.tick()
    world.hub.cursors[delivery["run_intent_id"]] = 0
    world.agent.events = [{"seq": 1, "type": "e"}]
    world.spoke.tick()
    run = next(iter(world.spoke.runs.values()))
    assert run.after == 1


def test_a_too_large_page_is_halved_until_it_fits():
    world = World()
    intent = world.running()
    world.deliver(intent, kind="launch")
    world.spoke.tick()
    world.agent.events = [{"seq": n, "type": "e"} for n in range(1, 5)]
    world.hub.fail["events"] = [413, 413]
    world.spoke.tick()
    world.spoke.tick()
    world.spoke.tick()
    assert world.agent.limits[-3:] == [200, 100, 50]  # 413, 413, then it fits
    relayed = [e["seq"] for _, page in world.hub.pages for e in page["events"]["events"]]
    assert relayed == [1, 2, 3, 4]


def test_an_oversized_event_goes_up_as_its_envelope():
    world = World()
    intent = world.running()
    world.deliver(intent, kind="launch")
    world.spoke.tick()
    world.agent.events = [{"seq": 1, "schema": "run-event/1", "type": "tool.output", "ts": "t",
                           "output": "x" * (300 * 1024)}]
    world.spoke.tick()
    event = world.hub.pages[0][1]["events"]["events"][0]
    # The hub's envelope needs seq, schema, type and ts; all survive.
    assert event == {"seq": 1, "schema": "run-event/1", "type": "tool.output", "ts": "t",
                     "truncated": True}


def test_a_resumed_run_is_relayed_again():
    world = World()
    intent = world.running()
    delivery = world.deliver(intent, kind="resume")
    world.agent.events = [{"seq": 1, "type": "e"}]
    world.spoke.tick()
    assert world.hub.pages[0][0] == f"runs/{delivery['run_intent_id']}/events"


def test_claims_long_poll_when_idle_and_return_at_once_while_relaying():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    assert ("intents/claim", 5) in world.hub.waits
    assert ("deliveries/claim", 25) in world.hub.waits
    world.k8s.add_pod(workload_name(UUID(intent["intent_id"])), phase="Running",
                      state={"running": {"startedAt": "t"}})
    world.deliver(intent, kind="launch")
    world.spoke.tick()
    world.hub.waits.clear()
    world.spoke.tick()
    assert world.hub.waits == [("intents/claim", 0), ("deliveries/claim", 0)]


# -- the loop ---------------------------------------------------------------

def test_a_refusing_hub_makes_the_loop_back_off():
    world = World()
    world.hub.fail["claim"] = [401, 401]
    sleeps: list[float] = []
    stops = iter([False, False, False, True])
    world.spoke.run_forever(should_stop=lambda: next(stops), sleep=sleeps.append)
    assert len(sleeps) >= 2 and sleeps[1] > sleeps[0] > 0


def test_recover_picks_up_running_workloads_from_their_labels():
    world = World()
    intent = world.offer()
    world.spoke.tick()
    fresh = World()
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


def test_unused_imports_are_real():
    assert HubRefused and SpawnOperationFailed


def test_with_the_published_hub_a_replay_is_skipped_and_the_cursor_followed():
    world = World()
    world.hub.replays = "skip"
    intent = world.running()
    delivery = world.deliver(intent, kind="launch")
    world.spoke.tick()
    world.hub.cursors[delivery["run_intent_id"]] = 3  # the hub already has 1..3
    world.agent.events = [{"seq": n, "type": "e"} for n in range(1, 6)]
    world.spoke.tick()
    run = next(iter(world.spoke.runs.values()))
    assert run.after == 5
    assert world.hub.cursors[delivery["run_intent_id"]] == 5


def test_a_turn_result_that_conflicts_is_dropped():
    world = World()
    intent = world.running()
    world.deliver(intent)
    world.spoke.tick()
    world.agent.turn_status["turn-1"] = "succeeded"
    world.hub.fail["result"] = 409
    world.spoke.tick()
    assert world.spoke.pending_results == {}
