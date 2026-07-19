"""Environment Definition File (EDF) contract — validation, canonicalization,
and resolution into an executable miragen profile.

This is the execution-side half of the MiraRun EDF contract (issue #33,
Phase A). The three artifacts stay conceptually distinct:

    EDF               desired environment, versioned + human-editable
    resolved profile  executable miragen configuration (a real AgentProfile)
    run snapshot      immutable resolved definition + hash + concrete
                      revisions, persisted per launched run

Validation is STRICT for the selected apiVersion: unknown fields, malformed
mount paths, duplicate/overlapping mounts, dangling secret references, and
dead config (an allowlist with no hosts) all fail loudly — same philosophy
as the profile models.

Canonical hash contract (must stay stable — MiraRun stores these hashes):
validate → apply explicit defaults → expand tool presets → sort object keys
→ serialize as RFC 8785-style canonical JSON (compact separators, UTF-8,
no ASCII escaping) → SHA-256. Array order remains significant. Secret VALUES
never appear anywhere in an EDF; stable secret-reference identity (name,
providerRef, exposeAs) is part of the hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from itertools import combinations
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.alias_generators import to_camel

from miragen.models import AgentProfile

EDF_API_VERSION = "mirarun.io/v1alpha1"

# Bump on any change to resolution semantics (mapping, defaults, presets) so
# stored snapshots can report which resolver produced them.
RESOLVER_VERSION = "1"

# ExecutorSpec.instructions is required; an EDF carries no instructions (the
# task arrives as the launch prompt), so resolution supplies this unless the
# caller passes its own via ResolutionContext.
DEFAULT_INSTRUCTIONS = (
    "You are an autonomous execution agent operating in a prepared workspace. "
    "Complete the task described in the prompt."
)

# Versioned tool presets: expansion happens BEFORE hashing, so the canonical
# document always carries the concrete flags. Explicitly-set flags win over
# the preset. Preset versions are recorded in the resolved output.
_TOOL_PRESETS: dict[str, dict[str, Any]] = {
    "mirarun-default": {
        "version": "1",
        "flags": {"git": True, "github": True, "shell": True, "web_search": True, "runtime_discovery": True},
    },
}


# ── Errors ───────────────────────────────────────────────────────────────────

class EDFValidationError(ValueError):
    """Structured EDF validation/resolution failure: `errors` is a list of
    {"loc": dotted.path, "message": str} suitable for a 422 response body."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        summary = "; ".join(f"{e['loc']}: {e['message']}" for e in errors[:5])
        super().__init__(f"invalid EDF ({len(errors)} error(s)): {summary}")


def _error(loc: str, message: str) -> EDFValidationError:
    return EDFValidationError([{"loc": loc, "message": message}])


# ── Shared value validation ──────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^(?:([0-9]+)h)?(?:([0-9]+)m)?(?:([0-9]+)s)?$")


def parse_duration(value: str) -> int:
    """Duration grammar → integer seconds. One or more <int><unit> segments,
    units h/m/s from largest to smallest, none repeated: '30m', '1h30m', '45s'."""
    match = _DURATION_RE.match(value)
    if match is None or not any(match.groups()):
        raise ValueError(
            f"invalid duration '{value}': expected <int><unit> segments with units h/m/s "
            "from largest to smallest, e.g. '30m', '1h30m', '45s'"
        )
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _positive_duration(value: str) -> str:
    if parse_duration(value) <= 0:
        raise ValueError(f"duration '{value}' must be positive")
    return value


def _valid_mount_path(path: str) -> str:
    """Relative, normalized workspace mount path: no leading '/', no empty
    segments (covers '//' and trailing '/'), no '.'/'..' segments."""
    if path.startswith("/"):
        raise ValueError(f"mount path '{path}' must be relative, not absolute")
    segments = path.split("/")
    if any(seg == "" for seg in segments):
        raise ValueError(f"mount path '{path}' contains an empty segment")
    if any(seg in (".", "..") for seg in segments):
        raise ValueError(f"mount path '{path}' must be normalized: no '.' or '..' segments")
    return path


# ── EDF document models (mirarun.io/v1alpha1) ────────────────────────────────
#
# Wire form is camelCase (matching mirarun's v1alpha1 JSON Schema); Python
# attribute names are snake_case via the alias generator. extra="forbid":
# unknown fields fail validation for the selected version.

