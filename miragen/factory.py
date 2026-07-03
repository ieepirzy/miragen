from __future__ import annotations

from collections.abc import Callable
from typing import Any, overload

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from miragen.approval import build_approval_hooks
from miragen.load import resolve_capabilities
from miragen.models import AgentProfile


# ── Tool registry ────────────────────────────────────────────────────────────
#
# Populated via @register. Do not edit directly.
#
# NOTE: MCP tools are NOT registered here — they are injected automatically
# when an MCP server is listed under spec.capabilities in the agent profile.
# This registry is only for local Python functions.

_TOOL_REGISTRY: dict[str, Any] = {}
_HANDLER_REGISTRY: dict[str, Callable] = {}
_approval_handler: Callable | None = None


@overload
def register(fn: Callable) -> Callable: ...
@overload
def register(name: str) -> Callable[[Callable], Callable]: ...

def register(fn: Callable | str) -> Any:
    """
    Register a local Python function as an injectable agent tool.

    Usage:
        @register
        def my_tool(ctx, arg: str) -> str: ...

        @register("custom_name")
        def my_tool(ctx, arg: str) -> str: ...
    """
    if callable(fn):
        _TOOL_REGISTRY[fn.__name__] = fn
        return fn

    name = fn
    def decorator(f: Callable) -> Callable:
        _TOOL_REGISTRY[name] = f
        return f

    return decorator


def registered_tools() -> dict[str, Any]:
    """Return a read-only view of the current tool registry."""
    return dict(_TOOL_REGISTRY)


def register_handler(name: str) -> Callable[[Callable], Callable]:
    """
    Register an on_complete output handler by name.

    Usage:
        @register_handler("miradb")
        async def _(agent_name: str, output: str) -> None:
            ...
    """
    def decorator(fn: Callable) -> Callable:
        _HANDLER_REGISTRY[name] = fn
        return fn
    return decorator


def registered_handlers() -> dict[str, Callable]:
    """Return a read-only view of the current handler registry."""
    return dict(_HANDLER_REGISTRY)


# ── Approval handler ─────────────────────────────────────────────────────────
#
# Single slot — only one approval handler per container.
# Takes precedence over approval_webhook in the profile.

def register_approval_handler(fn: Callable) -> Callable:
    """
    Register the approval handler for human-in-the-loop tool gating.

    The handler receives an ApprovalRequest and must return an ApprovalResponse.
    Only one handler can be registered at a time.

    Usage:
        @register_approval_handler
        async def _(request: ApprovalRequest) -> ApprovalResponse:
            approved = await ask_telegram(request)
            return ApprovalResponse(approved=approved)
    """
    global _approval_handler
    _approval_handler = fn
    return fn


def get_approval_handler() -> Callable | None:
    """Return the currently registered approval handler, or None."""
    return _approval_handler


# ── Factory ──────────────────────────────────────────────────────────────────

def build_agent(profile: AgentProfile) -> tuple[Agent, UsageLimits | None]:
    """
    Construct a live PydanticAI Agent from a validated AgentProfile.

    Resolves capabilities, injects whitelisted tools, applies usage limits.
    MCP tools are handled automatically via the MCP capability — they do
    not need to appear in the profile's tools list.
    """
    capabilities = resolve_capabilities(profile.spec.capabilities or [])

    approval_hooks = build_approval_hooks(profile)
    if approval_hooks is not None:
        capabilities.append(approval_hooks)

    limits_kwargs: dict[str, int] = {}
    if profile.spec.max_steps:
        limits_kwargs["request_limit"] = profile.spec.max_steps
    if profile.limits and profile.limits.tokens_per_run:
        limits_kwargs["total_tokens_limit"] = profile.limits.tokens_per_run
    limits = UsageLimits(**limits_kwargs) if limits_kwargs else None

    model_settings: ModelSettings | None = None
    if profile.spec.model_settings:
        ms: ModelSettings = {}
        if profile.spec.model_settings.max_tokens:
            ms["max_tokens"] = profile.spec.model_settings.max_tokens
        if profile.spec.model_settings.temperature is not None:
            ms["temperature"] = profile.spec.model_settings.temperature
        model_settings = ms or None

    agent = Agent(
        model=profile.spec.model,
        instructions=profile.spec.instructions,
        capabilities=capabilities,
        model_settings=model_settings,
    )

    _inject_tools(agent, profile)

    return agent, limits


def _inject_tools(agent: Agent, profile: AgentProfile) -> None:
    """
    Register whitelisted local tools onto the agent.

    If `tools` is None, no local tools are injected.
    Unknown tool names raise immediately rather than silently skipping.
    """
    if not profile.tools:
        return

    unknown = [t for t in profile.tools if t not in _TOOL_REGISTRY]
    if unknown:
        raise ValueError(
            f"Agent '{profile.name}' references unknown tools: {unknown}. "
            f"Registered: {sorted(_TOOL_REGISTRY)}"
        )

    for tool_name in profile.tools:
        agent.tool(_TOOL_REGISTRY[tool_name])