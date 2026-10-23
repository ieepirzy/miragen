"""The spoke's real HubClient against MiraRun's real spoke router (ADR-045).

Optional, and skipped unless `mirarun` is importable: set
`MIRARUN_SRC=<a mirarun checkout>` to add its `src/`. Run it in a Python that
has mirarun's dependencies (mirarun's own venv works):

    MIRARUN_SRC=../mirarun <mirarun-venv>/bin/python -m pytest -m hub_contract \
        tests/test_hub_contract.py

What is real: the router (routes, request and response models, bounded
bodies, bearer check) and the hub's own parsers for a pushed run record and
event page. The run record and events are produced by miragen's own
`RunStore` and event writer, so this checks miragen's output against the
hub's parser too. Only the hub's persistence is a stub, built from MiraRun's
domain types.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

if os.environ.get("MIRARUN_SRC"):
    sys.path.insert(0, os.path.join(os.environ["MIRARUN_SRC"], "src"))

spoke_api = pytest.importorskip("mirarun.api.spoke")
wd = pytest.importorskip("mirarun.domain.workloads")
aw = pytest.importorskip("mirarun.application.workloads")
wt = pytest.importorskip("mirarun.domain.workload_tokens")
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from miragen import events as run_events
from miragen.daemon.spoke.hub import (
    ClaimedDelivery,
    HubClient,
    HubRefused,
    Observation,
)
from miragen.runs import RunStore

pytestmark = pytest.mark.hub_contract

TOKEN = "mrsp_" + "c" * 43
NOW = datetime.now(UTC)


class StubService:
    """MiraRun's SpokePullService surface, with in-memory persistence."""

    def __init__(self) -> None:
        self.platform, self.tenant = uuid4(), uuid4()
        self.spoke = wd.Spoke(organization_id=self.platform, id=uuid4(), name="s1",
                              cluster="c1", created_by="op", created_at=NOW)
        self.intents: list = []
        self.deliveries: list = []
        self.calls: list[tuple[str, dict]] = []
        self.open: list = []
        self.mints = 0

    def authenticate(self, token: str):
        if token != TOKEN:
            raise wd.SpokeCredentialInvalid("no")
        return self.spoke

    def claim_intents(self, spoke, *, limit):
        out, self.intents = self.intents[:limit], self.intents[limit:]
        return out

    def acknowledge(self, spoke, **kwargs):
        self.calls.append(("ack", kwargs))

    def report(self, spoke, **kwargs):
        self.calls.append(("report", kwargs))

    def claim_deliveries_with_credentials(self, spoke, *, limit):
        out, self.deliveries = self.deliveries[:limit], self.deliveries[limit:]
        return out

    def settle(self, spoke, **kwargs):
        self.calls.append(("settle", kwargs))
        return SimpleNamespace(id=kwargs["delivery_id"], status=kwargs["status"])

    def record_result(self, spoke, *, delivery_id, result):
        # The hub's own checks on a turn record (application/workloads.py).
        validated = wd.validate_payload(result)
        if validated.get("status") not in aw._TURN_STATUSES:
            raise wd.WorkloadInvalid("not a finished turn's status")
        self.calls.append(("result", {"delivery_id": delivery_id, "result": validated}))
        return SimpleNamespace(id=delivery_id)

    def refresh_workload_token(self, spoke, *, organization_id, intent_id):
        self.mints += 1
        if self.mints > 1:
            raise wt.WorkloadTokenRateLimited("at most once a minute")
        return "MODEL_TOKEN", SimpleNamespace(
            token="minted-model-token", expires_at=NOW + timedelta(minutes=15))

    def open_runs(self, spoke):
        return list(self.open)

    def ingest_events(self, spoke, *, run_intent_id, backend_run, page):
        self.calls.append(("events", {"run": backend_run, "page": page}))
        last = max((e.sequence for e in page.events), default=page.next_after)
        return SimpleNamespace(run_intent_id=run_intent_id, status=backend_run.status,
                               last_event_sequence=last)

    # -- builders ---------------------------------------------------------
    def offer_intent(self):
        intent_id, profile_id = uuid4(), uuid4()
        intent = wd.WorkloadIntent(
            organization_id=self.tenant, id=intent_id, product="apogee",
            spoke_organization_id=self.platform, spoke_id=self.spoke.id,
            namespace="agents-sandbox", workload_class="ephemeral",
            principal="agent.profile.analyst", created_by="op", created_at=NOW)
        generation = wd.IntentGeneration(
            organization_id=self.tenant, intent_id=intent_id, generation=1,
            desired_state="running", instance_count=1,
            profile_organization_id=self.tenant, profile_revision_id=profile_id,
            grant_set={}, network_labels={}, requested_by="op", created_at=NOW,
            deadline_at=NOW + timedelta(hours=1))
        profile = wd.ProfileRevision(organization_id=self.tenant, id=profile_id,
                                     profile_name="analyst", sha256="b" * 64,
                                     yaml="name: analyst\n", created_by="op", created_at=NOW)
        self.intents.append(wd.ClaimedIntent(intent=intent, generation=generation,
                                             profile=profile, attempt=1,
                                             lease_expires_at=NOW + timedelta(minutes=1)))
        return intent

    def offer_launch(self, intent, model=None):
        delivery = wd.Delivery(
            organization_id=self.tenant, id=uuid4(), intent_id=intent.id,
            spoke_organization_id=self.platform, spoke_id=self.spoke.id, kind="launch",
            idempotency_key="launch-1", payload={"prompt": "go", "provenance": {},
                                                 "mirarun": {}},
            created_at=NOW, attempt=1, lease_expires_at=NOW + timedelta(minutes=1),
            run_intent_id=uuid4())
        self.deliveries.append((delivery, {
            "secret_env": {"MIRARUN_MCP_TOKEN": "minted"},
            "model": model or {"env": "MODEL_TOKEN", "token": "claim-model-token",
                               "expires_at": (NOW + timedelta(minutes=15)).isoformat()}}))
        return delivery


