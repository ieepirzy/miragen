"""Which credentials an agent profile is entitled to (per-profile grants).

miragend holds the deployment's provider keys as ``NAME_API_KEY`` env vars
or as Docker secrets named by ``NAME_API_KEY_FILE``. It used to hand every
one of them to every agent it spawned, so an agent that needed one provider
could read (and spend, or leak) all of them. An agent now receives only:

- **Implied keys** — what the profile itself already says it uses:
  - the provider key of every pydantic-ai model it names (``spec.model``,
    ``memory.extraction.model`` / ``memory.recall.model``, an
    ``ImageGeneration`` ``fallback_model``): ``openai:...`` (or a bare
    ``gpt-4o`` pydantic-ai still resolves) -> ``OPENAI_API_KEY``,
    ``gateway/...`` -> the gateway key. Subscription harness models
    (``claude-code:``, ``codex:``, ``grok-build:``) imply no key — they run
    on the subscription, and a stray metered key would silently outrank it;
  - an executor's own documented metered fallback (``kimi-code``,
    ``grok-build`` — except with ``grok_auth: subscription``, which the
    executor strips anyway). ``claude-code`` and ``codex`` run on the
    subscription: their metered keys (SUBSCRIPTION_OVERRIDING_KEYS) reach
    them only through an explicit grant, never through any implied route;
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
to agents that run Claude Code — ``executor: claude-code``, a
``claude-code:`` ``spec.model``, or a ``claude-code:`` memory recall /
extraction model — and never to any other.

Docs: docs/credential-grants.md.
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from miragen.models import GRANTABLE_CREDENTIAL_PATTERN, AgentProfile

GRANTABLE_RE = re.compile(GRANTABLE_CREDENTIAL_PATTERN)

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
# subscription-homes.md). claude-code and codex are absent on purpose: they
# run on the subscription (the long-lived token / the mounted auth.json),
# and a metered key outranks it — Claude Code prefers ANTHROPIC_API_KEY over
# its token, the codex executor logs in with an API key over auth.json. So
# their metered fallback is an explicit grant (`credentials:`), never implied.
EXECUTOR_FALLBACK_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "kimi-code": ("KIMI_API_KEY", "MOONSHOT_API_KEY", "KIMI_CODE_API_KEY"),
    "grok-build": ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY"),
}

# Keys a subscription executor would let outrank its subscription, by kind:
# they reach such an agent only when `credentials:` lists them, never through
# an implied route (a memory model's provider, a ${VAR} reference, a *_env
# field). The executors enforce the same rule on their child processes
# (executor/claude_code.py, executor/codex.py), because the daemon's plan is
# not the only way a key can reach a container (MIRAGEND_AGENT_ENV_PASSTHROUGH,
# a hand-run container).
SUBSCRIPTION_OVERRIDING_KEYS: dict[str, tuple[str, ...]] = {
    "claude-code": ("ANTHROPIC_API_KEY",),
    "codex": ("CODEX_API_KEY", "OPENAI_API_KEY"),
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
    """`executor: claude-code`, or any model the profile names with the
    `claude-code:` prefix: spec.model (the harness) or a memory model (the
    recall selector / extraction run headless `claude -p` in the agent)."""
    if profile.executor is not None and profile.executor.executor == "claude-code":
        return True
    return any(m.startswith(CLAUDE_CODE_MODEL_PREFIX) for m in _model_strings(profile))


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


# pydantic-ai's Pydantic AI Gateway routes `gateway/<provider>:model` through
# one gateway key (pydantic_ai/providers/gateway.py), whatever the upstream.
GATEWAY_PROVIDER_PREFIX = "gateway/"


def _model_provider(model: str) -> str | None:
    """The provider pydantic-ai resolves `model` to: its own parse_model_id,
    so `provider:model` and the legacy bare names it still accepts (`gpt-4o`
    -> openai, `claude-…` -> anthropic, `gemini-…` -> google; its
    _LEGACY_MODEL_PREFIXES) map exactly as the runtime will. None for a name
    the runtime cannot resolve either."""
    import warnings

    from pydantic_ai.models import parse_model_id

    with warnings.catch_warnings():
        # A bare name is deprecated upstream, not invalid: the runtime still
        # serves it, so the plan must too.
        warnings.simplefilter("ignore", DeprecationWarning)
        provider, _ = parse_model_id(model.strip())
    return provider.strip().lower() if provider else None


def provider_credentials(model: str) -> tuple[str, ...]:
    """'openai:gpt-5' / 'gpt-5' -> ('OPENAI_API_KEY',);
    'gateway/openai:gpt-5' -> the gateway key. Harness models and unknown
    providers -> ()."""
    if model.startswith(_HARNESS_MODEL_PREFIXES):
        return ()
    provider = _model_provider(model)
    if provider is None:
        return ()
    if provider.startswith(GATEWAY_PROVIDER_PREFIX):
        return PROVIDER_CREDENTIAL_ENV["gateway"]
    return PROVIDER_CREDENTIAL_ENV.get(provider, ())


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
    if profile.executor is not None:
        # Only an explicit grant may hand a subscription executor the key
        # that would outrank its subscription: an anthropic: memory model,
        # a ${ANTHROPIC_API_KEY} reference or a bearer_token_env naming it
        # implies nothing here.
        explicit = set(profile.credentials)
        names -= set(SUBSCRIPTION_OVERRIDING_KEYS.get(profile.executor.executor, ())) - explicit
    tokens = {CLAUDE_CODE_TOKEN_ENV} if runs_claude_code(profile) else set()
    return CredentialPlan(
        api_keys=frozenset(n for n in names if GRANTABLE_RE.fullmatch(n)),
        env_tokens=frozenset(tokens),
    )


# Never grantable (not a *_API_KEY), and Claude Code bills it over the token.
CLAUDE_CODE_ALWAYS_SCRUBBED = ("ANTHROPIC_AUTH_TOKEN",)


def scrub_subscription_overrides(
    profile: AgentProfile, environ: MutableMapping[str, str] | None = None
) -> list[str]:
    """Remove from `environ` (default: this process's) every key that would
    outrank a subscription executor's own credential, unless the profile
    grants it in `credentials:`. Runs in the agent at executor startup, after
    *_FILE secrets resolve and before any child (Claude Code, the Codex app
    server) is spawned — children inherit this environment, and the SDKs can
    override a variable but not unset it. Returns the names removed."""
    env = os.environ if environ is None else environ
    if profile.executor is None:
        return []
    kind = profile.executor.executor
    granted = set(profile.credentials)
    names = [k for k in SUBSCRIPTION_OVERRIDING_KEYS.get(kind, ()) if k not in granted]
    if kind == "claude-code":
        names += CLAUDE_CODE_ALWAYS_SCRUBBED
    removed = [k for k in names if k in env]
    for key in removed:
        del env[key]
    return removed


def granted_explicitly(profile: AgentProfile, key: str) -> bool:
    """`key` is listed in the profile's `credentials:`."""
    return key in profile.credentials