class _EDFModel(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class EDFMetadata(_EDFModel):
    # Stricter than the published schema's pattern alone: capped at 63 chars so
    # the name is always usable as a miragen agent name / container name.
    name: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class EDFBudget(_EDFModel):
    tokens_per_run: int = Field(gt=0)


class EDFSandbox(_EDFModel):
    mode: Literal["read-only", "workspace-write", "full-access"] = "workspace-write"


class EDFApprovals(_EDFModel):
    unattended: bool = True


class EDFExecutor(_EDFModel):
    kind: str = Field(min_length=1)
    model: Optional[str] = None
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    timeout: str = "30m"
    budget: Optional[EDFBudget] = None
    sandbox: EDFSandbox = Field(default_factory=EDFSandbox)
    approvals: EDFApprovals = Field(default_factory=EDFApprovals)

    _timeout_positive = field_validator("timeout")(_positive_duration)


class EDFRepositorySource(_EDFModel):
    provider: str = Field(min_length=1)
    connection_ref: str = Field(
        min_length=1,
        description="Opaque repository connection reference — never a clone URL carrying credentials.",
    )

    @field_validator("connection_ref")
    @classmethod
    def reject_credential_urls(cls, v: str) -> str:
        if "://" in v and "@" in v:
            raise ValueError(
                "connectionRef looks like a URL with embedded credentials; "
                "it must be an opaque connection reference"
            )
        return v


class EDFRepository(_EDFModel):
    name: str = Field(min_length=1)
    source: EDFRepositorySource
    ref: str = Field(min_length=1)
    mount_path: str
    writable: bool = False

    _mount_valid = field_validator("mount_path")(_valid_mount_path)


class EDFWorkspace(_EDFModel):
    repositories: list[EDFRepository]

    @model_validator(mode="after")
    def validate_unique_non_overlapping(self) -> "EDFWorkspace":
        names = [r.name for r in self.repositories]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate repository name(s): {dupes}")
        mounts = [r.mount_path for r in self.repositories]
        if len(mounts) != len(set(mounts)):
            dupes = sorted({m for m in mounts if mounts.count(m) > 1})
            raise ValueError(f"duplicate repository mountPath(s): {dupes}")
        for a, b in combinations(mounts, 2):
            if a.startswith(b + "/") or b.startswith(a + "/"):
                raise ValueError(f"repository mountPaths overlap: '{a}' and '{b}'")
        for mount in mounts:
            # .miragen at the workspace root holds harvest output (diff.patch,
            # per-repo diffs) — a repository mounted there would collide with it.
            if mount == ".miragen" or mount.startswith(".miragen/"):
                raise ValueError(
                    f"mountPath '{mount}' is reserved: '.miragen' holds miragen's "
                    "harvest output at the workspace root"
                )
        return self


class EDFMCPAuth(_EDFModel):
    bearer_token_secret_ref: str = Field(min_length=1)


class EDFMCPServer(_EDFModel):
    name: str = Field(min_length=1)
    transport: Literal["streamable-http", "sse", "stdio"]
    url: str = Field(min_length=1)
    auth: Optional[EDFMCPAuth] = None


class EDFTools(_EDFModel):
    preset: Optional[str] = None
    git: bool = False
    github: bool = False
    shell: bool = False
    web_search: bool = False
    runtime_discovery: bool = False
    mcp_servers: list[EDFMCPServer] = Field(default_factory=list)

    @model_validator(mode="after")
    def expand_preset(self) -> "EDFTools":
        """Deterministic preset expansion, applied at validation time so the
        canonical (hashed) document always carries the concrete flags.
        Explicitly-set flags win over the preset."""
        if self.preset is None:
            return self
        preset = _TOOL_PRESETS.get(self.preset)
        if preset is None:
            raise ValueError(
                f"unknown tools preset '{self.preset}'; known presets: {sorted(_TOOL_PRESETS)}"
            )
        for flag, value in preset["flags"].items():
            if flag not in self.model_fields_set:
                setattr(self, flag, value)
        return self

    @model_validator(mode="after")
    def validate_unique_server_names(self) -> "EDFTools":
        names = [s.name for s in self.mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate mcpServers name(s): {sorted({n for n in names if names.count(n) > 1})}")
        return self


class EDFSecretExposeAs(_EDFModel):
    environment_variable: Optional[str] = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")

    @model_validator(mode="after")
    def validate_at_least_one_exposure(self) -> "EDFSecretExposeAs":
        if self.environment_variable is None:
            raise ValueError("exposeAs requires at least one exposure (environmentVariable)")
        return self


class EDFSecretRef(_EDFModel):
    name: str = Field(min_length=1)
    provider_ref: str = Field(min_length=1, pattern=r"^[a-zA-Z][a-zA-Z0-9+.-]*://.+")
    expose_as: EDFSecretExposeAs


class EDFNetwork(_EDFModel):
    outbound: Literal["deny", "allowlist"]
    allowed_hosts: list[str] = Field(default_factory=list)

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts_non_empty_strings(cls, v: list[str]) -> list[str]:
        if any(not host for host in v):
            raise ValueError("allowedHosts entries must be non-empty")
        return v

    @model_validator(mode="after")
    def validate_no_dead_config(self) -> "EDFNetwork":
        if self.outbound == "deny" and self.allowed_hosts:
            raise ValueError("allowedHosts is dead config when outbound is 'deny'")
        if self.outbound == "allowlist" and not self.allowed_hosts:
            raise ValueError("outbound 'allowlist' requires at least one allowedHosts entry")
        return self


class EDFLifecycleStep(_EDFModel):
    id: str = Field(min_length=1)
    run: str = Field(min_length=1)
    working_directory: Optional[str] = None
    timeout: Optional[str] = None

    @field_validator("working_directory")
    @classmethod
    def validate_working_directory(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _valid_mount_path(v)

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _positive_duration(v)


class EDFLifecycle(_EDFModel):
    workspace_setup: list[EDFLifecycleStep] = Field(default_factory=list)
    before_run: list[EDFLifecycleStep] = Field(default_factory=list)
    after_run: list[EDFLifecycleStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_step_ids(self) -> "EDFLifecycle":
        for phase in ("workspace_setup", "before_run", "after_run"):
            ids = [s.id for s in getattr(self, phase)]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate step id(s) in lifecycle phase '{to_camel(phase)}'")
        return self


class EDFTarget(BaseModel):
    # Deliberately open (additionalProperties: true in the schema): targets are
    # adapter-owned and deferred beyond an empty list until Phase G.
    model_config = ConfigDict(extra="allow", alias_generator=to_camel, populate_by_name=True)

    name: str = Field(min_length=1)


class EDFSpec(_EDFModel):
    executor: EDFExecutor
    workspace: EDFWorkspace
    tools: EDFTools = Field(default_factory=EDFTools)
    variables: dict[str, str] = Field(default_factory=dict)
    secrets: list[EDFSecretRef] = Field(default_factory=list)
    network: EDFNetwork = Field(default_factory=lambda: EDFNetwork(outbound="deny"))
    lifecycle: EDFLifecycle = Field(default_factory=EDFLifecycle)
    targets: list[EDFTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_secret_references(self) -> "EDFSpec":
        names = [s.name for s in self.secrets]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate secret name(s): {sorted({n for n in names if names.count(n) > 1})}")
        env_vars = [s.expose_as.environment_variable for s in self.secrets if s.expose_as.environment_variable]
        if len(env_vars) != len(set(env_vars)):
            raise ValueError("two secrets expose the same environmentVariable")
        declared = set(names)
        for server in self.tools.mcp_servers:
            if server.auth is not None and server.auth.bearer_token_secret_ref not in declared:
                raise ValueError(
                    f"mcpServer '{server.name}' references undeclared secret "
                    f"'{server.auth.bearer_token_secret_ref}' (declare it under spec.secrets)"
                )
        return self


class EnvironmentDefinition(_EDFModel):
    api_version: Literal["mirarun.io/v1alpha1"]
    kind: Literal["Environment"]
    metadata: EDFMetadata
    spec: EDFSpec


# ── Validation entrypoint ────────────────────────────────────────────────────

def validate_edf(document: Any) -> EnvironmentDefinition:
    """Strict validation of a parsed EDF document (with deterministic default
    and preset expansion applied). Raises EDFValidationError with structured
    per-field errors."""
    if not isinstance(document, dict):
        raise _error("", f"EDF must be a JSON object, got {type(document).__name__}")
    api_version = document.get("apiVersion")
    if api_version != EDF_API_VERSION:
        raise _error(
            "apiVersion",
            f"unsupported apiVersion {api_version!r}; this resolver supports '{EDF_API_VERSION}' "
            "(breaking semantic changes require a new apiVersion and explicit conversion)",
        )
    try:
        return EnvironmentDefinition.model_validate(document)
    except ValidationError as e:
        raise EDFValidationError([
            {"loc": ".".join(str(part) for part in err["loc"]), "message": err["msg"]}
            for err in e.errors()
        ]) from e


# ── Canonicalization + hashing ───────────────────────────────────────────────

def canonical_document(edf: EnvironmentDefinition) -> dict[str, Any]:
    """The fully expanded wire-form document: every field present (null where
    unset), defaults and presets applied. This is what gets hashed."""
    return edf.model_dump(mode="json", by_alias=True)


def canonical_json(document: Any) -> str:
    """RFC 8785-style canonical JSON: sorted object keys, compact separators,
    no ASCII escaping. Array order remains significant."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha256(document: Any) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


# ── Resolution ───────────────────────────────────────────────────────────────

class RepositoryBinding(BaseModel):
    """An authorized runtime binding for one opaque connectionRef: where to
    actually fetch from, minted by the control plane just-in-time.

    Ephemeral by contract: bindings are consumed during workspace preparation
    and never persisted — not in profiles, snapshots, run records, or events —
    and any credentialed URL is redacted from errors and stripped from the
    clone's git config."""

    model_config = ConfigDict(extra="forbid")

    clone_url: str = Field(
        min_length=1,
        description="Git URL or local path to fetch from; may carry a short-lived credential.",
    )


class ResolutionContext(BaseModel):
    """Caller-supplied resolution inputs. Deliberately narrow: opaque product
    references are NOT dereferenced here — miragen never calls provider APIs
    with control-plane credentials embedded in an EDF."""

    model_config = ConfigDict(extra="forbid")

    instructions: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Executor task framing; DEFAULT_INSTRUCTIONS when omitted.",
    )
    repositories: dict[str, RepositoryBinding] = Field(
        default_factory=dict,
        description=(
            "connectionRef → authorized runtime binding. Ignored by pure "
            "resolution (bindings are launch-time state, never desired state, "
            "never hashed); consumed by workspace preparation at launch."
        ),
    )


class RepositoryPlanEntry(BaseModel):
    """One repository the workspace should contain. `commit` stays None until
    workspace preparation resolves the ref (issue #33 Phase D)."""

    name: str
    provider: str
    connection_ref: str
    ref: str
    mount_path: str
    writable: bool
    commit: Optional[str] = None


class SecretBinding(BaseModel):
    """Stable secret-reference identity — never a value. The deployment layer
    injects the value into the named environment variable at spawn."""

    name: str
    provider_ref: str
    environment_variable: Optional[str] = None


class ResolvedEDF(BaseModel):
    """Output of resolve_edf(): everything a control plane needs to display,
    hash-verify, and launch — and everything a run snapshot records."""

    resolver_version: str
    api_version: str
    name: str
    sha256: str
    canonical: dict[str, Any]
    preset_versions: dict[str, str]
    resolved_profile: dict[str, Any]
    repository_plan: list[RepositoryPlanEntry]
    secret_bindings: list[SecretBinding]
    workspace_plan: dict[str, Any]
    target_adapter_plan: list[dict[str, Any]]
    warnings: list[str]


_SANDBOX_MODES = {
    "read-only": "read-only",
    "workspace-write": "workspace-write",
    "full-access": "danger-full-access",
}

# spawn is deliberately absent: it needs an argv template, which EDF v1alpha1
# cannot express.
_RESOLVABLE_KINDS = {"codex", "claude-code"}

_MCP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def resolve_edf(
    document: Any,
    *,
    context: ResolutionContext | None = None,
) -> ResolvedEDF:
    """Validate + canonicalize + resolve an EDF into an executable miragen
    profile plus workspace/repository/secret binding plans.

    Pure and deterministic: no filesystem, network, or clock access — the same
    document and context always produce the same output and hash.
    """
    edf = document if isinstance(document, EnvironmentDefinition) else validate_edf(document)
    context = context or ResolutionContext()
    spec = edf.spec
    warnings: list[str] = []

    kind = spec.executor.kind
    if kind not in _RESOLVABLE_KINDS:
        raise _error(
            "spec.executor.kind",
            f"unsupported executor kind '{kind}'; resolvable kinds: {sorted(_RESOLVABLE_KINDS)} "
            "(spawn is not expressible in EDF v1alpha1 — it requires an argv template)",
        )

    mcp_servers: list[dict[str, Any]] = []
    secrets_by_name = {s.name: s for s in spec.secrets}
    for i, server in enumerate(spec.tools.mcp_servers):
        loc = f"spec.tools.mcpServers.{i}"
        if server.transport != "streamable-http":
            raise _error(
                f"{loc}.transport",
                f"transport '{server.transport}' is not supported by miragen executor MCP "
                "injection; only 'streamable-http' resolves in v1alpha1",
            )
        if not _MCP_NAME_RE.match(server.name):
            raise _error(
                f"{loc}.name",
                f"mcpServer name '{server.name}' must match {_MCP_NAME_RE.pattern} "
                "to be injectable into executor config",
            )
        bearer_token_env = None
        if server.auth is not None:
            secret = secrets_by_name[server.auth.bearer_token_secret_ref]
            bearer_token_env = secret.expose_as.environment_variable
        mcp_servers.append({
            "name": server.name,
            "url": server.url,
            **({"bearer_token_env": bearer_token_env} if bearer_token_env else {}),
        })

    executor_spec: dict[str, Any] = {
        "executor": kind,
        "instructions": context.instructions or DEFAULT_INSTRUCTIONS,
        "model": spec.executor.model,
        "sandbox_mode": _SANDBOX_MODES[spec.executor.sandbox.mode],
        "approval_policy": "never" if spec.executor.approvals.unattended else "on-request",
        "network_access": spec.network.outbound == "allowlist",
        "web_search": spec.tools.web_search,
        "reasoning_effort": spec.executor.reasoning_effort,
        "turn_timeout_s": parse_duration(spec.executor.timeout),
    }
    if mcp_servers:
        executor_spec["mcp_servers"] = mcp_servers

    profile_doc: dict[str, Any] = {
        "name": edf.metadata.name,
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "executor": executor_spec,
    }
    if spec.executor.budget is not None:
        profile_doc["limits"] = {"tokens_per_run": spec.executor.budget.tokens_per_run}

    try:
        profile = AgentProfile.model_validate(profile_doc)
    except ValidationError as e:  # a mapping bug, not a user error — surface loudly
        raise _error("", f"resolved profile failed AgentProfile validation: {e}") from e

    repository_plan = [
        RepositoryPlanEntry(
            name=repo.name,
            provider=repo.source.provider,
            connection_ref=repo.source.connection_ref,
            ref=repo.ref,
            mount_path=repo.mount_path,
            writable=repo.writable,
        )
        for repo in spec.workspace.repositories
    ]
    if spec.lifecycle.workspace_setup or spec.lifecycle.before_run or spec.lifecycle.after_run:
        warnings.append(
            "lifecycle phases are validated and recorded but not yet executed by miragen "
            "(issue #33 Phase D)"
        )
    if spec.targets:
        warnings.append(
            "targets are recorded but not provisioned; the target event/provenance contract "
            "is issue #33 Phase G"
        )
    if spec.network.outbound == "allowlist":
        warnings.append(
            "network.allowedHosts is recorded, but host-level egress enforcement is "
            "deployment-owned; miragen only toggles executor sandbox network access"
        )

    canonical = canonical_document(edf)
    preset_versions = (
        {spec.tools.preset: _TOOL_PRESETS[spec.tools.preset]["version"]} if spec.tools.preset else {}
    )

    return ResolvedEDF(
        resolver_version=RESOLVER_VERSION,
        api_version=edf.api_version,
        name=edf.metadata.name,
        sha256=canonical_sha256(canonical),
        canonical=canonical,
        preset_versions=preset_versions,
        resolved_profile=profile.model_dump(mode="json"),
        repository_plan=repository_plan,
        secret_bindings=[
            SecretBinding(
                name=s.name,
                provider_ref=s.provider_ref,
                environment_variable=s.expose_as.environment_variable,
            )
            for s in spec.secrets
        ],
        workspace_plan={
            "layout": "single-workspace",
            "variables": dict(spec.variables),
            "network": spec.network.model_dump(mode="json", by_alias=True),
            "lifecycle": spec.lifecycle.model_dump(mode="json", by_alias=True),
        },
        target_adapter_plan=[t.model_dump(mode="json", by_alias=True) for t in spec.targets],
        warnings=warnings,
    )


# ── Run snapshot ─────────────────────────────────────────────────────────────

RUN_SNAPSHOT_SCHEMA = "miragen/run-snapshot/v1"


def build_run_snapshot(resolved: ResolvedEDF, *, run_id: str, created_at: str) -> dict[str, Any]:
    """The immutable per-run snapshot document persisted alongside the run
    record. Contains the canonical definition + hash and the resolution plans;
    excludes secret values by construction (they never enter an EDF)."""
    return {
        "snapshot_schema": RUN_SNAPSHOT_SCHEMA,
        "run_id": run_id,
        "created_at": created_at,
        "resolver_version": resolved.resolver_version,
        "api_version": resolved.api_version,
        "name": resolved.name,
        "sha256": resolved.sha256,
        "canonical": resolved.canonical,
        "preset_versions": resolved.preset_versions,
        "resolved_profile": resolved.resolved_profile,
        "repository_plan": [entry.model_dump(mode="json") for entry in resolved.repository_plan],
        "secret_bindings": [binding.model_dump(mode="json") for binding in resolved.secret_bindings],
        "workspace_plan": resolved.workspace_plan,
        "target_adapter_plan": resolved.target_adapter_plan,
        "warnings": resolved.warnings,
    }
