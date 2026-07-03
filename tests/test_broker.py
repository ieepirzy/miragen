import asyncio

import pytest

from miragen.broker import ApprovalBroker
from miragen.models import ApprovalRequest, ApprovalResponse


def _request(request_id="r1", tool_name="delete_file"):
    return ApprovalRequest(
        agent_name="a", tool_name=tool_name, tool_args={"path": "/x"}, request_id=request_id,
    )


class TestApprovalBrokerSubmitResolve:
    async def test_resolve_approved_unblocks_submit(self):
        broker = ApprovalBroker()
        future = broker.submit(_request(), timeout_s=5)

        resolved = broker.resolve("r1", ApprovalResponse(approved=True))
        assert resolved is True

        response = await future
        assert response.approved is True

    async def test_resolve_denied_with_reason(self):
        broker = ApprovalBroker()
        future = broker.submit(_request(), timeout_s=5)

        broker.resolve("r1", ApprovalResponse(approved=False, prompt="not today"))

        response = await future
        assert response.approved is False
        assert response.prompt == "not today"

    def test_resolve_unknown_id_returns_false(self):
        broker = ApprovalBroker()
        assert broker.resolve("nope", ApprovalResponse(approved=True)) is False

    async def test_resolve_after_already_resolved_returns_false(self):
        broker = ApprovalBroker()
        future = broker.submit(_request(), timeout_s=5)
        broker.resolve("r1", ApprovalResponse(approved=True))
        await future

        # Second resolve should not affect the first outcome or raise.
        second = broker.resolve("r1", ApprovalResponse(approved=False))
        assert second is False

    async def test_resolving_one_id_does_not_affect_others(self):
        broker = ApprovalBroker()
        broker.submit(_request("r1"), timeout_s=5)
        broker.submit(_request("r2"), timeout_s=5)

        broker.resolve("r1", ApprovalResponse(approved=True))

        ids = {p.request.request_id for p in broker.pending()}
        assert ids == {"r2"}


class TestApprovalBrokerTimeout:
    async def test_timeout_resolves_to_denial(self):
        broker = ApprovalBroker()
        future = broker.submit(_request(), timeout_s=0.05)

        response = await asyncio.wait_for(future, timeout=2)

        assert response.approved is False
        assert "timed out" in response.prompt
        assert "0.05s" in response.prompt

    async def test_expired_entry_is_removed_from_pending(self):
        broker = ApprovalBroker()
        broker.submit(_request(), timeout_s=0.05)
        await asyncio.sleep(0.2)

        assert broker.pending() == []

    async def test_resolve_after_expiry_returns_false(self):
        broker = ApprovalBroker()
        broker.submit(_request(), timeout_s=0.05)
        await asyncio.sleep(0.2)

        assert broker.resolve("r1", ApprovalResponse(approved=True)) is False


class TestApprovalBrokerPending:
    def test_empty_broker(self):
        assert ApprovalBroker().pending() == []

    async def test_pending_includes_tool_args(self):
        broker = ApprovalBroker()
        broker.submit(_request(tool_name="delete_file"), timeout_s=5)

        pending = broker.pending()
        assert len(pending) == 1
        assert pending[0].request.tool_name == "delete_file"
        assert pending[0].request.tool_args == {"path": "/x"}

    async def test_ordering_by_submission(self):
        broker = ApprovalBroker()
        broker.submit(_request("r1"), timeout_s=5)
        broker.submit(_request("r2"), timeout_s=5)

        ids = [p.request.request_id for p in broker.pending()]
        assert ids == ["r1", "r2"]
