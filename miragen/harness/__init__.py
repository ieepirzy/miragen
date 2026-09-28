"""Base-tier harnesses (docs/design/harnesses.md)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from miragen.harness.base import (
    PYDANTIC_AI, Harness, HarnessResult, HarnessStream, HarnessTurn, InstanceBusyError,
    parse_harness_model,
)
from miragen.harness.pydantic_ai import PydanticAIHarness
from miragen.models import AgentProfile

__all__ = [
    "PYDANTIC_AI", "Harness", "HarnessResult", "HarnessStream", "HarnessTurn",
    "InstanceBusyError", "PydanticAIHarness", "build_model_harness", "parse_harness_model", "profile_harness",
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
    session_key_for=None,
):
    """A long-lived non-PydanticAI harness for this profile, plus the tool
    gateway it acts through: (harness, gateway)."""
    name = profile_harness(profile)
    if name not in ("grok-build", "claude-code"):
        raise ValueError(f"harness '{name}' is not available in this build")
    from miragen.harness.gateway import ToolGateway
    from miragen.harness.served import ServedLedger

    if name == "grok-build":
        from miragen.harness.grok import GrokHarness as cls, GrokSettings as settings_cls
    else:
        from miragen.harness.claude_code import (
            ClaudeCodeHarness as cls, ClaudeCodeSettings as settings_cls,
        )
    gateway = ToolGateway(profile, runtime_tools=runtime_tools,
                          registered_tools=registered_tools, bind_context=bind_context,
                          native_capabilities=cls.native_capabilities,
                          session_key_for=session_key_for)
    url = gateway_url or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/mcp/gateway/"
    # Which harness last served each instance, shared by all of them, so a
    # swap of spec.model shows up as `fresh` (miragen/harness/served.py).
    served = ServedLedger(runs_root / "harness" / "served.json")
    # Conversations that predate the ledger were all Grok's: record that
    # once, so the first swap away from Grok already reads as a swap.
    seeded = served.seed("grok-build", Path(os.environ.get("MIRAGEN_GROK_HOME", "/agent/grok-home"))
                         / "miragen-instances.json")
    if seeded:
        logging.getLogger("miragen.harness").info(
            "harness ledger seeded with %d Grok instance(s)", seeded)
    return cls(profile, gateway, settings_cls.from_env(gateway_url=url),
               system_guidance=system_guidance, served=served), gateway
