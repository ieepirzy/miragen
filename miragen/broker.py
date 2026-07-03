from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel

from miragen.models import ApprovalRequest, ApprovalResponse


class PendingApproval(BaseModel):
    request_id: str
    agent_name: str
    tool_name: str
    tool_args: dict
    submitted_at: str  # ISO8601


class ApprovalBroker:
    """
    In-process approval queue for queue-mode approval gating.

    Agents submit approval requests here and await the result. External callers
    resolve (approve/deny) via POST /approvals/{request_id}. Requests that
    aren't resolved within timeout_s are auto-denied and removed from the queue.
    """

    def __init__(self) -> None:
        self._pending: dict[str, tuple[PendingApproval, asyncio.Future]] = {}

    async def submit(self, request: ApprovalRequest, timeout_s: int) -> ApprovalResponse:
        """
        Register an approval request and wait for it to be resolved (or time out).

        On timeout the request is auto-denied and removed from the pending map.
        """
        loop = asyncio.get_event_loop()
        future: asyncio.Future[ApprovalResponse] = loop.create_future()

        pending = PendingApproval(
            request_id=request.request_id,
            agent_name=request.agent_name,
            tool_name=request.tool_name,
            tool_args=request.tool_args,
            submitted_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._pending[request.request_id] = (pending, future)

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
        except asyncio.TimeoutError:
            # Auto-deny on timeout; remove from pending map.
            self._pending.pop(request.request_id, None)
            if not future.done():
                future.cancel()
            return ApprovalResponse(
                approved=False,
                prompt=f"Approval request timed out after {timeout_s}s.",
            )
        except asyncio.CancelledError:
            self._pending.pop(request.request_id, None)
            return ApprovalResponse(approved=False, prompt="Approval request was cancelled.")

    def resolve(self, request_id: str, response: ApprovalResponse) -> bool:
        """
        Resolve a pending approval request.

        Returns True if the request was found and resolved; False if not found
        (already timed out, already resolved, or unknown id).
        """
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return False
        _, future = entry
        if future.done():
            return False
        future.set_result(response)
        return True

    def pending(self) -> list[PendingApproval]:
        """Return a snapshot of currently pending approval requests."""
        return [pa for pa, _ in self._pending.values()]