@pytest.fixture
def world():
    service = StubService()
    app = fastapi.FastAPI()
    app.include_router(spoke_api.spoke_router(service))
    hub = HubClient("http://testserver", TOKEN)
    hub._client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    return service, hub


def test_intent_claim_ack_and_report_round_trip(world):
    service, hub = world
    offered = service.offer_intent()
    claimed = hub.claim_intents(8, wait_seconds=0)
    assert [c.intent_id for c in claimed] == [offered.id]
    intent = claimed[0]
    assert intent.deadline_at is not None and intent.profile_yaml == "name: analyst\n"
    hub.acknowledge(intent)
    hub.report(intent.organization_id, intent.intent_id, Observation(
        generation=1, attempt=1, phase="running",
        started_at="2026-10-23T10:00:00+00:00", k8s_namespace="agents-sandbox",
        pod_uid="u-1"))
    kinds = [kind for kind, _ in service.calls]
    assert kinds == ["ack", "report"]
    assert service.calls[1][1]["phase"] == "running"


def test_a_launch_claim_settle_and_event_push_round_trip(world, tmp_path):
    service, hub = world
    intent = service.offer_intent()
    offered = service.offer_launch(intent)
    deliveries = hub.claim_deliveries(8, wait_seconds=0)
    delivery: ClaimedDelivery = deliveries[0]
    assert delivery.delivery_id == offered.id and delivery.lease_expires_at is not None
    assert delivery.credentials["secret_env"] == {"MIRARUN_MCP_TOKEN": "minted"}
    assert delivery.credentials["model"]["token"] == "claim-model-token"

    # miragen's own run record and event page, as the agent would serve them.
    store = RunStore(root=tmp_path / "runs")
    record = store.start(agent_name="analyst", trigger="launch", prompt="go",
                         instance="main", use_history=True)
    path = run_events.events_path(store.root, record.run_id)
    run_events.append_event(path, {"type": "turn.started"})
    run_events.append_event(path, {"type": "turn.completed"})
    page = run_events.read_events_page(path, after=0)
    page_body = {"run_id": record.run_id, "count": len(page.events), "events": page.events,
                 "next_after": page.next_after, "has_more": page.has_more}

    hub.settle(delivery, "accepted", f"turn {record.run_id}",
               {"run_id": record.run_id, "status": "running", "duplicate": False})
    settled = next(kw for kind, kw in service.calls if kind == "settle")
    assert settled["result"]["run_id"] == record.run_id

    cursor = hub.push_events(delivery.run_intent_id, record.model_dump(mode="json"), page_body)
    assert cursor == 2
    pushed = next(kw for kind, kw in service.calls if kind == "events")
    assert pushed["run"].run_id == record.run_id
    assert [e.sequence for e in pushed["page"].events] == [1, 2]


