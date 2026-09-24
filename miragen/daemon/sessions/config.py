"""Session-plane configuration: one YAML file the daemon owns.

The file names the daemon's memory principal and env-var NAMES for its
credentials (values never enter configuration — same rule as agent
profiles), the scope policy that maps a project to Loimi scopes, explicit
per-project bindings, the recall lane, and housekeeping bounds.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from miragen.models import MemoryRecallSpec

logger = logging.getLogger(__name__)

_SCOPE_ID = r"^[a-z0-9][a-z0-9_.:-]{0,126}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectBinding(_Model):
    """An explicit project → scope binding; wins over the template."""

    match: str = Field(
        min_length=1,
        description=(
            "Project identity to match: a normalized remote "
            "('github.com/org/repo'), or an absolute path prefix of the "
            "repository root."
        ),
    )
    scope: str = Field(pattern=_SCOPE_ID, description="The project's write scope.")
    read: list[str] = Field(
        default_factory=list,
        description="Extra read scopes for sessions in this project.",
    )


class ScopePolicy(_Model):
    shared_read: list[str] = Field(
        default_factory=list,
        description="Read-only scopes every external session may draw from "
                    "(e.g. the assistant's profile scope).",
    )
    project_scope: str = Field(
        default="group:project.{slug}",
        description="Template for a project's own scope; {slug} is the "
                    "normalized project identity.",
    )
    project_scope_kind: Literal["instance", "profile", "role", "group", "shared", "fleet"] = "group"
    provision: Literal["auto", "manual"] = Field(
        default="auto",
        description=(
            "'auto' creates a project scope + grants on first sight through "
            "the operator surface (requires operator_token_env to resolve); "
            "'manual' assumes scopes exist — a refusal degrades explicitly."
        ),
    )
    fallback_write: Optional[str] = Field(
        default=None, pattern=_SCOPE_ID,
        description="Scope to use when a project scope cannot be provisioned "
                    "(auto without an operator token). None = degrade.",
    )
    worker_principal: Optional[str] = Field(
        default=None,
        description=(
            "The extraction worker's Loimi principal (`miragen memory-worker`). "
            "Every project scope this daemon provisions is also granted to it "
            "with read/propose/maintain, so the worker can claim that scope's "
            "consolidate jobs. Unset: no worker grants."
        ),
    )
    adopt_by_name: bool = Field(
        default=True,
        description=(
            "A session that could only be identified by its directory name "
            "(a cloud VM reporting a bare path) joins the project the daemon "
            "already knows under that repository name, instead of opening a "
            "parallel `dir:` project. Single-operator deployments want this; "
            "a shared daemon with unrelated same-named repositories does not."
        ),
    )


class StoreNamespaceBinding(_Model):
    """project identity (remote form or path prefix) → Loimi namespace."""

    match: str = Field(min_length=1)
    namespace: str = Field(min_length=1, max_length=128)


class StorePolicy(_Model):
    """Loimi ARTIFACT store participation (the /v0 surface, distinct from
    /memory/v1): every external session gets a run, its episodes land as
    artifacts under that run, and the bridge tools let the model file its
    own artifacts against the same run."""

    enabled: bool = True
    endpoint_env: str = Field(
        default="LOIMI_STORE_URL",
        description="Env var NAME with the store base URL; falls back to the "
                    "memory endpoint (same Loimi) when unset.",
    )
    credential_env: str = Field(
        default="LOIMI_STORE_TOKEN",
        description="Env var NAME with the store bearer; falls back to the "
                    "operator token env (same credential on a Loimi deployment).",
    )
    agent_id: str = Field(
        default="mira", min_length=1,
        description="Registered Loimi agent every external session runs as.",
    )
    namespace: str = Field(
        default="mira", min_length=1,
        description="Namespace a session's run opens in when no binding matches.",
    )
    namespaces: list[StoreNamespaceBinding] = Field(default_factory=list)
    episode_kind: str = Field(default="session_episode", min_length=1)


class BridgeMcp(_Model):
    """The bridge's own MCP surface (memory_* + store_* tools) on /mcp."""

    enabled: bool = True
    default_project: str = Field(
        default="mcp:default", min_length=1,
        description="Project identity used by tool calls that name no "
                    "project and no session (a claude.ai chat, say).",
    )


class SessionsRecall(MemoryRecallSpec):
    on_prompt: bool = Field(
        default=True,
        description="Also run the optional recall lane on every user prompt "
                    "(one selector call per new prompt; cache hits are free).",
    )
    min_prompt_chars: int = Field(default=20, ge=0)
    # Selector endpoint knobs. Daemon-only on purpose: adding fields to the
    # profile-level MemoryRecallSpec (extra=forbid) would change the agent-
    # profile schema and need a profile-contract bump.
    base_url: Optional[str] = Field(
        default=None, min_length=1,
        description="Point a pydantic-ai selector model at an OpenAI- or "
                    "Anthropic-compatible endpoint (a local model server, a "
                    "proxy). `model` must then be openai:, openai-chat:, "
                    "openai-responses: or anthropic:<name>; not valid with "
                    "claude-code:<model>.",
    )
    api_key_env: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="NAME of the env var (or <NAME>_FILE) holding the base_url "
                    "endpoint's key, never the value. Unset = a keyless endpoint; "
                    "the provider's own OPENAI_API_KEY/ANTHROPIC_API_KEY is never "
                    "sent to a custom base_url.",
    )
    timeout_s: Optional[float] = Field(
        default=None, gt=0, le=600,
        description="Bound on one selector call. Unset = the claude-code "
                    "runner's own default; the plane's recall bound applies "
                    "either way.",
    )

    @model_validator(mode="after")
    def _selector_config(self) -> "SessionsRecall":
        from miragen.memory.selection import validate_selector_config

        validate_selector_config(self.model, self.base_url, self.api_key_env)
        return self


