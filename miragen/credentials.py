"""Which credentials an agent profile is entitled to (per-profile grants).

miragend holds the deployment's provider keys as ``NAME_API_KEY`` env vars
or as Docker secrets named by ``NAME_API_KEY_FILE``. It used to hand every
one of them to every agent it spawned, so an agent that needed one provider
could read (and spend, or leak) all of them. An agent now receives only:

- **Implied keys** — what the profile itself already says it uses:
  - the provider key of every pydantic-ai model it names (``spec.model``,
    ``memory.extraction.model`` / ``memory.recall.model``, an
    ``ImageGeneration`` ``fallback_model``): ``openai:...`` ->
    ``OPENAI_API_KEY``. Subscription harness models (``claude-code:``,
    ``codex:``, ``grok-build:``) imply no key — they run on the
    subscription, and a stray metered key would silently outrank it;
  - an executor's own documented metered fallback (``codex`` ->
    ``CODEX_API_KEY`` / ``OPENAI_API_KEY`` …). ``claude-code`` is left out
    for the same reason as the harness models, and ``grok-build`` with
    ``grok_auth: subscription`` gets none (the executor strips it anyway);
  - any ``*_env`` field naming a key (``voice.api_key_env``, which
    defaults to ``OPENAI_API_KEY`` for ``provider: openai``;
    ``bearer_token_env`` …) and any ``${..._API_KEY}`` reference in the
    profile text — the loader inside the container fails on an undefined
    reference, so a referenced key that was withheld would crash-loop it.
- **Granted keys** — ``credentials: [NAME_API_KEY, ...]`` on the profile,
  for anything else (a tool or capability that reads a key from the env).

Everything else is withheld. Only names matching
``GRANTABLE_CREDENTIAL_PATTERN`` are ever selected, so no profile can name
its way to a daemon variable outside the ``*_API_KEY`` family.

The subscription token (``CLAUDE_CODE_OAUTH_TOKEN``) is separate: it goes
to agents that run Claude Code — ``executor: claude-code`` or a
``claude-code:`` ``spec.model`` — and never to any other.

Docs: docs/credential-grants.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from miragen.models import GRANTABLE_CREDENTIAL_PATTERN, AgentProfile

_GRANTABLE_RE = re.compile(GRANTABLE_CREDENTIAL_PATTERN)

# pydantic-ai provider prefix -> the *_API_KEY variables its provider reads
# (pydantic_ai/providers/*). Providers that authenticate otherwise
# (bedrock, google-vertex: cloud credentials; huggingface: HF_TOKEN;
# heroku: HEROKU_INFERENCE_KEY) are absent: nothing in the *_API_KEY family
# would help them, and those variables were never forwarded either.
PROVIDER_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "openai-chat": ("OPENAI_API_KEY",),
    "openai-responses": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google-gla": ("GEMINI_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "google-cloud": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "cohere": ("CO_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "azure": ("AZURE_OPENAI_API_KEY",),
    "grok": ("GROK_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "moonshotai": ("MOONSHOTAI_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "github": ("GITHUB_API_KEY",),
    "vercel": ("VERCEL_AI_GATEWAY_API_KEY",),
    "ollama": ("OLLAMA_API_KEY",),
    "nebius": ("NEBIUS_API_KEY",),
    "ovhcloud": ("OVHCLOUD_API_KEY",),
    "alibaba": ("ALIBABA_API_KEY", "DASHSCOPE_API_KEY"),
    "sambanova": ("SAMBANOVA_API_KEY",),
    "voyageai": ("VOYAGE_API_KEY",),
    "gateway": ("PYDANTIC_AI_GATEWAY_API_KEY", "PAIG_API_KEY"),
}

# Executor kind -> its documented metered fallback (docs/design/
# subscription-homes.md). claude-code is absent on purpose: its credential
# is the subscription token below, and ANTHROPIC_API_KEY outranks that
# token inside Claude Code, so forwarding it would silently switch the
# agent to metered billing. Grant it explicitly to opt in.
EXECUTOR_FALLBACK_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "codex": ("CODEX_API_KEY", "OPENAI_API_KEY"),
    "kimi-code": ("KIMI_API_KEY", "MOONSHOT_API_KEY", "KIMI_CODE_API_KEY"),
    "grok-build": ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY"),
}

# The subscription token and who may receive it. Not a *_API_KEY: never
# grantable through `credentials`, only through running Claude Code.
CLAUDE_CODE_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_CODE_MODEL_PREFIX = "claude-code:"

# Model strings that name a subscription harness, not a pydantic-ai provider.
_HARNESS_MODEL_PREFIXES = ("claude-code:", "codex:", "grok-build:")

# Same grammar as the loader's ${VAR} / ${VAR:-default}; $${ is an escape.
_ENV_REF_RE = re.compile(r"\$\$\{|\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


@dataclass(frozen=True)
class CredentialPlan:
    """What an agent may receive. `api_keys`: *_API_KEY names (each covers
    NAME and NAME_FILE plus the secret NAME_FILE points at). `env_tokens`:
    executor credentials outside that family (the subscription token)."""

    api_keys: frozenset[str] = frozenset()
    env_tokens: frozenset[str] = frozenset()


NO_CREDENTIALS = CredentialPlan()


def runs_claude_code(profile: AgentProfile) -> bool:
    if profile.executor is not None:
        return profile.executor.executor == "claude-code"
    return profile.spec is not None and profile.spec.model.startswith(CLAUDE_CODE_MODEL_PREFIX)


def _model_strings(profile: AgentProfile) -> list[str]:
    models: list[str] = []
    if profile.spec is not None:
        models.append(profile.spec.model)
        for entry in profile.spec.capabilities or []:
            if isinstance(entry, dict) and len(entry) == 1:
                cfg = next(iter(entry.values()))
                if isinstance(cfg, dict) and isinstance(cfg.get("fallback_model"), str):
                    models.append(cfg["fallback_model"])
    if profile.memory is not None:
        for sub in (profile.memory.extraction, profile.memory.recall):
            model = getattr(sub, "model", None)
            if isinstance(model, str):
                models.append(model)
    return models


def provider_credentials(model: str) -> tuple[str, ...]:
    """'openai:gpt-5' -> ('OPENAI_API_KEY',). Harness models, bare names and
    unknown providers -> ()."""
    if model.startswith(_HARNESS_MODEL_PREFIXES):
        return ()
    provider, sep, _ = model.partition(":")
    if not sep:
        return ()
    return PROVIDER_CREDENTIAL_ENV.get(provider.strip().lower(), ())


def _env_field_names(value: Any) -> list[str]:
    """Values of every `*_env` string field anywhere in the profile dump."""
    out: list[str] = []
    if isinstance(value, dict):
        for key, v in value.items():
            if isinstance(key, str) and key.endswith("_env") and isinstance(v, str):
                out.append(v)
            else:
                out.extend(_env_field_names(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_env_field_names(v))
    return out


def _env_references(document: Any) -> list[str]:
    """${NAME} references in the profile as written (before interpolation)."""
    out: list[str] = []
    if isinstance(document, dict):
        for v in document.values():
            out.extend(_env_references(v))
    elif isinstance(document, list):
        for v in document:
            out.extend(_env_references(v))
    elif isinstance(document, str):
        out.extend(m.group(1) for m in _ENV_REF_RE.finditer(document) if m.group(1))
    return out


def credential_plan(profile: AgentProfile, raw_document: Any = None) -> CredentialPlan:
    """The credentials `profile` may receive. `raw_document`: the profile
    as written (parsed YAML, before ${VAR} expansion) — the only place its
    env references are still visible."""
    names: set[str] = set(profile.credentials)
    for model in _model_strings(profile):
        names.update(provider_credentials(model))
    if profile.voice is not None:
        if profile.voice.api_key_env:
            names.add(profile.voice.api_key_env)
        elif profile.voice.provider == "openai":
            names.add("OPENAI_API_KEY")
    if profile.executor is not None:
        kind = profile.executor.executor
        if not (kind == "grok-build" and profile.executor.grok_auth == "subscription"):
            names.update(EXECUTOR_FALLBACK_CREDENTIAL_ENV.get(kind, ()))
    names.update(_env_field_names(profile.model_dump(mode="json")))
    if raw_document is not None:
        names.update(_env_references(raw_document))
    tokens = {CLAUDE_CODE_TOKEN_ENV} if runs_claude_code(profile) else set()
    return CredentialPlan(
        api_keys=frozenset(n for n in names if _GRANTABLE_RE.fullmatch(n)),
        env_tokens=frozenset(tokens),
    )
