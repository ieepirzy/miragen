from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic_ai.capabilities import (
    AbstractCapability,
    Thinking,
    WebSearch,
    WebFetch,
    ImageGeneration,
    MCP,
)

from miragen.models import AgentProfile


# ── Capability registry ──────────────────────────────────────────────────────
#
# Maps capability name strings from YAML → instantiated PydanticAI capability.
# register_capability extends this dict when you add new built-in or custom capabilities.
#
# Two forms in YAML:
#   - WebSearch              (string)  → registry["WebSearch"]({})
#   - Thinking:              (dict)    → registry["Thinking"]({"effort": "low"})
#       effort: low

_CAPABILITY_REGISTRY: dict[str, Any] = {
    "WebSearch":       lambda cfg: WebSearch(),
    "WebFetch":        lambda cfg: WebFetch(local=cfg.get("local", True)),
    "Thinking":        lambda cfg: Thinking(effort=cfg.get("effort", "medium")),
    "ImageGeneration": lambda cfg: ImageGeneration(
                           fallback_model=cfg.get("fallback_model")
                       ),
    "MCP":             lambda cfg: MCP(
                           url=cfg["url"],
                           id=cfg.get("name"),
                       ),
}


def register_capability(name: str) -> Callable[[Callable[[dict], Any]], Callable[[dict], Any]]:
    """
    Register a custom capability factory in the capability registry.

    Usage:
        @register_capability("MyMemory")
        def _(cfg):
            return MyMemoryCapability(cfg.get("size", 1000))
    """
    def decorator(factory: Callable[[dict], Any]) -> Callable[[dict], Any]:
        _CAPABILITY_REGISTRY[name] = factory
        return factory
    return decorator


def resolve_capabilities(raw: list[str | dict]) -> list[AbstractCapability[Any]]:
    """
    Turn the raw YAML capability list into instantiated PydanticAI capability objects.

    Accepts both forms:
        - "WebSearch"                      → WebSearch()
        - {"Thinking": {"effort": "high"}} → Thinking(effort="high")
    """
    resolved = []

    for entry in raw:
        if isinstance(entry, str):
            name, cfg = entry, {}
        elif isinstance(entry, dict):
            if len(entry) != 1:
                raise ValueError(
                    f"Capability dict must have exactly one key (the capability name), got: {entry}"
                )
            name, cfg = next(iter(entry.items()))
            cfg = cfg or {}
        else:
            raise ValueError(f"Unexpected capability format: {entry!r}")

        if name not in _CAPABILITY_REGISTRY:
            raise ValueError(
                f"Unknown capability '{name}'. "
                f"Built-in capabilities: {sorted(_CAPABILITY_REGISTRY)}. "
                f"For custom capabilities, use @register_capability('{name}') in your "
                f"tools.py — it must be imported before the agent profile is loaded."
            )

        resolved.append(_CAPABILITY_REGISTRY[name](cfg))

    return resolved


# ── Loader ───────────────────────────────────────────────────────────────────

def load_profile(path: str | Path) -> AgentProfile:
    """
    Load and validate an agent profile YAML file.

    Returns a fully validated AgentProfile. Raises on any schema violation
    or unknown capability.
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Agent profile not found: {path}")

    with path.open() as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Agent profile must be a YAML mapping, got: {type(raw).__name__}")

    # Validate + coerce via Pydantic
    profile = AgentProfile.model_validate(raw)

    # Eagerly resolve capabilities so we catch unknown names at load time
    # rather than at agent construction time
    if profile.spec.capabilities:
        resolve_capabilities(profile.spec.capabilities)

    return profile