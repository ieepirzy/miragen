import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic_ai.exceptions import ModelRetry

import miragen.factory as factory_module
from miragen.approval import _run_approval_gate, build_approval_hooks
from miragen.factory import register_approval_handler, get_approval_handler
from miragen.models import AgentProfile, ApprovalRequest, ApprovalResponse


# ── Helpers ───────────────────────────────────────────────────────────────────

def _profile(**kw):
    return AgentProfile.model_validate({
        "name": "test-agent",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "spec": {"model": "m", "instructions": "i"},
        **kw,
    })


def _mock_call(tool_name: str, args: dict | None = None):
    call = MagicMock()
    call.tool_name = tool_name
    call.args_as_dict = MagicMock(return_value=args or {})
    return call


@pytest.fixture(autouse=True)
def reset_approval_handler():
    original = factory_module._approval_handler
    yield
    factory_module._approval_handler = original


# ── register_approval_handler ─────────────────────────────────────────────────

class TestRegisterApprovalHandler:
    def test_registers_function(self):
        async def my_handler(req): ...
        register_approval_handler(my_handler)
        assert get_approval_handler() is my_handler

    def test_returns_original_function(self):
        async def my_handler(req): ...
        result = register_approval_handler(my_handler)
        assert result is my_handler

    def test_as_decorator(self):
        @register_approval_handler
        async def handler(req): ...
        assert get_approval_handler() is handler

    def test_overwrites_previous(self):
        async def first(req): ...
        async def second(req): ...
        register_approval_handler(first)
        register_approval_handler(second)
        assert get_approval_handler() is second

    def test_get_returns_none_by_default(self):
        factory_module._approval_handler = None
        assert get_approval_handler() is None


# ── build_approval_hooks ──────────────────────────────────────────────────────

class TestBuildApprovalHooks:
    def test_returns_none_when_no_patterns(self):
        profile = _profile()
        assert build_approval_hooks(profile) is None

    def test_returns_none_when_empty_patterns(self):
        profile = _profile(approval_required=[])
        assert build_approval_hooks(profile) is None

    def test_returns_hooks_when_patterns_set(self):
        from pydantic_ai.capabilities import Hooks
        profile = _profile(approval_required=["delete_*"])
        result = build_approval_hooks(profile)
        assert isinstance(result, Hooks)


# ── _run_approval_gate — pass-through ─────────────────────────────────────────

class TestApprovalGatePassThrough:
    async def test_non_matching_tool_passes_through(self):
        profile = _profile(approval_required=["delete_*"])
        call = _mock_call("safe_tool")
        handler = AsyncMock(return_value="result")

        result = await _run_approval_gate(profile, call, {}, handler)

        assert result == "result"
        handler.assert_awaited_once()

    async def test_no_patterns_passes_through(self):
        profile = _profile()
        call = _mock_call("delete_everything")
        handler = AsyncMock(return_value="result")

        result = await _run_approval_gate(profile, call, {}, handler)
        assert result == "result"

    async def test_glob_wildcard_matches(self):
        profile = _profile(approval_required=["delete_*"])
        call = _mock_call("delete_file")
        handler = AsyncMock(return_value="deleted")

        approval = AsyncMock(return_value=ApprovalResponse(approved=True))
        factory_module._approval_handler = approval

        result = await _run_approval_gate(profile, call, {}, handler)
        assert result == "deleted"

    async def test_multiple_patterns_any_match(self):
        profile = _profile(approval_required=["delete_*", "execute_*", "rm_*"])
        call = _mock_call("execute_shell")
        handler = AsyncMock(return_value="ok")

        approval = AsyncMock(return_value=ApprovalResponse(approved=True))
        factory_module._approval_handler = approval

        result = await _run_approval_gate(profile, call, {}, handler)
        assert result == "ok"


# ── _run_approval_gate — auto-approve (fail open) ─────────────────────────────

