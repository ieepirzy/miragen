"""The spoke's pull loop (MiraRun ADR-045 §1, decisions 1 and 4).

One tick:

1. **Intents.** Claim what the hub addresses to this spoke. Check each one
   against the local allowlist (`SpokeConfig.allows`): a refused intent is
   reported `refused` and acknowledged, so it is not offered again, and is
   never applied. Otherwise render it from the spoke's own template, apply
   it, then acknowledge the generation, then report what the cluster says.
   Applying is idempotent, so a lease offered again is safe.
2. **Watch.** Report each tracked workload's phase when it changes. An
   ephemeral workload that has ended is deleted only after the hub has taken
   its final report (204) and its runs' events have been relayed.
3. **Deliveries.** Forward each claimed delivery to its agent in-cluster
   and settle it:
   - `launch`, `turn`, `answer` are turns, idempotent by the delivery's
     key. A failure where nothing or anything may have arrived is left
     unsettled, so the hub offers it again and the agent deduplicates.
     `discard` is idempotent by nature;
   - `resume`, `abandon` are forwarded at most once. If the outcome is
     unknown, they are settled `uncertain` and never sent again. If nothing
     was sent (the connection never opened), they are settled `rejected`.
4. **Events.** Relay each tracked run's event pages, read from the agent
   by cursor, to the hub with the run's record (`EventSink`). Once a run
   has stopped and nothing is left to read, its final record goes up once
   more, so the hub sees the terminal status even with no new event.

Claims long-poll. With nothing to relay, the delivery claim waits up to
`poll.wait_seconds` for work, and the intent claim waits a fifth of that,
so an idle spoke costs the hub one held request at a time. While runs are
being relayed, both claims return at once and the loop paces itself at
`poll.interval_seconds`.

Settling a delivery means "dispatched", never "finished". A `turn` or
`answer` is settled `accepted` once the agent has taken it. When the turn
finishes, the spoke posts its record to the hub's turn-result route. Those
pending results, and every settle and ack the hub did not answer, are kept
across ticks and retried. They live in memory: a spoke restart loses them,
and the hub's leases take over.

Failures are contained:
- one bad intent, delivery or run is logged and skipped;
- a hub that refuses or is unreachable, or a Kubernetes API that fails,
  makes the whole loop back off, with capped exponential backoff, rather
  than spin or die.

Nothing here is pushed by the hub. Every call is the spoke's.
"""

from __future__ import annotations

import json
import logging
import random
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from miragen.daemon.spawn.base import SpawnOperationFailed
from miragen.daemon.spawn.kubernetes_workloads import KubernetesWorkloadDriver, Workload
from miragen.daemon.spoke.agents import (
    STOPPED_RUN_STATUSES,
    TURN_KEY_MAX,
    AgentClient,
    AgentOutcomeUnknown,
    AgentRejected,
    AgentUnreachable,
)
from miragen.daemon.spoke.config import SpokeConfig
from miragen.daemon.spoke.credentials import ModelCredentialSource, NoModelCredentials
from miragen.daemon.spoke.hub import (
    ClaimedDelivery,
    ClaimedIntent,
    EventSink,
    HubClient,
    HubRefused,
    HubUnavailable,
    LeaseLost,
    Observation,
    OpenRun,
)

logger = logging.getLogger(__name__)

INTENT_LABEL = "miragen.io/intent"
ORGANIZATION_LABEL = "miragen.io/organization"
PRODUCT_LABEL = "miragen.io/product"
INTENT_ANNOTATION = "miragen.io/intent-id"
ORGANIZATION_ANNOTATION = "miragen.io/organization-id"
ATTEMPT_ANNOTATION = "miragen.io/lease-attempt"
PRINCIPAL_ANNOTATION = "miragen.io/principal"

_INSTANCE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")  # INSTANCE_NAME_PATTERN
_LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
_TURN_KINDS = ("launch", "turn", "answer")
_MAX_PAGES_PER_TICK = 10
# At-most-once deliveries this spoke has already sent, kept so that one is
# never sent twice even if offered again (the hub does not re-offer them;
# this is the spoke's own guard).
_SENT_ONCE_MEMORY = 10_000
_DEFAULT_PAGE_LIMIT = 200
# One event bigger than this is relayed as its envelope only, marked
# `truncated`: the hub caps an event push at 1 MiB.
_MAX_EVENT_BYTES = 256 * 1024
_ENVELOPE_KEYS = ("seq", "schema_version", "type", "ts", "target", "run_id")
# Hub refusals that concern one item (a delivery, a run, an intent), not the
# spoke as a whole: that item is dropped or skipped, the loop goes on.
_ITEM_REFUSALS = frozenset({404, 409, 422})