class Housekeeping(_Model):
    retention_hours: int = Field(default=24, ge=1)
    stale_after_minutes: int = Field(default=180, ge=1)
    sweep_interval_seconds: int = Field(default=60, ge=5)


class HarnessSetup(_Model):
    """The daemon writes Grok Build and Codex hook + MCP setup into the
    harness homes on THIS machine (miragen_hook/harness_setup.py), at
    startup and every `interval_s`. The URL is where harness sessions should
    report — the hosted bridge, not necessarily this daemon. Environment
    overrides: MIRAGEND_HARNESS_SETUP (on/off), MIRAGEND_HARNESS_SETUP_URL,
    MIRAGEND_HARNESS_SETUP_TOKEN_PATH, MIRAGEND_HARNESS_SETUP_INTERVAL_S."""

    enabled: bool | None = Field(
        default=None,
        description="None = on when a URL is configured and the daemon is not in a "
                    "container; each harness is skipped while its home does not exist.",
    )
    url: str | None = Field(
        default=None, pattern=r"^https?://[^\s$]+$",
        description="Base URL harness sessions report to (hooks + MCP proxy). No default: "
                    "never silently the loopback.",
    )
    token_file: Path | None = Field(
        default=None,
        description="0600 file with that daemon's bearer, baked into the hook commands "
                    "(the token itself is never written into harness config).",
    )
    interval_s: int = Field(default=600, ge=30)


class SessionsConfig(_Model):
    principal: str = Field(
        pattern=r"^[a-z0-9][a-z0-9_.:-]{0,126}$",
        description="The daemon's memory principal id (what the token was minted for).",
    )
    endpoint_env: str = "LOIMI_MEMORY_URL"
    credential_env: str = "LOIMI_MEMORY_TOKEN"
    operator_token_env: str = "LOIMI_OPERATOR_TOKEN"
    provision_principal: bool = Field(
        default=True,
        description=(
            "When the principal credential env is unset and the operator "
            "token is available, create the principal (or mint a token for "
            "an existing one) at startup and persist the token in the state "
            "dir. A hosted daemon then needs exactly one secret: the operator's."
        ),
    )
    scopes: ScopePolicy = Field(default_factory=ScopePolicy)
    projects: list[ProjectBinding] = Field(default_factory=list)
    recall: SessionsRecall = Field(default_factory=SessionsRecall)
    housekeeping: Housekeeping = Field(default_factory=Housekeeping)
    store: StorePolicy = Field(default_factory=StorePolicy)
    mcp: BridgeMcp = Field(default_factory=BridgeMcp)
    harness_setup: HarnessSetup = Field(default_factory=HarnessSetup)
    state_dir: Optional[Path] = Field(
        default=None,
        description="Where sessions.json, the event journal and the memory "
                    "context map live. Default: MIRAGEND_STATE_DIR or "
                    "~/.local/state/miragend.",
    )

    @model_validator(mode="after")
    def _template_has_slug(self) -> "SessionsConfig":
        if "{slug}" not in self.scopes.project_scope:
            raise ValueError("scopes.project_scope must contain {slug}")
        return self

    def resolved_state_dir(self, environ: dict | None = None) -> Path:
        env = os.environ if environ is None else environ
        if self.state_dir is not None:
            return Path(self.state_dir).expanduser()
        return Path(env.get("MIRAGEND_STATE_DIR") or Path.home() / ".local" / "state" / "miragend")


def load_sessions_config(path: str | Path) -> SessionsConfig:
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return SessionsConfig.model_validate(data)


def load_file_secrets(environ: dict | None = None) -> None:
    """Resolve *_FILE env vars into their plain counterparts — the same
    contract as the agent runtime's loader (miragen.app._load_file_secrets),
    kept local so the daemon never imports the agent runtime for it."""
    env = os.environ if environ is None else environ
    for file_var, path in list(env.items()):
        if not file_var.endswith("_FILE"):
            continue
        secret_path = Path(path)
        if not secret_path.exists():
            logger.warning(f"Secret file referenced by {file_var} not found: {path}")
            continue
        target = file_var[: -len("_FILE")]
        try:
            env[target] = secret_path.read_text().strip()
            del env[file_var]
        except OSError as exc:
            logger.error(f"Failed to read secret file {path} for {file_var}: {exc}")
