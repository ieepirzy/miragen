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
   by cursor, to the hub (`EventSink`).

Nothing here is pushed by the hub. Every call is the spoke's.
"""

from __future__ import annotations

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


@dataclass
class PendingSettle:
    """An at-most-once delivery already forwarded: only its settle may be
    retried, never the forward."""

    delivery: ClaimedDelivery
    status: str
    detail: str | None


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

    # -- one tick ---------------------------------------------------------------

    def tick(self) -> bool:
        """One pass. True when anything was claimed, so the caller polls
        again at once instead of sleeping."""
        busy = False
        for intent in self.hub.claim_intents(self.config.claim_limit):
            busy = True
            try:
                self.handle_intent(intent)
            except (HubRefused, SpawnOperationFailed) as exc:
                # One intent's trouble never stops the others.
                logger.error("intent %s: %s", intent.intent_id, exc)
        self.watch()
        self.retry_settles()
        for delivery in self.hub.claim_deliveries(self.config.claim_limit):
            busy = True
            try:
                self.handle_delivery(delivery)
            except HubRefused as exc:
                logger.error("delivery %s: %s", delivery.delivery_id, exc)
        self.pump_events()
        return busy

    def run_forever(self, *, should_stop: Callable[[], bool] = lambda: False,
                    sleep: Callable[[float], None] = time.sleep) -> None:
        self.recover()
        failures = 0
        while not should_stop():
            try:
                busy = self.tick()
                failures = 0
            except HubUnavailable as exc:
                failures += 1
                delay = min(
                    self.config.max_backoff_seconds,
                    self.config.interval_seconds * (2 ** min(failures, 10)),
                )
                logger.warning("hub unavailable (%s); retrying in %.0fs", exc, delay)
                sleep(delay * random.uniform(0.8, 1.2))
                continue
            except HubRefused as exc:
                # The spoke's own credential or request is wrong: say so loudly,
                # keep the loop alive at the normal pace.
                logger.error("the hub refused the spoke: %s", exc)
                busy = False
            if not busy:
                sleep(self.config.interval_seconds)

    # -- intents ----------------------------------------------------------------

    def _observe(self, intent: ClaimedIntent | TrackedWorkload, observation: Observation) -> None:
        self.hub.report(intent.organization_id, intent.intent_id, observation)

    def _refuse(self, intent: ClaimedIntent, reason: str) -> None:
        logger.warning("refusing intent %s: %s", intent.intent_id, reason)
        self._observe(intent, Observation(intent.generation, intent.attempt, "refused", reason))
        self._ack(intent)

    def _ack(self, intent: ClaimedIntent) -> bool:
        try:
            self.hub.acknowledge(intent)
            return True
        except LeaseLost:
            # A newer generation took the lease; the hub offers that one next.
            logger.info("lease lost on intent %s generation %s", intent.intent_id,
                        intent.generation)
            return False

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
        if not self._ack(intent):
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
            self._observe(intent, Observation(intent.generation, intent.attempt, "stopped",
                                              k8s_namespace=intent.namespace))
            self.workloads.pop(intent.intent_id, None)
            return
        self.workloads[intent.intent_id] = tracked
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
            started_at=status.started_at,
            finished_at=status.finished_at,
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
            except (HubRefused, SpawnOperationFailed) as exc:
                logger.warning("watching %s failed: %s", tracked.name, exc)
                continue
            if tracked.last_phase == "absent":
                self.workloads.pop(intent_id, None)
            elif ended and tracked.workload_class == "ephemeral":
                # The hub has the final report (the call above returned), so
                # the Job can go. Its backstop TTL covers a spoke that dies
                # between the two.
                self._drain_runs(intent_id)
                self.driver.delete(tracked.namespace, tracked.name, "ephemeral")
                self.workloads.pop(intent_id, None)

    # -- deliveries -------------------------------------------------------------

    def _settle(self, delivery: ClaimedDelivery, status: str, detail: str | None = None) -> None:
        try:
            self.hub.settle(delivery, status, detail)
            self.pending_settles.pop(delivery.delivery_id, None)
        except LeaseLost:
            self.pending_settles.pop(delivery.delivery_id, None)
            logger.info("delivery %s: lease lost before settling", delivery.delivery_id)
        except HubUnavailable:
            if delivery.at_most_once:
                # Never forward it again; only the settle is retried.
                self.pending_settles[delivery.delivery_id] = PendingSettle(delivery, status, detail)
            raise

    def retry_settles(self) -> None:
        for pending in list(self.pending_settles.values()):
            self._settle(pending.delivery, pending.status, pending.detail)

    def handle_delivery(self, delivery: ClaimedDelivery) -> None:
        if delivery.delivery_id in self.pending_settles:
            return
        if delivery.delivery_id in self.sent_once:
            # Already sent once: settle it as it was settled, never send again.
            status, detail = self.sent_once[delivery.delivery_id]
            self._settle(delivery, status, detail)
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
            detail = self._forward(delivery, tracked, endpoint, token)
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
        if delivery.at_most_once:
            self._remember(delivery, "accepted", detail)
            return
        self._settle(delivery, "accepted", detail)

    def _remember(self, delivery: ClaimedDelivery, status: str, detail: str | None) -> None:
        self.sent_once[delivery.delivery_id] = (status, detail)
        while len(self.sent_once) > _SENT_ONCE_MEMORY:
            self.sent_once.popitem(last=False)
        self._settle(delivery, status, detail)

    def _forward(self, delivery: ClaimedDelivery, tracked: TrackedWorkload,
                 endpoint: str, token: str) -> str | None:
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
            )
            turn_id = str(accepted.get("turn_id") or "")
            self._track_run(delivery, turn_id)
            return f"turn {turn_id}" + (" (duplicate)" if accepted.get("duplicate") else "")
        if delivery.kind == "discard":
            instance = payload.get("instance")
            if not isinstance(instance, str) or not _INSTANCE.fullmatch(instance):
                raise _Invalid("payload.instance is missing or not an instance name")
            self.agents.discard_instance(endpoint, token, instance=instance)
            return None
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
                self._track_run(delivery, run_id)
            else:
                discard = bool(payload.get("discard_workspace", False))
                self.agents.control_run(endpoint, token, run_id, "abandon",
                                        params={"discard_workspace": str(discard).lower()})
            return None
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
        for _ in range(_MAX_PAGES_PER_TICK):
            page = self.agents.events(endpoint, token, run.agent_run_id, after=run.after)
            if page.get("events"):
                assert self.events is not None
                self.events.push_page(run.run_intent_id, page)
            next_after = page.get("next_after")
            if isinstance(next_after, int):
                run.after = next_after
            if not page.get("has_more"):
                break
        if not page.get("events") and not page.get("has_more"):
            status = self.agents.run_status(endpoint, token, run.agent_run_id)
            if status in STOPPED_RUN_STATUSES:
                self.runs.pop(key, None)

    def pump_events(self) -> None:
        for key, run in list(self.runs.items()):
            try:
                self._pump(key, run)
            except HubUnavailable:
                raise
            except (HubRefused, AgentRejected, AgentUnreachable, AgentOutcomeUnknown) as exc:
                logger.warning("relaying events of %s failed: %s", run.agent_run_id, exc)

    def _drain_runs(self, intent_id: UUID) -> None:
        """Before a workload goes, relay what its runs still have."""
        for key, run in list(self.runs.items()):
            if run.intent_id != intent_id:
                continue
            try:
                self._pump(key, run)
            except (AgentRejected, AgentUnreachable, AgentOutcomeUnknown) as exc:
                logger.warning("draining events of %s failed: %s", run.agent_run_id, exc)
            self.runs.pop(key, None)


class _Invalid(Exception):
    """The delivery itself is malformed; it is settled `rejected`."""


def _mapping(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None
