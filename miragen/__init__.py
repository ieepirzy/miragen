"""
miragen — YAML-defined agent orchestration framework powered by PydanticAI.

Typical usage
─────────────
    # tools.py
    from miragen import register, register_capability, register_handler

    @register
    async def my_tool(ctx, arg: str) -> str:
        ...

    @register_capability("MyMemory")
    def _(cfg):
        return MyMemoryCapability(cfg.get("size", 1000))

    @register_handler("miradb")
    async def _(agent_name: str, output: str) -> None:
        ...

    @register_approval_handler
    async def _(request: ApprovalRequest) -> ApprovalResponse:
        approved = await ask_user(request.tool_name, request.tool_args)
        return ApprovalResponse(approved=approved)

Then run with:
    miragen run
    miragen validate agents/morning.yaml
"""

from miragen.factory import register, register_handler, register_approval_handler
from miragen.load import register_capability
from miragen.models import ApprovalRequest, ApprovalResponse

__all__ = [
    "register",
    "register_capability",
    "register_handler",
    "register_approval_handler",
    "ApprovalRequest",
    "ApprovalResponse",
]