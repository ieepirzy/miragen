from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel

from miragen.models import ApprovalRequest, ApprovalResponse


class PendingApproval(BaseModel):
    request: ApprovalRequest
    created_at: datetime
    expires_at: datetime
    # 'async': the turn that asked has moved on; resolving it (approved) runs
    # the call and answers with its outcome (profile approval_delivery).
    delivery: Literal["blocking", "async"] = "blocking"


# Runs an approved async call; returns the outcome fields (executed, ok,
# result_text, …).
AsyncExecute = Callable[[ApprovalResponse], Awaitable[dict[str, Any]]]
_OUTCOMES_KEPT = 200


@dataclass
class _AsyncEntry:
    execute: AsyncExecute
    handle: asyncio.TimerHandle


class ApprovalBroker:
    """
    In-memory queue of pending approval requests, resolvable over HTTP.

    In-memory only by design: the waiting side is an asyncio.Future inside a
    live agent run — neither can survive a container restart, so persisting
    the queue would only create zombies. A restart aborts the run, which the
    run-records startup sweep (RunStore.sweep_interrupted) records as
    `interrupted`.

    Async approvals (approval_delivery: async) are in memory too: a restart
    drops them, a later resolve answers 404, and nothing runs. The agent was
    told "not run yet", so a lost request fails safe; it can be asked again.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._futures: dict[str, asyncio.Future[ApprovalResponse]] = {}
        self._async: dict[str, _AsyncEntry] = {}
        self._outcomes: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Long-poll support: `version` moves whenever the pending set does,
        # and waiters are woken then (GET /approvals?since=&wait=).
        self.version = 0
        self._changed: asyncio.Event | None = None

    def _bump(self) -> None:
        self.version += 1
        changed, self._changed = self._changed, None
        if changed is not None:
            changed.set()

    async def wait_for_change(self, since: int, timeout_s: float) -> None:
        """Return once the pending set has changed since `since`, or after
        timeout_s, whichever comes first."""
        if self.version != since or timeout_s <= 0:
            return
        if self._changed is None:
            self._changed = asyncio.Event()
        changed = self._changed
        try:
            await asyncio.wait_for(changed.wait(), timeout_s)
        except TimeoutError:
            pass

    def submit(self, request: ApprovalRequest, timeout_s: int) -> asyncio.Future[ApprovalResponse]:
        """Park a request and return a Future that resolves on POST /approvals/{id}
        or, failing that, to a denial after timeout_s."""
        now = datetime.now(timezone.utc)
        self._pending[request.request_id] = PendingApproval(
            request=request,
            created_at=now,
            expires_at=now + timedelta(seconds=timeout_s),
        )

        loop = asyncio.get_event_loop()
        future: asyncio.Future[ApprovalResponse] = loop.create_future()
        self._futures[request.request_id] = future

        def _expire() -> None:
            self.resolve(
                request.request_id,
                ApprovalResponse(approved=False, prompt=f"approval request timed out after {timeout_s}s"),
            )

        handle = loop.call_later(timeout_s, _expire)
        future.add_done_callback(lambda _: handle.cancel())
        self._bump()
        return future

    def submit_async(self, request: ApprovalRequest, timeout_s: int,
                     execute: AsyncExecute) -> PendingApproval:
        """Park an async request: nothing waits on it. Approving it through
        resolve_async runs `execute`; denial or expiry never does."""
        now = datetime.now(timezone.utc)
        pending = PendingApproval(request=request, created_at=now,
                                  expires_at=now + timedelta(seconds=timeout_s), delivery="async")
        self._pending[request.request_id] = pending

        def _expire() -> None:
            if self._async.pop(request.request_id, None) is not None:
                self._pending.pop(request.request_id, None)
                self._store(request, {"executed": False, "approved": False, "expired": True,
                                      "reason": f"approval request timed out after {timeout_s}s"})
                self._bump()

        handle = asyncio.get_event_loop().call_later(timeout_s, _expire)
        self._async[request.request_id] = _AsyncEntry(execute=execute, handle=handle)
        self._bump()
        return pending

    def is_async(self, request_id: str) -> bool:
        return request_id in self._async

    async def resolve_async(self, request_id: str,
                            response: ApprovalResponse) -> dict[str, Any] | None:
        """Resolve an async request: approved → run it (once: the entry is
        taken before anything awaits), then return and keep its outcome.
        None when it is unknown, already resolved or expired."""
        entry = self._async.pop(request_id, None)
        pending = self._pending.pop(request_id, None)
        if entry is None or pending is None:
            return None
        entry.handle.cancel()
        self._bump()
        if not response.approved:
            outcome: dict[str, Any] = {"executed": False, "approved": False,
                                       "reason": response.prompt}
        else:
            try:
                outcome = {"approved": True, **await entry.execute(response)}
            except Exception as exc:  # noqa: BLE001 — reported, never re-run
                outcome = {"approved": True, "executed": False, "ok": False,
                           "result_text": f"{type(exc).__name__}: {exc}"}
        return self._store(pending.request, outcome)

    def _store(self, request: ApprovalRequest, outcome: dict[str, Any]) -> dict[str, Any]:
        outcome = {"request_id": request.request_id, "tool_name": request.tool_name,
                   "tool_args": request.tool_args, **outcome,
                   "resolved_at": datetime.now(timezone.utc).isoformat()}
        self._outcomes[request.request_id] = outcome
        while len(self._outcomes) > _OUTCOMES_KEPT:
            self._outcomes.popitem(last=False)
        return outcome

    def outcome(self, request_id: str) -> dict[str, Any] | None:
        """A resolved async request's outcome (the last few hundred are kept)."""
        return self._outcomes.get(request_id)

    def resolve(self, request_id: str, response: ApprovalResponse) -> bool:
        """Resolve a pending request. Returns False if unknown, already resolved, or expired."""
        if request_id in self._async:
            return False  # async requests resolve (and run) only through resolve_async
        future = self._futures.pop(request_id, None)
        if self._pending.pop(request_id, None) is not None:
            self._bump()
        if future is None or future.done():
            return False
        future.set_result(response)
        return True

    def pending(self) -> list[PendingApproval]:
        return list(self._pending.values())


_broker = ApprovalBroker()


def get_broker() -> ApprovalBroker:
    return _broker
