"""The spoke's side of the hub's pull API (MiraRun ADR-045 §2).

Every call is the spoke's, outbound. The hub never calls a spoke. The
routes and bodies are MiraRun's `/api/spoke/v1/*` contract:

- `POST intents/claim` → `{"intents": [...]}`: intents addressed to this
  spoke whose current generation it has not acknowledged, each under a lease
  (`attempt`, `lease_expires_at`);
- `POST intents/{org}/{intent}/ack` `{generation, attempt}` → 204: the
  generation is applied. Only under the live lease and only for the leased
  generation; 409 means the lease is no longer this spoke's;
- `POST intents/{org}/{intent}/observations` → 204: what the cluster says
  (a claim, kept as it was said);
- `POST deliveries/claim` → `{"deliveries": [...]}`;
- `POST deliveries/{org}/{delivery}/settle` `{attempt, status, detail}`;
- `POST runs/{run_intent}/events` `{run, events}` → the hub's projection:
  the agent's own run record and one of its event pages, both unchanged.
  That route is the newest part of the contract, so it sits behind its own
  small interface (`EventSink`) and one constant.

Both claim routes long-poll: `wait_seconds` (at most 25) lets the hub hold
the request until work appears.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)

API_PREFIX = "/api/spoke/v1"
# The hub route that takes a run's event page. Kept in one place: the hub
# side lands separately and its name may still change.
RUN_EVENTS_ROUTE = API_PREFIX + "/runs/{run_intent_id}/events"
MAX_CLAIM_WAIT_SECONDS = 25


class HubUnavailable(Exception):
    """The hub could not be reached or answered unexpectedly; retry later."""


class HubRefused(Exception):
    """The hub refused this request (4xx). Retrying the same thing will not help."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code


class LeaseLost(HubRefused):
    """409 on an ack or a settle: this spoke no longer holds the lease."""


@dataclass(frozen=True)
class ClaimedIntent:
    organization_id: UUID
    intent_id: UUID
    product: str
    namespace: str
    workload_class: str
    principal: str
    generation: int
    desired_state: str
    instance_count: int
    profile_name: str
    profile_sha256: str
    profile_yaml: str
    grant_set: dict[str, Any]
    network_labels: dict[str, str]
    deadline_at: datetime | None
    trace_id: str | None
    attempt: int

    @classmethod
    def from_json(cls, item: dict[str, Any]) -> ClaimedIntent:
        deadline = item.get("deadline_at")
        return cls(
            organization_id=UUID(item["organization_id"]),
            intent_id=UUID(item["intent_id"]),
            product=item["product"],
            namespace=item["namespace"],
            workload_class=item["workload_class"],
            principal=item["principal"],
            generation=int(item["generation"]),
            desired_state=item["desired_state"],
            instance_count=int(item["instance_count"]),
            profile_name=item["profile_name"],
            profile_sha256=item["profile_sha256"],
            profile_yaml=item["profile_yaml"],
            grant_set=dict(item.get("grant_set") or {}),
            network_labels={str(k): str(v) for k, v in (item.get("network_labels") or {}).items()},
            deadline_at=datetime.fromisoformat(deadline) if deadline else None,
            trace_id=item.get("trace_id"),
            attempt=int(item["attempt"]),
        )


@dataclass(frozen=True)
class ClaimedDelivery:
    organization_id: UUID
    delivery_id: UUID
    intent_id: UUID
    kind: str
    idempotency_key: str
    at_most_once: bool
    payload: dict[str, Any]
    run_intent_id: UUID | None
    trace_id: str | None
    attempt: int
    # A launch's run-scoped credentials, minted by the hub for this claim
    # only. Forwarded to the agent's turn, never stored, never in repr.
    credentials: dict[str, Any] | None = field(default=None, repr=False)

    @classmethod
    def from_json(cls, item: dict[str, Any]) -> ClaimedDelivery:
        run_intent = item.get("run_intent_id")
        credentials = item.get("credentials")
        return cls(
            organization_id=UUID(item["organization_id"]),
            delivery_id=UUID(item["delivery_id"]),
            intent_id=UUID(item["intent_id"]),
            kind=item["kind"],
            idempotency_key=item["idempotency_key"],
            at_most_once=bool(item["at_most_once"]),
            payload=dict(item.get("payload") or {}),
            run_intent_id=UUID(run_intent) if run_intent else None,
            trace_id=item.get("trace_id"),
            attempt=int(item["attempt"]),
            credentials=dict(credentials) if isinstance(credentials, dict) else None,
        )


