"""Load-time rules for public-facing profiles (`agent_type: external | hybrid`).

A profile declares who it serves. `internal` (the default) carries no extra
rules. `external` talks to the public — e.g. a customer-facing chat without
account backing — and `hybrid` serves both, so it is held to the same rules:

- base tier only (`spec:`): an executor runs its own loop on its own
  credentials, outside anything checked here;
- every model it names is a pydantic-ai ``provider:model`` string whose
  provider is in the deployment's allowed list (a cloud ``voice.provider``
  too, by name), read from
  ``MIRAGEN_EXTERNAL_ALLOWED_PROVIDERS``. An unset or empty list refuses
  every external/hybrid profile: no list is not "anything goes";
- subscription harness models (``claude-code:``, ``codex:``,
  ``grok-build:``) are refused even if the list names them — they run on a
  personal login, not a deployment's API account;
- no model is written as a ``${VAR}`` interpolation: the value is only known
  once the environment is, so it cannot be checked at validation time;
- no ``memory:`` block: a public-facing agent keeps no long-term memory of
  the people it talks to.

The rules run in the loader (`miragen.load.load_profile`), so `miragen run`
refuses to start and miragend's `/validate` returns the error. The allowed
list is a parameter so callers with their own environment mapping (miragend)
and tests pass it in; `None` reads the process environment.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

from miragen.harness.base import HARNESS_PREFIXES
from miragen.models import AgentProfile

ALLOWED_PROVIDERS_ENV = "MIRAGEN_EXTERNAL_ALLOWED_PROVIDERS"

PUBLIC_AGENT_TYPES = frozenset({"external", "hybrid"})


def parse_allowed_providers(raw: str | Iterable[str] | None) -> frozenset[str]:
    """``" Anthropic, openai ,"`` -> ``{"anthropic", "openai"}``: trimmed,
    lowercased, empties dropped."""
    if raw is None:
        return frozenset()
    items = raw.split(",") if isinstance(raw, str) else raw
    return frozenset(p.strip().lower() for p in items if p and p.strip())


def allowed_providers_from_env(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    env = os.environ if environ is None else environ
    return parse_allowed_providers(env.get(ALLOWED_PROVIDERS_ENV))


def _model_fields(profile: AgentProfile) -> list[tuple[str, tuple, Any]]:
    """(display path, raw-document path, value) for every model a base-tier
    profile names. memory.*.model is absent on purpose: a memory block is
    refused outright for public-facing profiles, before models are checked."""
    assert profile.spec is not None
    fields: list[tuple[str, tuple, Any]] = [
        ("spec.model", ("spec", "model"), profile.spec.model)
    ]
    for i, entry in enumerate(profile.spec.capabilities or []):
        if isinstance(entry, dict) and len(entry) == 1:
            name, cfg = next(iter(entry.items()))
            cfg = cfg or {}
            if name == "ImageGeneration" and isinstance(cfg, dict) \
                    and cfg.get("fallback_model") is not None:
                fields.append((
                    f"spec.capabilities[{i}].ImageGeneration.fallback_model",
                    ("spec", "capabilities", i, "ImageGeneration", "fallback_model"),
                    cfg["fallback_model"],
                ))
    return fields


def _lookup(doc: Any, path: tuple) -> Any:
    for key in path:
        try:
            doc = doc[key]
        except (KeyError, IndexError, TypeError):
            return None
    return doc


def _is_interpolated(value: Any) -> bool:
    # Same grammar the loader expands; a `$${` escape is a literal, not a
    # substitution.
    from miragen.load import _ENV_RE

    return isinstance(value, str) and any(
        m.group(0) != "$${" for m in _ENV_RE.finditer(value)
    )


def check_agent_type(
    profile: AgentProfile,
    allowed_providers: Iterable[str] | str | None,
    *,
    uninterpolated: Mapping[str, Any] | None = None,
) -> None:
    """Raise ValueError if a public-facing profile breaks a rule; return
    otherwise. Internal profiles are never checked.

    `allowed_providers`: the deployment's list (already parsed, or the raw
    comma-separated string). `uninterpolated`: the profile document as
    written, before ``${VAR}`` expansion — the only place an interpolated
    model is still visible."""
    if profile.agent_type not in PUBLIC_AGENT_TYPES:
        return
    who = f"agent_type: {profile.agent_type}"

    allowed = parse_allowed_providers(allowed_providers)
    if not allowed:
        raise ValueError(
            f"{who} needs the deployment's allowed-provider list: set "
            f"{ALLOWED_PROVIDERS_ENV} to comma-separated provider names "
            "(e.g. 'anthropic,openai'). Unset or empty refuses every "
            "external/hybrid profile."
        )

    if profile.spec is None:  # i.e. an executor profile (exactly one backend)
        raise ValueError(
            f"executor: {who} profiles must be base tier (`spec:`); an "
            "executor profile runs its own loop on its own credentials"
        )

    if profile.memory is not None:
        raise ValueError(
            f"memory: {who} profiles may not have a `memory:` block; a "
            "public-facing agent keeps no long-term memory of the people it "
            "talks to"
        )

    for display, raw_path, value in _model_fields(profile):
        if uninterpolated is not None and _is_interpolated(_lookup(uninterpolated, raw_path)):
            raise ValueError(
                f"{display}: {who} profiles may not set a model with ${{VAR}} "
                "interpolation; it cannot be checked against "
                f"{ALLOWED_PROVIDERS_ENV} until it is resolved — write it literally"
            )
        if not isinstance(value, str):
            raise ValueError(f"{display}: {who} needs a 'provider:model' string, got {value!r}")
        for prefix in HARNESS_PREFIXES:
            if value.startswith(prefix):
                raise ValueError(
                    f"{display}: {who} profiles may not use the subscription harness "
                    f"model {value!r}; use a pydantic-ai provider:model string"
                )
        provider, sep, _ = value.partition(":")
        if not sep or not provider.strip():
            raise ValueError(
                f"{display}: {who} needs a pydantic-ai 'provider:model' string, got {value!r}"
            )
        if provider.strip().lower() not in allowed:
            raise ValueError(
                f"{display}: provider {provider!r} is not in {ALLOWED_PROVIDERS_ENV} "
                f"({', '.join(sorted(allowed))}); {who} profiles may only use "
                "allowed providers"
            )

    _check_capabilities(profile, who)
    _check_voice(profile, allowed, who, uninterpolated)


def _capability_entries(profile: AgentProfile) -> list[tuple[int, str, dict]]:
    """(index, name, config) for each spec capability, in both YAML forms.
    Malformed entries are skipped here: the loader's own capability
    resolution already refused them."""
    out = []
    for i, entry in enumerate(profile.spec.capabilities or []):
        if isinstance(entry, str):
            out.append((i, entry, {}))
        elif isinstance(entry, dict) and len(entry) == 1:
            name, cfg = next(iter(entry.items()))
            out.append((i, name, cfg if isinstance(cfg, dict) else {}))
    return out


def _check_capabilities(profile: AgentProfile, who: str) -> None:
    for i, name, _cfg in _capability_entries(profile):
        where = f"spec.capabilities[{i}].{name}"
        if name == "Peer":
            raise ValueError(
                f"{where}: {who} profiles may not use the Peer capability; "
                "call_agent would forward the public user's text to another "
                "agent that is not held to these rules"
            )


def _check_voice(
    profile: AgentProfile,
    allowed: frozenset[str],
    who: str,
    uninterpolated: Mapping[str, Any] | None,
) -> None:
    """A cloud speech provider receives everything the agent says aloud, so
    it is held to the same list (by its provider name, e.g. 'openai'). The
    'http' provider is the deployment's own endpoint and is not checked."""
    voice = profile.voice
    if voice is None:
        return
    if uninterpolated is not None and _is_interpolated(
        _lookup(uninterpolated, ("voice", "provider"))
    ):
        raise ValueError(
            f"voice.provider: {who} profiles may not set the speech provider with "
            "${VAR} interpolation; write it literally"
        )
    if voice.provider != "http" and voice.provider.lower() not in allowed:
        raise ValueError(
            f"voice.provider: speech provider {voice.provider!r} is not in "
            f"{ALLOWED_PROVIDERS_ENV} ({', '.join(sorted(allowed))}); {who} "
            "profiles may only use allowed providers"
        )
