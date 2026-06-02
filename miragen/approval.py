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


async def _run_approval_gate(
    profile: AgentProfile,
    call: Any,
    args: Any,
    handler: Any,
) -> Any:
    """
    Core approval gate logic — separated from the hook wrapper for testability.

    Checks whether `call.tool_name` matches any glob in `profile.approval_required`.
    If it does, dispatches to the registered handler or webhook and either:
      - Raises ModelRetry if the tool call is denied
      - Returns the tool result (optionally prefixed with an approver note)

    If no handler and no webhook are configured, logs a warning and auto-approves
    (fail open — an unconfigured gate should not silently break agents).
    """
    tool_name = call.tool_name
    patterns = profile.approval_required or []

    if not any(fnmatch.fnmatch(tool_name, p) for p in patterns):
        return await handler(args)

    # Lazy import to avoid circular dependency at module load time
    from miragen.factory import get_approval_handler

    request = ApprovalRequest(
        agent_name=profile.name,
        tool_name=tool_name,
        tool_args=call.args_as_dict() or {},
        request_id=str(uuid.uuid4()),
    )

    handler_fn = get_approval_handler()

    if handler_fn is None and profile.approval_webhook is None:
        logger.warning(
            f"[{profile.name}] Tool '{tool_name}' matches approval_required but no "
            f"handler or webhook is configured — auto-approving (fail open)."
        )
        return await handler(args)

    if handler_fn is not None:
        response: ApprovalResponse = await handler_fn(request)
    else:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                str(profile.approval_webhook),
                json=request.model_dump(),
            )
            resp.raise_for_status()
            response = ApprovalResponse.model_validate(resp.json())

    if not response.approved:
        raise ModelRetry(
            f"Tool call '{tool_name}' was not approved."
            + (f" Reason: {response.prompt}" if response.prompt else "")
        )

    result = await handler(args)

    if response.prompt:
        return f"[Approver note: {response.prompt}]\n{result}"  # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string

    return result


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