class TestApprovalGateFailOpen:
    async def test_no_handler_no_webhook_auto_approves(self, caplog):
        import logging
        profile = _profile(approval_required=["delete_*"])
        call = _mock_call("delete_file")
        handler = AsyncMock(return_value="deleted")
        factory_module._approval_handler = None

        with caplog.at_level(logging.WARNING, logger="miragen.approval"):
            result = await _run_approval_gate(profile, call, {}, handler)

        assert result == "deleted"
        assert "auto-approving" in caplog.text
        assert "fail open" in caplog.text


# ── _run_approval_gate — registered handler ───────────────────────────────────

class TestApprovalGateWithHandler:
    async def test_approved_runs_tool(self):
        profile = _profile(approval_required=["delete_*"])
        call = _mock_call("delete_file", {"path": "/tmp/x"})
        handler = AsyncMock(return_value="deleted")

        approval_fn = AsyncMock(return_value=ApprovalResponse(approved=True))
        factory_module._approval_handler = approval_fn

        result = await _run_approval_gate(profile, call, {}, handler)

        assert result == "deleted"
        handler.assert_awaited_once()

    async def test_handler_receives_correct_request(self):
        profile = _profile(approval_required=["delete_*"])
        call = _mock_call("delete_file", {"path": "/tmp/x"})
        handler = AsyncMock(return_value="ok")

        captured: list[ApprovalRequest] = []

        async def approval_fn(req: ApprovalRequest) -> ApprovalResponse:
            captured.append(req)
            return ApprovalResponse(approved=True)

        factory_module._approval_handler = approval_fn

        await _run_approval_gate(profile, call, {}, handler)

        assert len(captured) == 1
        req = captured[0]
        assert req.agent_name == "test-agent"
        assert req.tool_name == "delete_file"
        assert req.tool_args == {"path": "/tmp/x"}
        assert len(req.request_id) == 36  # uuid4

    async def test_denied_raises_model_retry(self):
        profile = _profile(approval_required=["delete_*"])
        call = _mock_call("delete_file")
        handler = AsyncMock(return_value="deleted")

        approval_fn = AsyncMock(return_value=ApprovalResponse(approved=False))
        factory_module._approval_handler = approval_fn

        with pytest.raises(ModelRetry, match="delete_file"):
            await _run_approval_gate(profile, call, {}, handler)

        handler.assert_not_awaited()

    async def test_denied_with_reason_included_in_error(self):
        profile = _profile(approval_required=["delete_*"])
        call = _mock_call("delete_file")
        handler = AsyncMock()

        approval_fn = AsyncMock(
            return_value=ApprovalResponse(approved=False, prompt="Too risky")
        )
        factory_module._approval_handler = approval_fn

        with pytest.raises(ModelRetry, match="Too risky"):
            await _run_approval_gate(profile, call, {}, handler)

    async def test_approved_with_prompt_wraps_result(self):
        profile = _profile(approval_required=["delete_*"])
        call = _mock_call("delete_file")
        handler = AsyncMock(return_value="file deleted")

        approval_fn = AsyncMock(
            return_value=ApprovalResponse(approved=True, prompt="Verified by admin")
        )
        factory_module._approval_handler = approval_fn

        result = await _run_approval_gate(profile, call, {}, handler)

        assert "Verified by admin" in result
        assert "file deleted" in result
        assert result.startswith("[Approver note:")

    async def test_approved_without_prompt_no_prefix(self):
        profile = _profile(approval_required=["delete_*"])
        call = _mock_call("delete_file")
        handler = AsyncMock(return_value="file deleted")

        approval_fn = AsyncMock(return_value=ApprovalResponse(approved=True))
        factory_module._approval_handler = approval_fn

        result = await _run_approval_gate(profile, call, {}, handler)

        assert result == "file deleted"
        assert "Approver" not in result

    async def test_each_call_gets_unique_request_id(self):
        profile = _profile(approval_required=["delete_*"])
        ids: list[str] = []

        async def approval_fn(req: ApprovalRequest) -> ApprovalResponse:
            ids.append(req.request_id)
            return ApprovalResponse(approved=True)

        factory_module._approval_handler = approval_fn

        for _ in range(3):
            call = _mock_call("delete_file")
            await _run_approval_gate(profile, call, {}, AsyncMock(return_value="ok"))

        assert len(set(ids)) == 3


