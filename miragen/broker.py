from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from miragen.models import ApprovalRequest, ApprovalResponse


class PendingApproval(BaseModel):
    request: ApprovalRequest
    created_at: datetime
    expires_at: datetime


class ApprovalBroker:
    """
    In-memory queue of pending approval requests, resolvable over HTTP.

    In-memory only by design: the waiting side is an asyncio.Future inside a
    live agent run — neither can survive a container restart, so persisting
    the queue would only create zombies. A restart aborts the run, which the
    run-records startup sweep (RunStore.sweep_interrupted) records as
    `interrupted`.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._futures: dict[str, asyncio.Future[ApprovalResponse]] = {}

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
        return future

    def resolve(self, request_id: str, response: ApprovalResponse) -> bool:
        """Resolve a pending request. Returns False if unknown, already resolved, or expired."""
        future = self._futures.pop(request_id, None)
        self._pending.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(response)
        return True

    def pending(self) -> list[PendingApproval]:
        return list(self._pending.values())


_broker = ApprovalBroker()


def get_broker() -> ApprovalBroker:
    return _broker
