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
  the people it talks to;
- a model on the ``anthropic`` provider is sent with the Messages API's
  ``inference_geo`` pinned to ``us`` (`PUBLIC_INFERENCE_GEO`; not
  configurable), applied when the agent is built
  (`miragen.factory.build_agent`), not at load;
- every outbound URL (MCP servers, an http voice endpoint,
  ``approval_webhook``, ``on_complete.post_to``) has its host in
  ``MIRAGEN_EXTERNAL_ALLOWED_HOSTS`` (exact hostnames; unset refuses any);
- only safe built-in capabilities (see PUBLIC_CAPABILITIES), no Peer, no
  custom capabilities and no local ``tools:``.

The rules run in the loader (`miragen.load.load_profile`), so `miragen run`
refuses to start and miragend's `/validate` returns the error. The allowed
list is a parameter so callers with their own environment mapping (miragend)
and tests pass it in; `None` reads the process environment.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from miragen.harness.base import HARNESS_PREFIXES
from miragen.models import AgentProfile

ALLOWED_PROVIDERS_ENV = "MIRAGEN_EXTERNAL_ALLOWED_PROVIDERS"
ALLOWED_HOSTS_ENV = "MIRAGEN_EXTERNAL_ALLOWED_HOSTS"
# Where Anthropic runs inference for a public-facing profile's model
# requests: the Messages API's `inference_geo`. Customer chats must stay under
# EU or US jurisdiction (mirarun ADR-036, Decision 5), and the owner's decision
# is that they run in the US. The API's only other value, "global", may process
# anywhere, and leaving the field out defers to the API workspace's default,
# which can be "global" too. So the value is fixed here rather than read from
# the environment: a deployment cannot widen it. Changing it is a code change
# that needs a transfer analysis for the new geography first.
PUBLIC_INFERENCE_GEO = "us"

PUBLIC_AGENT_TYPES = frozenset({"external", "hybrid"})


# Files the runtime (or a Python started in the agent directory, which is the
# container's working directory) can load as code: tools.py is imported at
# boot, and a module file in the working directory shadows an import for
# `python -c` (e.g. the image healthcheck).
CODE_SUFFIXES = frozenset({".py", ".pyc", ".so", ".pyd"})

# What a code-free tools.py may contain: docstrings/comments and the
# placeholder import miragend writes for a new agent.
_PLACEHOLDER_IMPORTS = frozenset({"register", "register_handler", "register_approval_handler"})