# ── _run_approval_gate — webhook fallback ────────────────────────────────────

class TestApprovalGateWithWebhook:
    async def test_posts_to_webhook_and_approves(self):
        profile = _profile(
            approval_required=["delete_*"],
            approval_webhook="https://approval.example.com/review",
        )
        call = _mock_call("delete_file")
        handler = AsyncMock(return_value="deleted")
        factory_module._approval_handler = None

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"approved": True})

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=MagicMock(
            post=AsyncMock(return_value=mock_resp)
        ))
        mock_http.__aexit__ = AsyncMock(return_value=False)

        with patch("miragen.approval.httpx.AsyncClient", return_value=mock_http):
            result = await _run_approval_gate(profile, call, {}, handler)

        assert result == "deleted"

    async def test_webhook_denied_raises_model_retry(self):
        profile = _profile(
            approval_required=["delete_*"],
            approval_webhook="https://approval.example.com/review",
        )
        call = _mock_call("delete_file")
        handler = AsyncMock()
        factory_module._approval_handler = None

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"approved": False, "prompt": "Not allowed"})

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=MagicMock(
            post=AsyncMock(return_value=mock_resp)
        ))
        mock_http.__aexit__ = AsyncMock(return_value=False)

        with patch("miragen.approval.httpx.AsyncClient", return_value=mock_http):
            with pytest.raises(ModelRetry, match="Not allowed"):
                await _run_approval_gate(profile, call, {}, handler)

    async def test_registered_handler_takes_precedence_over_webhook(self):
        profile = _profile(
            approval_required=["delete_*"],
            approval_webhook="https://approval.example.com/review",
        )
        call = _mock_call("delete_file")
        handler = AsyncMock(return_value="ok")

        handler_called = []

        async def approval_fn(req):
            handler_called.append(True)
            return ApprovalResponse(approved=True)

        factory_module._approval_handler = approval_fn

        with patch("miragen.approval.httpx.AsyncClient") as mock_client:
            result = await _run_approval_gate(profile, call, {}, handler)

        assert result == "ok"
        assert handler_called == [True]
        mock_client.assert_not_called()


# ── Models ────────────────────────────────────────────────────────────────────

class TestApprovalModels:
    def test_approval_request_fields(self):
        req = ApprovalRequest(
            agent_name="my-agent",
            tool_name="delete_file",
            tool_args={"path": "/tmp/x"},
            request_id="abc-123",
        )
        assert req.agent_name == "my-agent"
        assert req.tool_args == {"path": "/tmp/x"}

    def test_approval_response_approved(self):
        resp = ApprovalResponse(approved=True)
        assert resp.approved is True
        assert resp.prompt is None

    def test_approval_response_denied_with_prompt(self):
        resp = ApprovalResponse(approved=False, prompt="Too dangerous")
        assert resp.approved is False
        assert resp.prompt == "Too dangerous"

    def test_agent_profile_approval_webhook(self):
        profile = _profile(approval_webhook="https://example.com/approve")
        assert str(profile.approval_webhook).startswith("https://example.com")

    def test_agent_profile_no_webhook_by_default(self):
        profile = _profile()
        assert profile.approval_webhook is None


# ── ApprovalBroker ────────────────────────────────────────────────────────────