def workload_name(intent_id: UUID) -> str:
    """Deterministic, so a restarted spoke finds its objects again."""
    return "mw-" + intent_id.hex


@dataclass
class TrackedWorkload:
    organization_id: UUID
    intent_id: UUID
    name: str
    namespace: str
    workload_class: str
    generation: int
    attempt: int
    last_phase: str | None = None


@dataclass
class TrackedRun:
    run_intent_id: UUID
    intent_id: UUID
    agent_run_id: str
    after: int = 0
    # Halved on a 413 from the hub, down to one event per page.
    page_limit: int = _DEFAULT_PAGE_LIMIT


@dataclass
class PendingSettle:
    """A delivery already handled whose settle the hub did not answer: only
    the settle is retried, never the forward."""

    delivery: ClaimedDelivery
    status: str
    detail: str | None
    result: dict[str, Any] | None = None


@dataclass
class PendingTurnResult:
    """A dispatched `turn` or `answer` whose record goes to the hub once the
    turn has finished."""

    delivery_id: UUID
    intent_id: UUID
    instance: str
    turn_id: str


@dataclass(frozen=True)
class _Forwarded:
    detail: str | None
    result: dict[str, Any] | None = None
    turn: tuple[str, str] | None = None  # (instance, turn id) of a turn or answer


