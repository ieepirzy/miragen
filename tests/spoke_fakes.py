"""Strict fakes for the spoke's tests: a hub that validates every request
against mirror copies of MiraRun's request models (src/mirarun/api/spoke.py
on feat/workload-deliveries-wiring, 6a49d67) and enforces leases and event
cursors, plus a fake agent. tests/test_hub_contract.py checks the same
client against the real router."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

INT4_MAX = 2**31 - 1
INT8_MAX = 2**63 - 1
PHASE_PATTERN = r"^[a-z][a-z_]{0,31}$"
DELIVERY_SETTLED = ("accepted", "rejected", "uncertain")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


# -- MiraRun's request models, mirrored exactly -------------------------------

class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=32, ge=1, le=32)
    wait_seconds: int = Field(default=0, ge=0, le=25)


class AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: int = Field(ge=1, le=INT8_MAX)
    attempt: int = Field(ge=1, le=INT4_MAX)


class ObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: int = Field(ge=1, le=INT8_MAX)
    attempt: int = Field(ge=0, le=INT4_MAX)
    phase: str = Field(pattern=PHASE_PATTERN)
    reason: str | None = Field(default=None, max_length=255)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = Field(default=None, ge=-INT4_MAX - 1, le=INT4_MAX)
    oom_killed: bool | None = None
    k8s_namespace: str | None = Field(default=None, max_length=63)
    pod_uid: str | None = Field(default=None, max_length=64)


class SettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: int = Field(ge=1, le=INT4_MAX)
    status: str
    detail: str | None = Field(default=None, max_length=1024)
    result: dict[str, Any] | None = None


class PushedEventsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: dict[str, Any]
    events: dict[str, Any]


class TurnResultRequest(BaseModel):
    # The coordinator's decided shape for the new turn-result route.
    model_config = ConfigDict(extra="forbid")
    result: dict[str, Any]


class Timeline(list):
    """Every hub, API-server and agent call, in order."""


def _refuse(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail})


class StrictHub:
    def __init__(self, timeline: Timeline) -> None:
        self.timeline = timeline
        self.intents: list[dict] = []
        self.deliveries: list[dict] = []
        self.claimed: dict[str, dict] = {}       # delivery id -> the delivery as claimed
        self.settled: dict[str, dict] = {}       # delivery id -> settle body
        self.acks: list[tuple[str, dict]] = []
        self.reports: list[tuple[str, dict]] = []
        self.settles: list[tuple[str, dict]] = []
        self.pages: list[tuple[str, dict]] = []
        self.results: list[tuple[str, dict]] = []
        self.waits: list[tuple[str, int]] = []
        self.cursors: dict[str, int] = {}        # run intent -> last event sequence
        self.open: list[dict] = []               # what GET runs/open returns
        self.fail: dict[str, int | list[int]] = {}

    def _injected(self, kind: str) -> int | None:
        planned = self.fail.get(kind)
        if planned is None:
            return None
        if isinstance(planned, list):
            status = planned.pop(0)
            if not planned:
                self.fail.pop(kind)
            return status
        self.fail.pop(kind)
        return planned

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer mrsp_")
        path = urlsplit(str(request.url)).path.removeprefix("/api/spoke/v1/")
        parts = path.split("/")
        kind = parts[-1]
        if parts[0] == "runs" and kind == "events":
            kind = "events"
        if path == "runs/open":
            kind = "open"
        self.timeline.append(("hub", kind))
        injected = self._injected(kind)
        if injected is not None:
            return _refuse(injected, "injected")
        body = json.loads(request.content) if request.content else {}
        try:
            if path in ("intents/claim", "deliveries/claim"):
                claim = ClaimRequest.model_validate(body)
                self.waits.append((path, claim.wait_seconds))
                return self._claim(path, claim.limit)
            if kind == "ack":
                AcknowledgeRequest.model_validate(body)
                self.acks.append((path, body))
                return httpx.Response(204)
            if kind == "observations":
                ObservationRequest.model_validate(body)
                self.reports.append((path, body))
                return httpx.Response(204)
            if kind == "settle":
                return self._settle(parts[2], SettleRequest.model_validate(body), path, body)
            if kind == "events":
                return self._events(parts[1], PushedEventsRequest.model_validate(body), path)
            if kind == "result":
                TurnResultRequest.model_validate(body)
                if parts[1] not in self.settled:
                    return _refuse(404, "not found")
                self.results.append((path, body))
                return httpx.Response(204)
            if kind == "open":
                return httpx.Response(200, json={"runs": self.open})
        except ValidationError as exc:
            return _refuse(422, str(exc)[:200])
        raise AssertionError(f"unexpected hub route {path}")

    def _claim(self, path: str, limit: int) -> httpx.Response:
        if path == "intents/claim":
            out, self.intents = self.intents[:limit], self.intents[limit:]
            return httpx.Response(200, json={"intents": out})
        out, self.deliveries = self.deliveries[:limit], self.deliveries[limit:]
        for item in out:
            self.claimed[item["delivery_id"]] = item
        return httpx.Response(200, json={"deliveries": out})

    def _settle(self, delivery_id: str, body: SettleRequest, path: str, raw: dict):
        if body.status not in DELIVERY_SETTLED:
            return _refuse(422, "bad status")
        claimed = self.claimed.get(delivery_id)
        if claimed is None:
            return _refuse(404, "not found")
        if body.attempt != claimed["attempt"]:
            return _refuse(409, "the lease is no longer yours")
        if claimed["kind"] in ("turn", "answer") and body.result is not None:
            # Settled means dispatched: the reply comes on the result route.
            return _refuse(422, "a turn settle carries no result")
        if (claimed["kind"] == "launch" and body.status == "accepted"
                and not (body.result and body.result.get("run_id"))):
            return _refuse(422, "an accepted launch needs its result.run_id")
        previous = self.settled.get(delivery_id)
        if previous is not None and previous["status"] != body.status:
            return _refuse(409, f"already {previous['status']}")
        self.settled[delivery_id] = raw
        self.settles.append((path, raw))
        return httpx.Response(200, json={"delivery_id": delivery_id, "status": body.status})

    def _events(self, run_intent_id: str, body: PushedEventsRequest, path: str):
        page = body.events
        run = body.run
        if page.get("run_id") != run.get("run_id"):
            return _refuse(422, "page and record name different runs")
        events = page.get("events") or []
        if page.get("count") != len(events):
            return _refuse(422, "page count mismatch")
        cursor = self.cursors.get(run_intent_id, 0)
        seqs = [e["seq"] for e in events]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            return _refuse(422, "events out of order")
        if seqs and seqs[0] <= cursor:
            # The old hub's behaviour: a replayed page is refused.
            return _refuse(422, f"replayed events (cursor {cursor})")
        if seqs and seqs[0] != cursor + 1:
            return _refuse(422, f"gap after {cursor}")
        if seqs:
            self.cursors[run_intent_id] = seqs[-1]
        self.pages.append((path, {"run": run, "events": page}))
        return httpx.Response(200, json={
            "run_intent_id": run_intent_id, "status": run.get("status", "running"),
            "last_event_sequence": self.cursors.get(run_intent_id, 0)})


class FakeAgent:
    def __init__(self, timeline: Timeline) -> None:
        self.timeline = timeline
        self.turns: dict[str, str] = {}
        self.turn_status: dict[str, str] = {}
        self.forwarded: list[tuple[str, str]] = []
        self.events: list[dict] = []
        self.status = "running"
        self.raise_on: dict[str, type[Exception]] = {}
        self.limits: list[int] = []

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
            self.turn_status.setdefault(self.turns[key], "running")
            self.forwarded.append(("turn", key))
            return httpx.Response(200 if duplicate else 202, json={
                "turn_id": self.turns[key], "instance": path.split("/")[2],
                "status": "running", "duplicate": duplicate})
        if request.method == "GET" and "/turns/" in path:
            turn_id = path.rsplit("/", 1)[-1]
            if turn_id not in self.turn_status:
                return httpx.Response(404, json={"detail": "no such turn"})
            return httpx.Response(200, json={"run_id": turn_id,
                                             "status": self.turn_status[turn_id],
                                             "output": "the reply"})
        if request.method == "DELETE" and path.startswith("/instances/"):
            self.forwarded.append(("discard", path))
            return httpx.Response(200, json={})
        if request.method == "POST" and path.endswith(("/resume", "/abandon")):
            run_id = path.split("/")[2]
            self.forwarded.append((path.rsplit("/", 1)[-1], run_id))
            return httpx.Response(200, json={"run_id": run_id, "status": "running"})
        if request.method == "GET" and path.endswith("/events"):
            after = int(request.url.params["after"])
            limit = int(request.url.params["limit"])
            self.limits.append(limit)
            pending = [e for e in self.events if e["seq"] > after]
            events = pending[:limit]
            return httpx.Response(200, json={
                "run_id": path.split("/")[2], "count": len(events), "events": events,
                "next_after": events[-1]["seq"] if events else after,
                "has_more": len(pending) > limit})
        if request.method == "GET" and path.startswith("/runs/"):
            return httpx.Response(200, json={"run_id": path.split("/")[2], "status": self.status})
        raise AssertionError(path)


def offered_intent(tenant: UUID, namespace: str, **changes) -> dict:
    intent = {
        "organization_id": str(tenant), "intent_id": str(uuid4()), "product": "apogee",
        "namespace": namespace, "workload_class": "ephemeral",
        "principal": "agent.profile.analyst",
        "generation": 1, "desired_state": "running", "instance_count": 1,
        "profile_name": "analyst", "profile_sha256": "b" * 64,
        "profile_yaml": "name: analyst\n", "grant_set": {}, "network_labels": {},
        "deadline_at": (NOW + timedelta(hours=1)).isoformat(), "trace_id": None,
        "attempt": 1, "lease_expires_at": (NOW + timedelta(minutes=1)).isoformat(),
    }
    intent.update(changes)
    return intent


def offered_delivery(intent: dict, *, kind: str = "turn", **changes) -> dict:
    """As the hub builds it: only a launch, resume or abandon belongs to a run
    intent, and only a launch carries credentials."""
    run_bound = kind in ("launch", "resume", "abandon")
    delivery = {
        "organization_id": intent["organization_id"], "delivery_id": str(uuid4()),
        "intent_id": intent["intent_id"], "kind": kind,
        "idempotency_key": f"{kind}-{uuid4().hex[:8]}",
        "at_most_once": kind in ("resume", "abandon"),
        "payload": {"instance": "main", "prompt": "analyse it", "provenance": {},
                    "mirarun": {}},
        "run_intent_id": str(uuid4()) if run_bound else None, "trace_id": None,
        "attempt": 1, "lease_expires_at": (NOW + timedelta(minutes=1)).isoformat(),
        "credentials": {"secret_env": {}} if kind == "launch" else None,
    }
    if kind in ("resume", "abandon"):
        delivery["payload"] = {"run_id": "run-9", "prompt": "go"} if kind == "resume" \
            else {"run_id": "run-9", "discard_workspace": False}
    delivery.update(changes)
    return delivery