class TestApprovalBroker:
    async def test_submit_and_resolve_approved(self):
        from miragen.broker import ApprovalBroker

        broker = ApprovalBroker()
        request = ApprovalRequest(
            agent_name="a", tool_name="delete_file",
            tool_args={"path": "/tmp/x"}, request_id="req-001",
        )

        # Resolve from a background task
        async def _resolve():
            await asyncio.sleep(0)
            broker.resolve("req-001", ApprovalResponse(approved=True, prompt="OK"))

        import asyncio
        asyncio.create_task(_resolve())
        response = await broker.submit(request, timeout_s=5)

        assert response.approved is True
        assert response.prompt == "OK"

    async def test_submit_and_resolve_denied(self):
        from miragen.broker import ApprovalBroker
        import asyncio

        broker = ApprovalBroker()
        request = ApprovalRequest(
            agent_name="a", tool_name="delete_file",
            tool_args={}, request_id="req-002",
        )

        async def _resolve():
            await asyncio.sleep(0)
            broker.resolve("req-002", ApprovalResponse(approved=False, prompt="Denied"))

        asyncio.create_task(_resolve())
        response = await broker.submit(request, timeout_s=5)

        assert response.approved is False

    async def test_submit_timeout_auto_denies(self):
        from miragen.broker import ApprovalBroker

        broker = ApprovalBroker()
        request = ApprovalRequest(
            agent_name="a", tool_name="risky",
            tool_args={}, request_id="req-003",
        )

        response = await broker.submit(request, timeout_s=0)
        assert response.approved is False
        assert "timed out" in response.prompt.lower()
        # Should be removed from pending after timeout
        assert len(broker.pending()) == 0

    async def test_resolve_unknown_returns_false(self):
        from miragen.broker import ApprovalBroker

        broker = ApprovalBroker()
        result = broker.resolve("nonexistent", ApprovalResponse(approved=True))
        assert result is False

    async def test_resolve_already_resolved_returns_false(self):
        from miragen.broker import ApprovalBroker
        import asyncio

        broker = ApprovalBroker()
        request = ApprovalRequest(
            agent_name="a", tool_name="t", tool_args={}, request_id="req-004",
        )

        async def _resolve():
            await asyncio.sleep(0)
            broker.resolve("req-004", ApprovalResponse(approved=True))

        asyncio.create_task(_resolve())
        await broker.submit(request, timeout_s=5)

        # Already resolved and removed from pending
        result = broker.resolve("req-004", ApprovalResponse(approved=True))
        assert result is False

    async def test_pending_list(self):
        from miragen.broker import ApprovalBroker
        import asyncio

        broker = ApprovalBroker()
        request1 = ApprovalRequest(
            agent_name="a", tool_name="t1", tool_args={}, request_id="r1",
        )
        request2 = ApprovalRequest(
            agent_name="a", tool_name="t2", tool_args={}, request_id="r2",
        )

        # Submit two requests without resolving them
        task1 = asyncio.create_task(broker.submit(request1, timeout_s=60))
        task2 = asyncio.create_task(broker.submit(request2, timeout_s=60))
        await asyncio.sleep(0)  # Let tasks register

        pending = broker.pending()
        assert len(pending) == 2
        pending_ids = {p.request_id for p in pending}
        assert "r1" in pending_ids
        assert "r2" in pending_ids

        # Clean up
        broker.resolve("r1", ApprovalResponse(approved=True))
        broker.resolve("r2", ApprovalResponse(approved=True))
        await task1
        await task2


# ── Strict mode ───────────────────────────────────────────────────────────────

class TestApprovalGateStrictMode:
    async def test_strict_mode_denies_when_unconfigured(self):
        profile = _profile(approval_required=["delete_*"], approval_mode="strict")
        call = _mock_call("delete_file")
        handler = AsyncMock(return_value="deleted")
        factory_module._approval_handler = None

        with pytest.raises(ModelRetry, match="unconfigured"):
            await _run_approval_gate(profile, call, {}, handler)

        handler.assert_not_awaited()

    async def test_strict_mode_passes_when_handler_configured(self):
        profile = _profile(approval_required=["delete_*"], approval_mode="strict")
        call = _mock_call("delete_file")
        handler = AsyncMock(return_value="deleted")

        factory_module._approval_handler = AsyncMock(
            return_value=ApprovalResponse(approved=True)
        )

        result = await _run_approval_gate(profile, call, {}, handler)
        assert result == "deleted"