@dataclass
class Spoke:
    config: SpokeConfig
    hub: HubClient
    driver: KubernetesWorkloadDriver
    agents: AgentClient
    events: EventSink | None = None
    credentials: ModelCredentialSource = field(default_factory=NoModelCredentials)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    new_token: Callable[[], str] = lambda: secrets.token_urlsafe(32)
    workloads: dict[UUID, TrackedWorkload] = field(default_factory=dict)
    runs: dict[str, TrackedRun] = field(default_factory=dict)
    pending_settles: dict[UUID, PendingSettle] = field(default_factory=dict)
    pending_results: dict[UUID, PendingTurnResult] = field(default_factory=dict)
    pending_acks: dict[UUID, ClaimedIntent] = field(default_factory=dict)
    monotonic: Callable[[], float] = time.monotonic
    _next_resync: float = 0.0
    _open_runs_supported: bool = True
    sent_once: OrderedDict[UUID, tuple[str, str | None]] = field(default_factory=OrderedDict)

    def __post_init__(self) -> None:
        if self.events is None:
            self.events = self.hub

    # -- start-up ---------------------------------------------------------------

    def recover(self) -> None:
        """Pick up the workloads this spoke already runs, from their labels
        and annotations. Event cursors are not recovered (see the design
        note): a run's relay restarts only when the hub delivers to it again."""
        for namespace in self.config.namespaces:
            for item in self.driver.list_managed(namespace):
                try:
                    intent_id = UUID(item.annotations[INTENT_ANNOTATION])
                    organization_id = UUID(item.annotations[ORGANIZATION_ANNOTATION])
                    generation = int(item.annotations["miragen.io/generation"])
                    attempt = int(item.annotations.get(ATTEMPT_ANNOTATION, "0"))
                except (KeyError, ValueError):
                    logger.warning("skipping %s/%s: not a spoke workload", namespace, item.name)
                    continue
                self.workloads[intent_id] = TrackedWorkload(
                    organization_id=organization_id,
                    intent_id=intent_id,
                    name=item.name,
                    namespace=namespace,
                    workload_class=item.workload_class,
                    generation=generation,
                    attempt=attempt,
                )
        self.resync_open_runs()

    def resync_open_runs(self) -> None:
        """Resume relaying the runs the hub still expects from this spoke,
        each from the hub's own cursor. Runs at startup, then every
        `poll.resync_seconds`."""
        self._next_resync = self.monotonic() + self.config.resync_seconds
        if not self._open_runs_supported:
            return
        try:
            open_runs = self.hub.open_runs()
        except HubRefused as exc:
            if exc.status_code == 404:
                self._open_runs_supported = False
                logger.warning("the hub has no open-runs route; relays resume only on delivery")
                return
            raise
        for open_run in open_runs:
            self._adopt_run(open_run)

    def _adopt_run(self, open_run: OpenRun) -> None:
        key = f"{open_run.intent_id}/{open_run.agent_run_id}"
        run = self.runs.get(key)
        if run is None:
            self.runs[key] = TrackedRun(
                run_intent_id=open_run.run_intent_id,
                intent_id=open_run.intent_id,
                agent_run_id=open_run.agent_run_id,
                after=open_run.last_event_sequence,
            )
        else:
            run.after = open_run.last_event_sequence

    # -- one tick ---------------------------------------------------------------

    def tick(self) -> bool:
        """One pass. True when anything was claimed, so the caller polls
        again at once instead of sleeping."""
        if self.monotonic() >= self._next_resync:
            self.resync_open_runs()
        busy = False
        relaying = bool(self.runs or self.pending_settles or self.pending_results
                        or self.pending_acks)
        wait = 0 if relaying else self.config.wait_seconds
        for intent in self.hub.claim_intents(self.config.claim_limit, wait_seconds=wait // 5):
            busy = True
            self._contained("intent", intent.intent_id, self.handle_intent, intent)
        self.retry_acks()
        self.watch()
        self.retry_settles()
        deliveries = self.hub.claim_deliveries(self.config.claim_limit, wait_seconds=wait)
        # At-most-once ones first: their leases are the ones that must not run out.
        for delivery in sorted(deliveries, key=lambda d: not d.at_most_once):
            busy = True
            self._contained("delivery", delivery.delivery_id, self.handle_delivery, delivery)
        self.pump_events()
        self.pump_turn_results()
        return busy

    def _contained(self, what: str, item: object, handler: Callable[[Any], None],
                   value: Any) -> None:
        """One item's trouble never stops the others. A refusal about the
        spoke itself (401, 403, 413, 429...) is not one item's, so it ends
        the tick and the loop backs off."""
        try:
            handler(value)
        except HubRefused as exc:
            if exc.status_code not in _ITEM_REFUSALS:
                raise
            logger.error("%s %s: %s", what, item, exc)
        except SpawnOperationFailed as exc:
            logger.error("%s %s: the cluster API failed: %s", what, item, exc)

    def run_forever(self, *, should_stop: Callable[[], bool] = lambda: False,
                    sleep: Callable[[float], None] = time.sleep) -> None:
        failures = 0
        recovered = False
        while not should_stop():
            try:
                if not recovered:
                    self.recover()
                    recovered = True
                busy = self.tick()
                failures = 0
            except (HubUnavailable, HubRefused, SpawnOperationFailed) as exc:
                failures += 1
                delay = self.backoff(failures)
                logger.warning("%s (%s); retrying in %.0fs", type(exc).__name__, exc, delay)
                sleep(delay)
                continue
            if not busy and (self.runs or self.config.wait_seconds == 0):
                # A long-poll already waited; only pace a loop that did not.
                sleep(self.config.interval_seconds)

    def backoff(self, failures: int) -> float:
        delay = min(self.config.max_backoff_seconds,
                    self.config.interval_seconds * (2 ** min(failures, 10)))
        return delay * random.uniform(0.8, 1.2)

    # -- intents ----------------------------------------------------------------

    def _observe(self, intent: ClaimedIntent | TrackedWorkload, observation: Observation) -> None:
        self.hub.report(intent.organization_id, intent.intent_id, observation)

    def _refuse(self, intent: ClaimedIntent, reason: str) -> None:
        logger.warning("refusing intent %s: %s", intent.intent_id, reason)
        self._observe(intent, Observation(intent.generation, intent.attempt, "refused", reason))
        self._ack(intent)

    def _ack(self, intent: ClaimedIntent) -> bool:
        """Acknowledge, or keep it to retry: the hub's ack is idempotent, and
        a lost 204 must not strand a workload that is already applied."""
        try:
            self.hub.acknowledge(intent)
            self.pending_acks.pop(intent.intent_id, None)
            return True
        except LeaseLost:
            # A newer generation took the lease; the hub offers that one next.
            self.pending_acks.pop(intent.intent_id, None)
            logger.info("lease lost on intent %s generation %s", intent.intent_id,
                        intent.generation)
            return False
        except HubUnavailable:
            self.pending_acks[intent.intent_id] = intent
            raise

    def retry_acks(self) -> None:
        for intent in list(self.pending_acks.values()):
            try:
                self._ack(intent)
            except HubRefused as exc:
                if exc.status_code not in _ITEM_REFUSALS:
                    raise
                self.pending_acks.pop(intent.intent_id, None)

    def _labels(self, intent: ClaimedIntent) -> dict[str, str] | str:
        labels = {
            INTENT_LABEL: intent.intent_id.hex,
            ORGANIZATION_LABEL: intent.organization_id.hex,
        }
        if _LABEL_VALUE.fullmatch(intent.product) and len(intent.product) <= 63:
            labels[PRODUCT_LABEL] = intent.product
        for key, value in intent.network_labels.items():
            if key not in self.config.network_label_keys:
                return f"network label '{key}' is not allowed by this spoke"
            if not _LABEL_VALUE.fullmatch(value) or len(value) > 63:
                return f"network label '{key}' has an invalid value"
            labels[key] = value
        return labels

    def handle_intent(self, intent: ClaimedIntent) -> None:
        reason = self.config.allows(intent.namespace, intent.workload_class,
                                    intent.organization_id)
        if reason is not None:
            self._refuse(intent, reason)
            return
        if intent.workload_class == "persistent" and intent.instance_count > 1:
            self._refuse(intent, "a persistent workload runs at most one instance")
            return
        labels = self._labels(intent)
        if isinstance(labels, str):
            self._refuse(intent, labels)
            return
        name = workload_name(intent.intent_id)

        if intent.desired_state == "deleted":
            self._drain_runs(intent.intent_id)
            self.driver.delete(intent.namespace, name, intent.workload_class, volumes=True)
            if self._ack(intent):
                self._observe(intent, Observation(intent.generation, intent.attempt, "deleted",
                                                  k8s_namespace=intent.namespace))
            self.workloads.pop(intent.intent_id, None)
            return

        deadline_seconds = None
        if intent.deadline_at is not None:
            deadline_seconds = int((intent.deadline_at - self.clock()).total_seconds())
            if deadline_seconds < 1:
                self._refuse(intent, "the intent's deadline has already passed")
                return
        running = intent.desired_state == "running" and intent.instance_count > 0
        if not running and intent.workload_class == "ephemeral":
            self._drain_runs(intent.intent_id)
        token = (
            self.driver.internal_token(intent.namespace, name, intent.workload_class)
            or self.new_token()
        )
        workload = Workload(
            name=name,
            namespace=intent.namespace,
            workload_class=intent.workload_class,
            template=self.config.classes[intent.workload_class],
            profile_yaml=intent.profile_yaml,
            generation=intent.generation,
            running=running,
            internal_token=token,
            labels=labels,
            annotations={
                INTENT_ANNOTATION: str(intent.intent_id),
                ORGANIZATION_ANNOTATION: str(intent.organization_id),
                ATTEMPT_ANNOTATION: str(intent.attempt),
                PRINCIPAL_ANNOTATION: intent.principal,
            },
            env={
                **self.credentials.environment(intent),
                "MIRARUN_INTENT_ID": str(intent.intent_id),
                "MIRARUN_ORGANIZATION_ID": str(intent.organization_id),
                "MIRARUN_PRINCIPAL": intent.principal,
            },
            deadline_seconds=deadline_seconds if intent.workload_class == "ephemeral" else None,
        )
        try:
            self.driver.apply(workload)
        except SpawnOperationFailed as exc:
            # Not acknowledged: the hub offers this generation again when the
            # lease runs out. The report says why it has not happened yet.
            logger.error("applying intent %s failed: %s", intent.intent_id, exc)
            self._observe(intent, Observation(intent.generation, intent.attempt,
                                              "apply_failed", str(exc)[:255],
                                              k8s_namespace=intent.namespace))
            return
        tracked = TrackedWorkload(
            organization_id=intent.organization_id,
            intent_id=intent.intent_id,
            name=name,
            namespace=intent.namespace,
            workload_class=intent.workload_class,
            generation=intent.generation,
            attempt=intent.attempt,
        )
        if not running and intent.workload_class == "ephemeral":
            self.workloads.pop(intent.intent_id, None)
            if self._ack(intent):
                self._observe(intent, Observation(intent.generation, intent.attempt, "stopped",
                                                  k8s_namespace=intent.namespace))
            return
        # Tracked before the ack: if the ack's answer is lost, the workload is
        # still watched, and the ack is retried.
        self.workloads[intent.intent_id] = tracked
        if not self._ack(intent):
            return
        self._report_status(tracked)

    # -- watching ---------------------------------------------------------------

    def _report_status(self, tracked: TrackedWorkload) -> bool:
        status = self.driver.status(tracked.namespace, tracked.name, tracked.workload_class)
        if status.phase == tracked.last_phase:
            return status.terminal
        self._observe(tracked, Observation(
            generation=tracked.generation,
            attempt=tracked.attempt,
            phase=status.phase,
            reason=status.reason,
            started_at=_iso(status.started_at),
            finished_at=_iso(status.finished_at),
            exit_code=status.exit_code,
            oom_killed=status.oom_killed,
            k8s_namespace=tracked.namespace,
            pod_uid=status.pod_uid,
        ))
        tracked.last_phase = status.phase
        return status.terminal

    def watch(self) -> None:
        for intent_id, tracked in list(self.workloads.items()):
            try:
                ended = self._report_status(tracked)
            except HubUnavailable:
                raise
            except HubRefused as exc:
                if exc.status_code not in _ITEM_REFUSALS:
                    raise
                logger.warning("watching %s failed: %s", tracked.name, exc)
                continue
            except SpawnOperationFailed as exc:
                logger.warning("watching %s failed: %s", tracked.name, exc)
                continue
            if tracked.last_phase == "absent":
                self.workloads.pop(intent_id, None)
            elif ended and tracked.workload_class == "ephemeral":
                # The hub has the final report (the call above returned), so
                # the Job can go. Its backstop TTL covers a spoke that dies
                # between the two.
                self._drain_runs(intent_id)
                try:
                    self.driver.delete(tracked.namespace, tracked.name, "ephemeral")
                except SpawnOperationFailed as exc:
                    logger.warning("deleting %s failed, retrying next tick: %s", tracked.name, exc)
                    continue
                self.workloads.pop(intent_id, None)

    # -- deliveries -------------------------------------------------------------

    def _settle(
        self,
        delivery: ClaimedDelivery,
        status: str,
        detail: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Settle, or keep it (with its result) to retry on the next tick. A
        hub that says the delivery is gone (404), or the settle invalid (422),
        or the lease no longer this spoke's (409), ends it."""
        try:
            self.hub.settle(delivery, status, detail, result)
            self.pending_settles.pop(delivery.delivery_id, None)
        except LeaseLost:
            self.pending_settles.pop(delivery.delivery_id, None)
            logger.info("delivery %s: lease lost before settling", delivery.delivery_id)
        except HubRefused as exc:
            if exc.status_code not in _ITEM_REFUSALS:
                self.pending_settles[delivery.delivery_id] = PendingSettle(
                    delivery, status, detail, result)
                raise
            self.pending_settles.pop(delivery.delivery_id, None)
            logger.warning("delivery %s: the hub refused the settle: %s",
                           delivery.delivery_id, exc)
        except HubUnavailable:
            self.pending_settles[delivery.delivery_id] = PendingSettle(
                delivery, status, detail, result)
            raise

    def retry_settles(self) -> None:
        for pending in list(self.pending_settles.values()):
            self._settle(pending.delivery, pending.status, pending.detail, pending.result)

    def handle_delivery(self, delivery: ClaimedDelivery) -> None:
        pending = self.pending_settles.get(delivery.delivery_id)
        if pending is not None:
            # Offered again while its settle was outstanding: settle it now,
            # under the new lease, without forwarding it again.
            pending.delivery = delivery
            self._settle(delivery, pending.status, pending.detail, pending.result)
            return
        if delivery.delivery_id in self.sent_once:
            # Already sent once: settle it as it was settled, never send again.
            status, detail = self.sent_once[delivery.delivery_id]
            self._settle(delivery, status, detail)
            return
        if (
            delivery.at_most_once
            and delivery.lease_expires_at is not None
            and delivery.lease_expires_at <= self.clock()
        ):
            # Its lease is gone: the hub may already count it uncertain.
            self._settle(delivery, "rejected", "lease_expired")
            return
        if delivery.kind == "launch" and delivery.credentials is None:
            self._settle(delivery, "rejected",
                         "refused: a launch is never forwarded without its credentials")
            return
        tracked = self.workloads.get(delivery.intent_id)
        if tracked is None:
            self._settle(delivery, "rejected", "no workload for this intent on this spoke")
            return
        endpoint = self.driver.endpoint(tracked.namespace, tracked.name)
        token = self.driver.internal_token(tracked.namespace, tracked.name,
                                           tracked.workload_class)
        if endpoint is None or token is None:
            if delivery.at_most_once:
                self._settle(delivery, "rejected", "the agent is not running; not forwarded")
            # Otherwise leave it: the hub offers it again once the lease ends.
            return
        try:
            forwarded = self._forward(delivery, tracked, endpoint, token)
        except AgentOutcomeUnknown as exc:
            if delivery.at_most_once:
                self._remember(delivery, "uncertain", f"forwarded once, outcome unknown: {exc}")
            return
        except _Invalid as exc:
            self._settle(delivery, "rejected", str(exc))
            return
        except AgentRejected as exc:
            # It reached the agent: an at-most-once one is never sent again.
            settle = self._remember if delivery.at_most_once else self._settle
            settle(delivery, "rejected", f"the agent refused it: {exc}")
            return
        except AgentUnreachable as exc:
            if delivery.at_most_once:
                self._settle(delivery, "rejected", f"not forwarded: {exc}")
            return
        if forwarded.turn is not None and delivery.kind in ("turn", "answer"):
            # Dispatched now; its record goes up when the turn has finished.
            instance, turn_id = forwarded.turn
            self.pending_results[delivery.delivery_id] = PendingTurnResult(
                delivery_id=delivery.delivery_id, intent_id=delivery.intent_id,
                instance=instance, turn_id=turn_id)
        if delivery.at_most_once:
            self._remember(delivery, "accepted", forwarded.detail)
            return
        self._settle(delivery, "accepted", forwarded.detail, forwarded.result)

    def _remember(self, delivery: ClaimedDelivery, status: str, detail: str | None) -> None:
        self.sent_once[delivery.delivery_id] = (status, detail)
        while len(self.sent_once) > _SENT_ONCE_MEMORY:
            self.sent_once.popitem(last=False)
        self._settle(delivery, status, detail)

    def _forward(
        self, delivery: ClaimedDelivery, tracked: TrackedWorkload, endpoint: str, token: str
    ) -> _Forwarded:
        """Forward one delivery: the settle detail, a `launch`'s `result` for
        the hub, and a turn's (instance, id) to collect its record later."""
        payload = delivery.payload
        if delivery.kind in _TURN_KINDS:
            if len(delivery.idempotency_key) > TURN_KEY_MAX:
                raise _Invalid("the idempotency key is longer than the agent accepts")
            instance = payload.get("instance") or (
                f"r-{delivery.run_intent_id.hex[:24]}" if delivery.run_intent_id else None
            )
            prompt = payload.get("prompt")
            if not isinstance(instance, str) or not _INSTANCE.fullmatch(instance):
                raise _Invalid("payload.instance is missing or not an instance name")
            if not isinstance(prompt, str) or not prompt:
                raise _Invalid("payload.prompt is missing")
            accepted = self.agents.start_turn(
                endpoint, token,
                instance=instance,
                prompt=prompt,
                idempotency_key=delivery.idempotency_key,
                provenance=_mapping(payload.get("provenance")),
                mirarun=_mapping(payload.get("mirarun")),
                credentials=_turn_credentials(delivery.credentials),
            )
            turn_id = str(accepted.get("turn_id") or "")
            if not turn_id:
                raise AgentOutcomeUnknown("the agent accepted the turn without an id")
            self._track_run(delivery, turn_id)
            detail = f"turn {turn_id}" + (" (duplicate)" if accepted.get("duplicate") else "")
            result = None
            if delivery.kind == "launch":
                # The hub's launch result: the run whose events follow.
                result = {
                    "run_id": turn_id,
                    "status": str(accepted.get("status") or "running"),
                    "duplicate": bool(accepted.get("duplicate", False)),
                }
            return _Forwarded(detail, result, (instance, turn_id))
        if delivery.kind == "discard":
            instance = payload.get("instance")
            if not isinstance(instance, str) or not _INSTANCE.fullmatch(instance):
                raise _Invalid("payload.instance is missing or not an instance name")
            self.agents.discard_instance(endpoint, token, instance=instance)
            return _Forwarded(None)
        if delivery.kind in ("resume", "abandon"):
            run_id = payload.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise _Invalid("payload.run_id is missing")
            if delivery.kind == "resume":
                body: dict[str, Any] = {}
                for key in ("prompt", "answer"):
                    if payload.get(key) is not None:
                        body[key] = payload[key]
                if not body:
                    raise _Invalid("resume needs payload.prompt or payload.answer")
                self.agents.control_run(endpoint, token, run_id, "resume", json=body)
            else:
                discard = bool(payload.get("discard_workspace", False))
                self.agents.control_run(endpoint, token, run_id, "abandon",
                                        params={"discard_workspace": str(discard).lower()})
            # Relay the run again: its new events, or its terminal status.
            self._track_run(delivery, run_id)
            return _Forwarded(None)
        raise _Invalid(f"unknown delivery kind '{delivery.kind}'")

    def _track_run(self, delivery: ClaimedDelivery, agent_run_id: str) -> None:
        if delivery.run_intent_id is None or not agent_run_id:
            return
        key = f"{delivery.intent_id}/{agent_run_id}"
        self.runs.setdefault(key, TrackedRun(
            run_intent_id=delivery.run_intent_id,
            intent_id=delivery.intent_id,
            agent_run_id=agent_run_id,
        ))

    # -- events -----------------------------------------------------------------

    def _pump(self, key: str, run: TrackedRun) -> None:
        tracked = self.workloads.get(run.intent_id)
        if tracked is None:
            self.runs.pop(key, None)
            return
        endpoint = self.driver.endpoint(tracked.namespace, tracked.name)
        token = self.driver.internal_token(tracked.namespace, tracked.name,
                                           tracked.workload_class)
        if endpoint is None or token is None:
            return
        if not self._relay(endpoint, token, run):
            return
        record = self.agents.run_record(endpoint, token, run.agent_run_id)
        if record is None or record.get("status") not in STOPPED_RUN_STATUSES:
            return
        # The run may have written its last events between the empty page
        # above and the status read: read once more, after seeing it stopped,
        # and only let go when that read is empty too. Then the stopped record
        # goes up once more with an empty page, so the hub sees the terminal
        # status even when the run's last event came before it.
        if self._relay(endpoint, token, run):
            self._sink().push_events(run.run_intent_id, record, _empty_page(record, run.after))
            self.runs.pop(key, None)

    def _sink(self) -> EventSink:
        if self.events is None:
            raise RuntimeError("the spoke has no event sink")
        return self.events

    def _relay(self, endpoint: str, token: str, run: TrackedRun) -> bool:
        """Relay what the agent has after the cursor (bounded). True when the
        last page read was empty and final: nothing is left right now.

        The cursor follows the hub's answer (`last_event_sequence`): the hub
        skips events it already has, so the spoke resumes from the hub's
        position rather than its own. A 413 halves the page size, down to one
        event; an event too big to send alone goes up as its envelope."""
        sink = self._sink()
        page: dict[str, Any] = {}
        for _ in range(_MAX_PAGES_PER_TICK):
            page = self.agents.events(endpoint, token, run.agent_run_id,
                                      after=run.after, limit=run.page_limit)
            next_after = page.get("next_after")
            if page.get("events"):
                record = self.agents.run_record(endpoint, token, run.agent_run_id)
                if record is None:
                    raise AgentRejected(404, "the agent has no record of the run")
                try:
                    cursor = sink.push_events(run.run_intent_id, record, _capped(page))
                except HubRefused as exc:
                    if exc.status_code == 413 and run.page_limit > 1:
                        run.page_limit = max(1, run.page_limit // 2)
                        return False
                    raise
                if cursor is not None:
                    run.after = cursor
                elif isinstance(next_after, int):
                    run.after = next_after
            elif isinstance(next_after, int):
                run.after = next_after
            if not page.get("has_more"):
                break
        return not page.get("events") and not page.get("has_more")

    def pump_events(self) -> None:
        for key, run in list(self.runs.items()):
            try:
                self._pump(key, run)
            except HubUnavailable:
                raise
            except HubRefused as exc:
                if exc.status_code == 404:
                    # The hub has no such run for this spoke: stop relaying it.
                    self.runs.pop(key, None)
                elif exc.status_code not in _ITEM_REFUSALS:
                    raise
                logger.warning("relaying events of %s: %s", run.agent_run_id, exc)
            except (AgentRejected, AgentUnreachable, AgentOutcomeUnknown,
                    SpawnOperationFailed) as exc:
                logger.warning("relaying events of %s failed: %s", run.agent_run_id, exc)

    def pump_turn_results(self) -> None:
        """Post each finished turn's record. A turn still running stays; one
        the agent or the hub no longer knows (404, 409) is dropped."""
        for delivery_id, pending in list(self.pending_results.items()):
            tracked = self.workloads.get(pending.intent_id)
            if tracked is None:
                self.pending_results.pop(delivery_id, None)
                continue
            try:
                endpoint = self.driver.endpoint(tracked.namespace, tracked.name)
                token = self.driver.internal_token(tracked.namespace, tracked.name,
                                                   tracked.workload_class)
                if endpoint is None or token is None:
                    continue
                record = self.agents.get_turn(endpoint, token, instance=pending.instance,
                                              turn_id=pending.turn_id)
                if record is None:
                    self.pending_results.pop(delivery_id, None)
                    continue
                if record.get("status") == "running":
                    continue
                self.hub.post_turn_result(delivery_id, record)
                self.pending_results.pop(delivery_id, None)
            except HubUnavailable:
                raise
            except HubRefused as exc:
                if exc.status_code in (404, 409):
                    self.pending_results.pop(delivery_id, None)
                elif exc.status_code not in _ITEM_REFUSALS:
                    raise
                logger.warning("turn result for %s: %s", delivery_id, exc)
            except (AgentRejected, AgentUnreachable, AgentOutcomeUnknown,
                    SpawnOperationFailed) as exc:
                logger.warning("reading turn %s failed: %s", pending.turn_id, exc)

    def _drain_runs(self, intent_id: UUID) -> None:
        """Before a workload goes, relay what its runs still have."""
        for key, run in list(self.runs.items()):
            if run.intent_id != intent_id:
                continue
            try:
                self._pump(key, run)
            except HubRefused as exc:
                if exc.status_code not in _ITEM_REFUSALS:
                    raise
                logger.warning("draining events of %s: %s", run.agent_run_id, exc)
            except (AgentRejected, AgentUnreachable, AgentOutcomeUnknown,
                    SpawnOperationFailed) as exc:
                logger.warning("draining events of %s failed: %s", run.agent_run_id, exc)
            self.runs.pop(key, None)


def _iso(value: str | None) -> str | None:
    """A Kubernetes timestamp as the hub's datetime, or None: a malformed one
    must not cost the whole observation."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).isoformat()


def _capped(page: dict[str, Any]) -> dict[str, Any]:
    """The page, with any single event too big to send replaced by its
    envelope and `truncated: true`."""
    events = page.get("events") or []
    capped = []
    changed = False
    for event in events:
        if isinstance(event, dict) and len(json.dumps(event, default=str)) > _MAX_EVENT_BYTES:
            changed = True
            capped.append({**{k: event[k] for k in _ENVELOPE_KEYS if k in event},
                           "truncated": True})
        else:
            capped.append(event)
    return {**page, "events": capped} if changed else page


def _turn_credentials(minted: dict[str, Any] | None) -> dict[str, Any] | None:
    """The hub's minted credentials as the agent's `TurnCredentials`.

    The base tier finds a run-scoped MCP credential by the environment
    variable its capability names (`bearer_token_env`), so `secret_env` must
    be keyed by that name. Values keyed by EDF binding name (`secrets`) cannot
    be placed by the agent: refused, never dropped silently."""
    if not minted:
        return None
    secret_env = minted.get("secret_env") or {}
    if minted.get("secrets") and not secret_env:
        raise _Invalid(
            "the run's credentials are keyed by binding name; a base-tier turn "
            "needs them keyed by environment variable (secret_env)"
        )
    out: dict[str, Any] = {}
    if secret_env:
        out["secret_env"] = dict(secret_env)
    if minted.get("repositories"):
        # Forwarded so the agent refuses them loudly (no base-tier checkout).
        out["repositories"] = dict(minted["repositories"])
    return out or None


def _empty_page(record: dict[str, Any], after: int) -> dict[str, Any]:
    return {
        "run_id": record.get("run_id"),
        "count": 0,
        "events": [],
        "next_after": after,
        "has_more": False,
    }


class _Invalid(Exception):
    """The delivery itself is malformed; it is settled `rejected`."""


def _mapping(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None
