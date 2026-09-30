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


def parse_approval_rule(rule: str) -> tuple[str, str | None, bool, list[str]]:
    """``tool_glob`` or ``tool_glob:arg=g1|g2`` (gated when the argument
    matches one of the globs) or ``tool_glob:arg!=g1|g2`` (gated unless it
    does — the fail-closed form, e.g. "every CRM call except reads").

    Returns (tool_glob, arg or None, negated, value_globs)."""
    if ":" not in rule:
        return rule, None, False, []
    tool, cond = rule.split(":", 1)
    negated = "!=" in cond
    arg, _, values = cond.partition("!=" if negated else "=")
    arg, globs = arg.strip(), [v.strip() for v in values.split("|") if v.strip()]
    if not tool or not arg or not globs:
        raise ValueError(f"approval rule {rule!r}: expected 'tool', 'tool:arg=glob|glob' or "
                         "'tool:arg!=glob|glob'")
    return tool, arg, negated, globs


def approval_gated(profile: AgentProfile, *tool_names: str, args: dict | None = None) -> bool:
    """Does this call match an approval_required rule? A rule with an
    argument condition gates on the argument too; a call whose argument is
    missing or not a string is gated (fail closed)."""
    for rule in profile.approval_required or []:
        tool, arg, negated, globs = parse_approval_rule(rule)
        if not any(fnmatch.fnmatch(name, tool) for name in tool_names):
            continue
        if arg is None:
            return True
        value = (args or {}).get(arg)
        if not isinstance(value, str):
            return True
        hit = any(fnmatch.fnmatch(value, g) for g in globs)
        if hit != negated:
            return True
    return False


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
    call_args = call.args_as_dict() or {}
    if not approval_gated(profile, tool_name, args=call_args):
        return await handler(args)
    try:
        response = await decide_approval(profile, tool_name, call_args)
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