@dataclass(frozen=True)
class Observation:
    generation: int
    attempt: int
    phase: str
    reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    oom_killed: bool | None = None
    k8s_namespace: str | None = None
    pod_uid: str | None = None

    def to_json(self) -> dict[str, Any]:
        body = {k: v for k, v in self.__dict__.items() if v is not None}
        if self.reason is not None:
            body["reason"] = self.reason[:255]
        return body


class EventSink(Protocol):
    def push_events(
        self, run_intent_id: UUID, run: dict[str, Any], page: dict[str, Any]
    ) -> None:
        """Hand one event page of a run to the hub, with the run's record.
        Both are the agent's own answers, unchanged: `GET /runs/{id}` (the
        projection needs its status, timings and usage) and
        `GET /runs/{id}/events?after=`. (run_id, seq) deduplicates events, so
        pushing a page twice is harmless."""


class HubClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 70.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, body: dict[str, Any], *, lease: bool = False) -> httpx.Response:
        try:
            response = self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise HubUnavailable(type(exc).__name__) from exc
        if response.status_code == 409 and lease:
            raise LeaseLost(409, _detail(response))
        if 400 <= response.status_code < 500:
            raise HubRefused(response.status_code, _detail(response))
        if response.status_code >= 300:
            raise HubUnavailable(f"hub answered {response.status_code}")
        return response

    def claim_intents(self, limit: int, *, wait_seconds: int = 0) -> list[ClaimedIntent]:
        response = self._post(
            f"{API_PREFIX}/intents/claim", _claim_body(limit, wait_seconds)
        )
        return [ClaimedIntent.from_json(item) for item in response.json().get("intents") or []]

    def acknowledge(self, intent: ClaimedIntent) -> None:
        self._post(
            f"{API_PREFIX}/intents/{intent.organization_id}/{intent.intent_id}/ack",
            {"generation": intent.generation, "attempt": intent.attempt},
            lease=True,
        )

    def report(self, organization_id: UUID, intent_id: UUID, observation: Observation) -> None:
        self._post(
            f"{API_PREFIX}/intents/{organization_id}/{intent_id}/observations",
            observation.to_json(),
        )

    def claim_deliveries(self, limit: int, *, wait_seconds: int = 0) -> list[ClaimedDelivery]:
        response = self._post(
            f"{API_PREFIX}/deliveries/claim", _claim_body(limit, wait_seconds)
        )
        return [
            ClaimedDelivery.from_json(item) for item in response.json().get("deliveries") or []
        ]

    def settle(
        self,
        delivery: ClaimedDelivery,
        status: str,
        detail: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """`result` is the agent's answer to an accepted `launch`: the hub
        maps the queued run to the agent's run through its `run_id`."""
        body: dict[str, Any] = {"attempt": delivery.attempt, "status": status}
        if detail:
            body["detail"] = detail[:1024]
        if result is not None:
            body["result"] = result
        self._post(
            f"{API_PREFIX}/deliveries/{delivery.organization_id}/{delivery.delivery_id}/settle",
            body,
            lease=True,
        )

    def push_events(
        self, run_intent_id: UUID, run: dict[str, Any], page: dict[str, Any]
    ) -> None:
        self._post(
            RUN_EVENTS_ROUTE.format(run_intent_id=run_intent_id),
            {"run": run, "events": page},
        )


def _claim_body(limit: int, wait_seconds: int) -> dict[str, Any]:
    return {"limit": limit, "wait_seconds": max(0, min(wait_seconds, MAX_CLAIM_WAIT_SECONDS))}


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"][:200]
    return str(body)[:200]
