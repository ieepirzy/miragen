from __future__ import annotations

import fnmatch
import logging
import uuid
from typing import Any

import httpx
from pydantic_ai.capabilities import Hooks
from pydantic_ai.exceptions import ModelRetry

from miragen.models import AgentProfile, ApprovalRequest, ApprovalResponse

logger = logging.getLogger(__name__)


class ApprovalDenied(Exception):
    """A gated tool call was not approved. The message is what the model is
    told; each harness surfaces it its own way (PydanticAI: ModelRetry; the
    tool gateway: an MCP tool error)."""


def approval_gated(profile: AgentProfile, *tool_names: str) -> bool:
    """Does any of these names match an approval_required glob?"""
    patterns = profile.approval_required or []
    return any(fnmatch.fnmatch(name, p) for name in tool_names for p in patterns)


async def decide_approval(
    profile: AgentProfile, tool_name: str, tool_args: dict[str, Any],
) -> ApprovalResponse:
    """Harness-neutral approval decision for a call that IS gated.

    Precedence: registered handler > approval_webhook > approval_mode. An
    approved response may carry an approver note (`prompt`); a denial raises
    ApprovalDenied with the reason the model should see."""
    # Lazy import to avoid circular dependency at module load time
    from miragen.factory import get_approval_handler

    request = ApprovalRequest(
        agent_name=profile.name,
        tool_name=tool_name,
        tool_args=tool_args,
        request_id=str(uuid.uuid4()),
    )

    handler_fn = get_approval_handler()

    if handler_fn is not None:
        response: ApprovalResponse = await handler_fn(request)
    elif profile.approval_webhook is not None:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                str(profile.approval_webhook),
                json=request.model_dump(),
            )
            resp.raise_for_status()
            response = ApprovalResponse.model_validate(resp.json())
    else:
        response = await _unconfigured_gate_response(profile, tool_name, request)

    if not response.approved:
        raise ApprovalDenied(
            f"Tool call '{tool_name}' was not approved."
            + (f" Reason: {response.prompt}" if response.prompt else "")
        )
    return response


def with_approver_note(response: ApprovalResponse, result: Any) -> Any:
    if response.prompt:
        return f"[Approver note: {response.prompt}]\n{result}"  # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string
    return result


async def _run_approval_gate(
    profile: AgentProfile,
    call: Any,
    args: Any,
    handler: Any,
) -> Any:
    """
    The PydanticAI hook's use of the gate: pass through ungated calls; for
    gated ones, deny with ModelRetry or run the tool (prefixing any approver
    note). The decision itself is decide_approval, shared with the tool
    gateway.
    """
    tool_name = call.tool_name
    if not approval_gated(profile, tool_name):
        return await handler(args)
    try:
        response = await decide_approval(profile, tool_name, call.args_as_dict() or {})
    except ApprovalDenied as exc:
        raise ModelRetry(str(exc)) from exc
    return with_approver_note(response, await handler(args))


async def _unconfigured_gate_response(
    profile: AgentProfile,
    tool_name: str,
    request: ApprovalRequest,
) -> ApprovalResponse:
    """
    What happens when a gated tool call has neither a registered handler nor an
    approval_webhook — governed by profile.approval_mode:
      - 'open' (default): auto-approve with a warning — unconfigured gates
        should not silently break agents during development.
      - 'strict': deny immediately.
      - 'queue': park the request in the ApprovalBroker for HTTP resolution via
        GET/POST /approvals; denies after approval_timeout_s if unresolved.
    """
    if profile.approval_mode == "strict":
        raise ApprovalDenied(
            f"Tool call '{tool_name}' denied: approval gate is unconfigured (approval_mode: strict)."
        )

    if profile.approval_mode == "queue":
        from miragen.broker import get_broker

        return await get_broker().submit(request, profile.approval_timeout_s)

    logger.warning(
        f"[{profile.name}] Tool '{tool_name}' matches approval_required but no "
        f"handler or webhook is configured — auto-approving (fail open)."
    )
    return ApprovalResponse(approved=True)


def build_approval_hooks(profile: AgentProfile) -> Hooks | None:
    """
    Build a Hooks capability that gates tool calls matching approval_required globs.
    Returns None if approval_required is not set or empty — no overhead added.
    """
    if not profile.approval_required:
        return None

    async def approval_gate(ctx: Any, /, *, call: Any, tool_def: Any, args: Any, handler: Any) -> Any:
        return await _run_approval_gate(profile, call, args, handler)

    return Hooks(tool_execute=approval_gate)
