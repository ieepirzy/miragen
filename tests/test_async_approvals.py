"""approval_delivery: async — a gated gateway call answers at once, and the
approval (POST /approvals/{id}) runs it later, once, with frozen arguments."""

from __future__ import annotations

import asyncio
import json
import textwrap

import pytest

import miragen.app as app_module
from miragen.broker import get_broker
from miragen.harness.gateway import ToolGateway, approval_code
from miragen.load import load_profile
from miragen.models import AgentProfile, ApprovalResponse

CALLS: list[dict] = []


def book(slot: str, note: dict | None = None) -> str:
    """Book a slot."""
    CALLS.append({"slot": slot, "note": note})
    return json.dumps({"booked": slot})


def profile(**extra) -> AgentProfile:
    return AgentProfile.model_validate({
        "name": "mira", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "codex:gpt-6-luna", "instructions": "x"},
        "approval_required": ["book"], "approval_mode": "queue",
        "approval_delivery": "async", "approval_timeout_s": 60, **extra,
    })


@pytest.fixture(autouse=True)
def _clean():
    CALLS.clear()
    yield
    CALLS.clear()


def _pending(request_id: str):
    return next(p for p in get_broker().pending() if p.request.request_id == request_id)


async def _ask(gateway: ToolGateway, args: dict, instance="chat", run_id="run-1"):
    with gateway.turn(instance, run_id) as log:
        result = await gateway.call_tool("book", args, instance=instance)
    body = json.loads(result.content[0].text)
    return body, log, body and next(
        p.request.request_id for p in get_broker().pending()
        if approval_code(p.request.request_id) == body["code"])


async def test_a_gated_call_answers_at_once_and_does_not_run():
    gw = ToolGateway(profile(), runtime_tools=[book])
    body, log, rid = await _ask(gw, {"slot": "10:00"})
    assert body["status"] == "approval_pending" and "NOT run yet" in body["message"]
    assert CALLS == []
    assert [(c.tool_name, c.ok, c.result_status) for c in log.calls] == [
        ("book", True, "approval_pending")]
    assert _pending(rid).delivery == "async"
    await get_broker().resolve_async(rid, ApprovalResponse(approved=False))


async def test_approval_runs_it_once_with_the_frozen_arguments_after_the_turn():
    gw = ToolGateway(profile(), runtime_tools=[book])
    args = {"slot": "10:00", "note": {"who": "ilari"}}
    _body, _log, rid = await _ask(gw, args)
    args["slot"] = "23:00"          # the caller's dict changes afterwards…
    args["note"]["who"] = "someone"  # …nested too
    # the turn is over (gateway.turn exited); async approvals still run
    outcome = await get_broker().resolve_async(rid, ApprovalResponse(approved=True, prompt="ok"))
    assert CALLS == [{"slot": "10:00", "note": {"who": "ilari"}}]
    assert outcome["executed"] and outcome["ok"] and outcome["approved"]
    assert json.loads(outcome["result_text"]) == {"booked": "10:00"}
    assert outcome["run_id"] == "run-1" and outcome["instance"] == "chat"
    assert outcome["approver_note"] == "ok" and outcome["tool_name"] == "book"
    # never twice, by either door
    assert await get_broker().resolve_async(rid, ApprovalResponse(approved=True)) is None
    assert get_broker().resolve(rid, ApprovalResponse(approved=True)) is False
    assert len(CALLS) == 1
    assert get_broker().outcome(rid)["executed"] is True


async def test_a_denied_or_expired_call_never_runs():
    gw = ToolGateway(profile(), runtime_tools=[book])
    _b, _l, rid = await _ask(gw, {"slot": "1"})
    outcome = await get_broker().resolve_async(rid, ApprovalResponse(approved=False, prompt="no"))
    assert outcome["executed"] is False and outcome["approved"] is False and outcome["reason"] == "no"

    gw = ToolGateway(profile(approval_timeout_s=1), runtime_tools=[book])
    _b, _l, rid2 = await _ask(gw, {"slot": "2"})
    await asyncio.sleep(1.3)
    assert rid2 not in [p.request.request_id for p in get_broker().pending()]
    assert await get_broker().resolve_async(rid2, ApprovalResponse(approved=True)) is None
    assert get_broker().outcome(rid2)["expired"] is True
    assert CALLS == []


async def test_a_failing_approved_call_is_reported_not_retried():
    def book(slot: str) -> str:
        """Book."""
        CALLS.append({"slot": slot})
        raise RuntimeError("upstream down")

    gw = ToolGateway(profile(), runtime_tools=[book])
    _b, _l, rid = await _ask(gw, {"slot": "3"})
    outcome = await get_broker().resolve_async(rid, ApprovalResponse(approved=True))
    assert outcome["executed"] is True and outcome["ok"] is False
    assert "upstream down" in outcome["result_text"]
    assert len(CALLS) == 1


async def test_the_endpoint_returns_the_outcome_and_404s_a_second_answer():
    from fastapi import HTTPException

    gw = ToolGateway(profile(), runtime_tools=[book])
    _b, _l, rid = await _ask(gw, {"slot": "9"})
    answer = await app_module.resolve_approval(rid, ApprovalResponse(approved=True))
    assert answer.resolved and answer.outcome["executed"] and CALLS == [{"slot": "9", "note": None}]
    with pytest.raises(HTTPException) as err:
        await app_module.resolve_approval(rid, ApprovalResponse(approved=True))
    assert err.value.status_code == 404
    assert (await app_module.approval_outcome(rid))["request_id"] == rid
    assert len(CALLS) == 1


async def test_blocking_mode_is_unchanged():
    gw = ToolGateway(profile(approval_delivery="blocking"), runtime_tools=[book])
    with gw.turn("chat", "run-1"):
        task = asyncio.create_task(gw.call_tool("book", {"slot": "5"}, instance="chat"))
        await asyncio.sleep(0.05)
        rid = next(p.request.request_id for p in get_broker().pending()
                   if p.request.tool_args == {"slot": "5"})
        assert _pending(rid).delivery == "blocking" and not get_broker().is_async(rid)
        assert get_broker().resolve(rid, ApprovalResponse(approved=True))
        result = await task
    assert json.loads(result.content[0].text) == {"booked": "5"}


def test_async_needs_queue_mode_and_a_gateway_harness(tmp_path):
    with pytest.raises(ValueError, match="approval_mode: queue"):
        profile(approval_mode="strict")
    path = tmp_path / "agent.yaml"
    path.write_text(textwrap.dedent("""
        name: a
        mode: interactive
        triggers: [{type: http}]
        spec: {model: "anthropic:claude-sonnet-5", instructions: x}
        approval_required: [book]
        approval_mode: queue
        approval_delivery: async
    """))
    with pytest.raises(ValueError, match="tool-gateway harnesses"):
        load_profile(path)


async def test_the_blocking_resolver_cannot_consume_an_async_request():
    gw = ToolGateway(profile(), runtime_tools=[book])
    _b, _l, rid = await _ask(gw, {"slot": "7"})
    assert get_broker().resolve(rid, ApprovalResponse(approved=True)) is False
    assert _pending(rid).delivery == "async"  # still there, still runnable
    outcome = await get_broker().resolve_async(rid, ApprovalResponse(approved=True))
    assert outcome["executed"] and CALLS == [{"slot": "7", "note": None}]
