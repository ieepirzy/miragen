from __future__ import annotations

import os
import re
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
from miragen.peer import build_peer_capability


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
    "MCP":             lambda cfg: _build_mcp(cfg),
    "Peer":            lambda cfg: build_peer_capability(cfg),
}


# Config keys the MCP capability accepts from a profile.
#
# Declared explicitly because the registry entries above pick keys by name: a
# key nobody names is dropped without a word. That is precisely how
# `allowed_tools` and `defer_loading` stayed unreachable from a profile even
# though PydanticAI has accepted both all along — no error, no warning, just a
# capability quietly built without them (issue #70). Naming the accepted set
# means the next key to go missing fails loudly instead.
_MCP_CONFIG_KEYS = frozenset(
    {"url", "name", "allowed_tools", "defer_loading", "authorization_token"}
)


def _build_mcp(cfg: dict) -> Any:
    """Build the MCP capability, passing through per-agent tool scoping.

    `allowed_tools` restricts which of the server's tools this agent may call,
    and is what makes one shared, domain-scoped MCP server usable by several
    agents with different grants. It is enforced client-side, in the agent
    process: it shapes what the model is offered, and is NOT an authorization
    boundary. Anything that must hold against a determined caller belongs in
    the MCP server's own auth.

    `defer_loading` hides the server's tools from the model until it discovers
    them through tool search, so an agent attached to several servers does not
    pay every schema's context cost on every request.
    """
    unknown = sorted(set(cfg) - _MCP_CONFIG_KEYS)
    if unknown:
        raise ValueError(
            f"Unknown MCP capability config key(s): {unknown}. "
            f"Accepted: {sorted(_MCP_CONFIG_KEYS)} — plus 'bearer_token_env', "
            "which resolve_capabilities turns into 'authorization_token' before "
            "reaching here."
        )

    allowed = cfg.get("allowed_tools")
    if allowed is not None and (
        not isinstance(allowed, list) or not all(isinstance(t, str) for t in allowed)
    ):
        raise ValueError(
            f"MCP 'allowed_tools' must be a list of tool-name strings, got: {allowed!r}"
        )

    defer = cfg.get("defer_loading", False)
    if not isinstance(defer, bool):
        raise ValueError(f"MCP 'defer_loading' must be true or false, got: {defer!r}")

    return MCP(
        url=cfg["url"],
        id=cfg.get("name"),
        native=True,
        authorization_token=cfg.get("authorization_token"),
        allowed_tools=allowed,
        defer_loading=defer,
    )


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


def resolve_capabilities(
    raw: list[str | dict],
    *,
    secret_env: dict[str, str] | None = None,
) -> list[AbstractCapability[Any]]:
    """
    Turn the raw YAML capability list into instantiated PydanticAI capability objects.

    Accepts both forms:
        - "WebSearch"                      → WebSearch()
        - {"Thinking": {"effort": "high"}} → Thinking(effort="high")

    A capability config may name a credential indirectly with
    `bearer_token_env` rather than carrying the secret in the profile. It is
    resolved here into `authorization_token`, checking `secret_env` (this
    run's ephemeral values) before the deployment-level environment — the
    same precedence the executor adapters use for their own
    `bearer_token_env` lookup, so a run-scoped credential wins over the
    static one and a profile with neither still loads.
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

        if "bearer_token_env" in cfg:
            cfg = {k: v for k, v in cfg.items() if k != "bearer_token_env"}
            env_name = entry[name]["bearer_token_env"]
            token = (secret_env or {}).get(env_name) or os.environ.get(env_name)
            if token:
                cfg["authorization_token"] = token

        resolved.append(_CAPABILITY_REGISTRY[name](cfg))

    return resolved


# ── Environment interpolation ────────────────────────────────────────────────
#
# ${VAR} and ${VAR:-default} — the two POSIX forms people expect, nothing else.
# $${VAR} escapes to a literal "${VAR}". Applied to string values only,
# recursively through the whole document (dict values, list items — never
# dict keys), between YAML parse and Pydantic validation.

_ENV_RE = re.compile(r"\$\$\{|\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def interpolate_env(value: Any, path: str = "") -> Any:
    """Recursively substitute ${VAR}/${VAR:-default} in string values."""
    if isinstance(value, dict):
        return {
            key: interpolate_env(v, f"{path}.{key}" if path else str(key))
            for key, v in value.items()
        }
    if isinstance(value, list):
        return [interpolate_env(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, str):
        def _sub(m: re.Match[str]) -> str:
            if m.group(0) == "$${":
                return "${"
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ValueError(
                f"profile references undefined environment variable '{name}' "
                f"(at {path}); set it or use ${{{name}:-default}}"
            )
        return _ENV_RE.sub(_sub, value)
    return value


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

    raw = interpolate_env(raw)

    # Validate + coerce via Pydantic
    profile = AgentProfile.model_validate(raw)

    # Eagerly resolve capabilities so we catch unknown names at load time
    # rather than at agent construction time (model tier only — executor
    # profiles carry no capability list; the executor's tools are its own)
    if profile.spec is not None and profile.spec.capabilities:
        resolve_capabilities(profile.spec.capabilities)

    return profile