def is_code_free_python(source: str) -> bool:
    """True when `source` defines nothing: only string expressions and
    `from miragen import register` (the placeholder a new agent gets)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "miragen" and node.level == 0 \
                and all(a.name in _PLACEHOLDER_IMPORTS and a.asname is None for a in node.names):
            continue
        return False
    return True


def is_code_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in CODE_SUFFIXES


def code_violations(root: Path) -> list[str]:
    """Relative paths under an agent directory that would give a public
    agent custom code: any code file except a code-free top-level tools.py."""
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            full = Path(dirpath) / fname
            if not is_code_path(full):
                continue
            rel = full.relative_to(root).as_posix()
            if rel == "tools.py":
                try:
                    if is_code_free_python(full.read_text()):
                        continue
                except (OSError, UnicodeDecodeError):
                    pass
            found.append(rel)
    return sorted(found)


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


def allowed_hosts_from_env(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """Hostnames an external/hybrid profile's MCP servers and http voice
    endpoint may live on: exact names, no wildcards."""
    env = os.environ if environ is None else environ
    return parse_allowed_providers(env.get(ALLOWED_HOSTS_ENV))


def anthropic_request_extras(profile: AgentProfile) -> dict[str, str] | None:
    """Extra Messages API request fields for a public-facing profile whose
    model runs on the `anthropic` provider: `{"inference_geo": "us"}`. None
    for internal profiles and for any other provider (inference_geo is a
    first-party Anthropic API parameter; Bedrock and Vertex place inference
    by region instead)."""
    if profile.agent_type not in PUBLIC_AGENT_TYPES or profile.spec is None:
        return None
    model = profile.spec.model
    if not isinstance(model, str) or model.partition(":")[0] != "anthropic":
        return None
    return {"inference_geo": PUBLIC_INFERENCE_GEO}


def _check_url_host(
    where: str,
    url: Any,
    raw_url: Any,
    allowed_hosts: frozenset[str],
    who: str,
) -> None:
    if _is_interpolated(raw_url):
        raise ValueError(
            f"{where}: {who} profiles may not set a URL with ${{VAR}} interpolation; "
            f"its host cannot be checked against {ALLOWED_HOSTS_ENV} until it is "
            "resolved — write it literally"
        )
    if not allowed_hosts:
        raise ValueError(
            f"{where}: {who} profiles reach other hosts only through the "
            f"deployment's host list: set {ALLOWED_HOSTS_ENV} to comma-separated "
            "hostnames. Unset or empty refuses every MCP server and http voice "
            "endpoint on an external/hybrid profile."
        )
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{where}: {who} needs an http(s) URL with a host, got {url!r}")
    if parts.hostname not in allowed_hosts:
        raise ValueError(
            f"{where}: host {parts.hostname!r} is not in {ALLOWED_HOSTS_ENV} "
            f"({', '.join(sorted(allowed_hosts))}); {who} profiles may only reach "
            "allowed hosts"
        )


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
    allowed_hosts: Iterable[str] | str | None = None,
    uninterpolated: Mapping[str, Any] | None = None,
) -> None:
    """Raise ValueError if a public-facing profile breaks a rule; return
    otherwise. Internal profiles are never checked.

    `allowed_providers` / `allowed_hosts`: the deployment's lists (already
    parsed, or the raw comma-separated strings); None or empty refuses. `uninterpolated`: the profile document as
    written, before ``${VAR}`` expansion — the only place an interpolated
    model is still visible."""
    # Every profile, internal included: `${AT:-internal}` would otherwise
    # decide at load time whether any rule applies.
    if uninterpolated is not None and _is_interpolated(uninterpolated.get("agent_type")):
        raise ValueError(
            "agent_type: may not use ${VAR} interpolation; who a profile serves "
            "decides which rules apply, so write it literally"
        )
    if profile.agent_type not in PUBLIC_AGENT_TYPES:
        return
    who = f"agent_type: {profile.agent_type}"

    allowed = parse_allowed_providers(allowed_providers)
    if not allowed:
        raise ValueError(
            f"{who} needs the deployment's allowed-provider list: set "
            f"{ALLOWED_PROVIDERS_ENV} to comma-separated provider names "
            "(e.g. 'anthropic'). Unset or empty refuses every "
            "external/hybrid profile."
        )

    if profile.spec is None:  # i.e. an executor profile (exactly one backend)
        raise ValueError(
            f"executor: {who} profiles must be base tier (`spec:`); an "
            "executor profile runs its own loop on its own credentials"
        )

    if profile.tools:
        raise ValueError(
            f"tools: {who} profiles may not list local tools (custom code "
            "with whatever access the container has)"
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
        if not sep or not provider:
            raise ValueError(
                f"{display}: {who} needs a pydantic-ai 'provider:model' string, got {value!r}"
            )
        # Exactly as written: only the allowlist entries are normalized.
        if provider not in allowed:
            raise ValueError(
                f"{display}: provider {provider!r} is not in {ALLOWED_PROVIDERS_ENV} "
                f"({', '.join(sorted(allowed))}); {who} profiles may only use "
                "allowed providers"
            )

    hosts = parse_allowed_providers(allowed_hosts)
    _check_capabilities(profile, who, hosts, uninterpolated or {})
    _check_outbound_urls(profile, who, hosts, uninterpolated or {})
    _check_voice(profile, allowed, hosts, who, uninterpolated or {})


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


# Built-in capabilities a public-facing profile may use. An allowlist, not a
# denylist: custom @register_capability entries (arbitrary code) and any
# capability added later are refused until someone decides they are safe.
#   Thinking         — model-side only.
#   ImageGeneration  — native (the allowed provider) or a fallback_model,
#                      which is checked against the provider list.
#   MCP              — url host checked against the host allowlist.
#   WebFetch         — only `local: false` (the provider's native fetch);
#                      the local default fetches any URL from inside the
#                      deployment's network on the public user's say-so.
# Refused: WebSearch (falls back to a local DuckDuckGo search, a third party,
# when the model has no native search, and the profile cannot turn that off)
# and Peer (forwards the public user's text to an unchecked agent).
PUBLIC_CAPABILITIES = frozenset({"Thinking", "ImageGeneration", "MCP", "WebFetch"})


def _check_capabilities(
    profile: AgentProfile,
    who: str,
    hosts: frozenset[str],
    uninterpolated: Mapping[str, Any],
) -> None:
    for i, name, cfg in _capability_entries(profile):
        where = f"spec.capabilities[{i}].{name}"
        if name == "Peer":
            raise ValueError(
                f"{where}: {who} profiles may not use the Peer capability; "
                "call_agent would forward the public user's text to another "
                "agent that is not held to these rules"
            )
        if name not in PUBLIC_CAPABILITIES:
            raise ValueError(
                f"{where}: {who} profiles may not use the {name} capability "
                f"(allowed: {', '.join(sorted(PUBLIC_CAPABILITIES))}); custom "
                "capabilities and ones that reach the network on their own "
                "are refused"
            )
        if name == "MCP":
            _check_url_host(
                f"{where}.url", cfg.get("url"),
                _lookup(uninterpolated, ("spec", "capabilities", i, name, "url")),
                hosts, who,
            )
        if name == "WebFetch" and cfg.get("local", True) is not False:
            raise ValueError(
                f"{where}.local: {who} profiles may use WebFetch only with "
                "`local: false` (the provider's native fetch); the local "
                "fetcher would request any URL from inside this deployment"
            )


def _check_outbound_urls(
    profile: AgentProfile,
    who: str,
    hosts: frozenset[str],
    uninterpolated: Mapping[str, Any],
) -> None:
    """Profile-level delivery targets miragen POSTs to on its own: the
    approval webhook receives every gated tool call with its arguments (the
    public user's details), on_complete.post_to every run's output. Held to
    the host list like an MCP server. (Executor-tier URLs never get here:
    executor profiles are refused first; voice is checked separately.)"""
    targets = [
        ("approval_webhook", ("approval_webhook",), profile.approval_webhook),
        ("on_complete.post_to", ("on_complete", "post_to"),
         profile.on_complete.post_to if profile.on_complete else None),
    ]
    for where, raw_path, value in targets:
        if value is None:
            continue
        _check_url_host(where, str(value), _lookup(uninterpolated, raw_path), hosts, who)


def _check_voice(
    profile: AgentProfile,
    allowed: frozenset[str],
    hosts: frozenset[str],
    who: str,
    uninterpolated: Mapping[str, Any],
) -> None:
    """A cloud speech provider receives everything the agent says aloud, so
    it is held to the provider list (by its name, e.g. 'openai'); an 'http'
    endpoint's host is held to the host list."""
    voice = profile.voice
    if voice is None:
        return
    if _is_interpolated(
        _lookup(uninterpolated, ("voice", "provider"))
    ):
        raise ValueError(
            f"voice.provider: {who} profiles may not set the speech provider with "
            "${VAR} interpolation; write it literally"
        )
    if voice.provider == "http":
        _check_url_host("voice.url", voice.url, _lookup(uninterpolated, ("voice", "url")),
                        hosts, who)
    elif voice.provider not in allowed:
        raise ValueError(
            f"voice.provider: speech provider {voice.provider!r} is not in "
            f"{ALLOWED_PROVIDERS_ENV} ({', '.join(sorted(allowed))}); {who} "
            "profiles may only use allowed providers"
        )
