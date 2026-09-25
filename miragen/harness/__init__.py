"""Base-tier harnesses (docs/design/harnesses.md)."""

from __future__ import annotations

import os
from pathlib import Path

from miragen.harness.base import (
    PYDANTIC_AI, Harness, HarnessResult, HarnessStream, HarnessTurn, parse_harness_model,
)
from miragen.harness.pydantic_ai import PydanticAIHarness
from miragen.models import AgentProfile

__all__ = [
    "PYDANTIC_AI", "Harness", "HarnessResult", "HarnessStream", "HarnessTurn",
    "PydanticAIHarness", "build_model_harness", "parse_harness_model", "profile_harness",
    "pydantic_ai_model",
]


def profile_harness(profile: AgentProfile) -> str:
    """Which harness runs this profile's base-tier turns ('pydantic-ai' for
    executor-tier profiles too — they have no base-tier turns)."""
    if profile.spec is None:
        return PYDANTIC_AI
    return parse_harness_model(profile.spec.model)[0]


def pydantic_ai_model(profile: AgentProfile) -> str | None:
    """spec.model when it is a pydantic-ai model string, else None.

    For fallbacks that hand spec.model to PydanticAI (the memory recall
    selector, the extraction worker): a harness model such as
    ``grok-build:grok-4.6`` is not something PydanticAI can call, so those
    features need their own explicit model on such profiles."""
    if profile.spec is None or profile_harness(profile) != PYDANTIC_AI:
        return None
    return profile.spec.model


def build_model_harness(
    profile: AgentProfile,
    *,
    runs_root: Path,
    runtime_tools: list | None = None,
    registered_tools: dict | None = None,
    bind_context=None,
    gateway_url: str | None = None,
    system_guidance: str | None = None,
):
    """A long-lived non-PydanticAI harness for this profile, plus the tool
    gateway it acts through: (harness, gateway)."""
    name = profile_harness(profile)
    if name == "grok-build":
        from miragen.harness.gateway import ToolGateway
        from miragen.harness.grok import GrokHarness, GrokSettings

        gateway = ToolGateway(profile, runtime_tools=runtime_tools,
                              registered_tools=registered_tools, bind_context=bind_context,
                              native_capabilities=GrokHarness.native_capabilities)
        url = gateway_url or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/mcp/gateway/"
        return GrokHarness(profile, gateway, GrokSettings.from_env(gateway_url=url),
                           system_guidance=system_guidance), gateway
    raise ValueError(f"harness '{name}' is not available in this build")
