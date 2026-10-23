"""Strict fakes for the spoke's tests: a hub that validates every request
body and every response it sends against MiraRun's published spoke contract
(tests/contracts/spoke-api.openapi.json, copied from mirarun
docs/contracts/spoke-api/openapi.json at feat/workload-deliveries-wiring
42d015d), and enforces the hub's leases, write-once turn results and event
cursors; plus a fake agent. tests/test_hub_contract.py runs the same client
against the real router."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import jsonschema

SPEC = json.loads((Path(__file__).parent / "contracts" / "spoke-api.openapi.json").read_text())
DELIVERY_SETTLED = ("accepted", "rejected", "uncertain")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _operation(method: str, path: str) -> dict:
    for template, operations in SPEC["paths"].items():
        pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$"
        if re.match(pattern, path) and method.lower() in operations:
            return operations[method.lower()]
    raise AssertionError(f"{method} {path} is not in the published contract")


def _validate(schema: dict, instance: Any, what: str) -> None:
    root = {**schema, "components": SPEC["components"]}
    try:
        jsonschema.Draft202012Validator(root).validate(instance)
    except jsonschema.ValidationError as exc:
        raise ContractViolation(f"{what}: {exc.message}") from exc


class ContractViolation(Exception):
    pass


def validate_request(method: str, path: str, body: Any) -> None:
    operation = _operation(method, path)
    request_body = operation.get("requestBody")
    if request_body is None:
        assert body in (None, {}), f"{method} {path} takes no body"
        return
    _validate(request_body["content"]["application/json"]["schema"], body, f"{path} request")


def validate_response(method: str, path: str, status: int, body: Any) -> None:
    operation = _operation(method, path)
    response = operation["responses"].get(str(status))
    if response is None or status >= 400:
        return  # errors carry `detail` only
    content = (response.get("content") or {}).get("application/json")
    if content and content.get("schema"):
        _validate(content["schema"], body, f"{path} {status} response")


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
        self.turn_results: dict[str, dict] = {}
        # "reject": refuse a replayed page (stricter); "skip": the published hub.
        self.replays = "reject"

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
        full = urlsplit(str(request.url)).path
        path = full.removeprefix("/api/spoke/v1/")
        parts = path.split("/")
        kind = "open" if path == "runs/open" else (
            "events" if parts[0] == "runs" else parts[-1])
        self.timeline.append(("hub", kind))
        injected = self._injected(kind)
        if injected is not None:
            return _refuse(injected, "injected")
        body = json.loads(request.content) if request.content else None
        try:
            validate_request(request.method, full, body)
        except ContractViolation as exc:
            return _refuse(422, str(exc)[:300])
        response = self._route(path, parts, kind, body or {})
        payload = json.loads(response.content) if response.content else None
        validate_response(request.method, full, response.status_code, payload)
        return response

    def _route(self, path: str, parts: list[str], kind: str, body: dict) -> httpx.Response:
        if path in ("intents/claim", "deliveries/claim"):
            self.waits.append((path, body.get("wait_seconds", 0)))
            return self._claim(path, body.get("limit", 32))
        if kind == "ack":
            self.acks.append((path, body))
            return httpx.Response(204)
        if kind == "observations":
            self.reports.append((path, body))
            return httpx.Response(204)
        if kind == "settle":
            return self._settle(parts[2], body, path)
        if kind == "events":
            return self._events(parts[1], body, path)
        if kind == "result":
            return self._result(parts[1], body, path)
        if kind == "open":
            return httpx.Response(200, json={"runs": self.open})
        raise AssertionError(f"unexpected hub route {path}")

    def _claim(self, path: str, limit: int) -> httpx.Response:
        if path == "intents/claim":
            out, self.intents = self.intents[:limit], self.intents[limit:]
            return httpx.Response(200, json={"intents": out})
        out, self.deliveries = self.deliveries[:limit], self.deliveries[limit:]
        for item in out:
            self.claimed[item["delivery_id"]] = item
        return httpx.Response(200, json={"deliveries": out})

    def _settle(self, delivery_id: str, body: dict, path: str) -> httpx.Response:
        status = body["status"]
        if status not in DELIVERY_SETTLED:
            return _refuse(422, "bad status")
        claimed = self.claimed.get(delivery_id)
        if claimed is None:
            return _refuse(404, "not found")
        if body["attempt"] != claimed["attempt"]:
            return _refuse(409, "the lease is no longer yours")
        result = body.get("result")
        if claimed["kind"] != "launch" and result is not None:
            # Settled means dispatched: only an accepted launch carries a result.
            return _refuse(422, "only an accepted launch carries a result")
        if claimed["kind"] == "launch" and status == "accepted" and not (
                result and result.get("run_id")):
            return _refuse(422, "an accepted launch needs its result.run_id")
        previous = self.settled.get(delivery_id)
        if previous is not None and previous["status"] != status:
            return _refuse(409, f"already {previous['status']}")
        self.settled[delivery_id] = body
        self.settles.append((path, body))
        return httpx.Response(200, json={"delivery_id": delivery_id, "status": status})

    def _result(self, delivery_id: str, body: dict, path: str) -> httpx.Response:
        settled = self.settled.get(delivery_id)
        if settled is None or settled["status"] != "accepted":
            return _refuse(404, "not found")
        previous = self.turn_results.get(delivery_id)
        if previous is not None and previous != body["result"]:
            return _refuse(409, "a different reply is already recorded")
        self.turn_results[delivery_id] = body["result"]
        self.results.append((path, body))
        return httpx.Response(200, json={"delivery_id": delivery_id, "recorded": True})

    def _events(self, run_intent_id: str, body: dict, path: str) -> httpx.Response:
        page, run = body["events"], body["run"]
        if page.get("run_id") != run.get("run_id"):
            return _refuse(422, "page and record name different runs")
        events = page.get("events") or []
        if page.get("count") != len(events):
            return _refuse(422, "page count mismatch")
        cursor = self.cursors.get(run_intent_id, 0)
        seqs = [e["seq"] for e in events]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            return _refuse(422, "events out of order")
        if self.replays == "reject" and seqs and seqs[0] <= cursor:
            # Stricter than the published hub, which skips them: a spoke that
            # follows the cursor never replays, so it passes either way.
            return _refuse(422, f"replayed events (cursor {cursor})")
        fresh = [s for s in seqs if s > cursor]
        if fresh and fresh[0] != cursor + 1:
            return _refuse(422, f"gap after {cursor}")
        if fresh:
            self.cursors[run_intent_id] = fresh[-1]
        self.pages.append((path, body))
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