def test_a_wrong_token_is_refused(world):
    _, hub = world
    hub._client.headers["Authorization"] = "Bearer mrsp_" + "z" * 43
    with pytest.raises(HubRefused) as refused:
        hub.claim_intents(1)
    assert refused.value.status_code == 401


def test_a_finished_turn_record_is_accepted_by_the_hubs_checks(world, tmp_path):
    service, hub = world
    store = RunStore(root=tmp_path / "runs")
    record = store.start(agent_name="analyst", trigger="launch", prompt="hi",
                         instance="main", use_history=True)
    finished = store.finish(record, status="succeeded", output="the reply")
    delivery_id = uuid4()
    hub.post_turn_result(delivery_id, finished.model_dump(mode="json"))
    recorded = next(kw for kind, kw in service.calls if kind == "result")
    assert recorded["delivery_id"] == delivery_id
    assert recorded["result"]["output"] == "the reply"


def test_open_runs_are_read_with_their_cursors(world):
    service, hub = world
    intent = service.offer_intent()
    run_intent = uuid4()
    service.open.append(wd.OpenRun(organization_id=intent.organization_id,
                                   intent_id=intent.id, run_intent_id=run_intent,
                                   run_id="run-7", last_event_sequence=4))
    runs = hub.open_runs()
    assert [(r.run_intent_id, r.agent_run_id, r.last_event_sequence) for r in runs] == [
        (run_intent, "run-7", 4)]


def test_a_model_token_is_refreshed_and_a_second_mint_is_a_429(world):
    service, hub = world
    intent = service.offer_intent()
    refreshed = hub.refresh_model_token(intent.organization_id, intent.id)
    assert refreshed["env"] == "MODEL_TOKEN" and refreshed["token"] == "minted-model-token"
    assert datetime.fromisoformat(refreshed["expires_at"]) > NOW
    with pytest.raises(HubRefused) as refused:
        hub.refresh_model_token(intent.organization_id, intent.id)
    assert refused.value.status_code == 429


WORKLOAD_PRINCIPAL = re.compile(r"^agent\.[a-z0-9][a-z0-9-]*\.[a-z]+\.[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)?$")


def test_the_claimed_model_token_is_minted_by_the_hub_and_verified_by_luotsi(world, tmp_path):
    """Three repositories: MiraRun's real key store, minter and issuer mint
    the token; the spoke's HubClient claims it through MiraRun's real router;
    Luotsi's real verifier and policy accept it. Needs luotsi importable too."""
    luotsi_tokens = pytest.importorskip("luotsi.workload_tokens")
    luotsi_policy = pytest.importorskip("luotsi.policy")
    from mirarun.adapters.credentials.workload_tokens import (
        JwtWorkloadTokenMinter,
        WorkloadKeyStore,
    )
    from mirarun.application.workload_tokens import WorkloadTokenIssuer

    service, hub = world
    store = WorkloadKeyStore(tmp_path / "keys", tmp_path / "published.json")
    store.maintain()
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(store.jwks()))
    issuer = WorkloadTokenIssuer(
        JwtWorkloadTokenMinter(store, issuer="https://hub.test"),
        {service.tenant: "acme"}, token_env="ANTHROPIC_AUTH_TOKEN")
    intent = service.offer_intent()
    service.offer_launch(intent, model=issuer.credential(issuer.mint(intent)))

    delivery = hub.claim_deliveries(8, wait_seconds=0)[0]
    token = delivery.credentials["model"]["token"]
    claims = luotsi_tokens.JwksFileVerifier(jwks_path, issuer="https://hub.test").verify(token)
    assert claims.principal == "agent.acme.profile.analyst"
    assert WORKLOAD_PRINCIPAL.fullmatch(claims.principal)
    assert claims.tenant == "acme" and claims.workload == str(intent.id)
    policy = luotsi_policy.load_policy(
        "version: 1\n"
        "tenants: [{id: acme}]\n"
        "principals: [{id: agent.acme.profile.analyst, kind: workload, tenants: [acme]}]\n")
    assert policy.workload_principal(claims.principal) is not None
