"""
Lifecycle core: every operation that touches the Docker socket, the swarm
workspace (agents/, compose.yml, exports/), or profile validation.

Extracted from miragen-mcp's server.py so that exactly one process — the
miragend daemon — holds these privileges. The MCP server (and mirarun, and
any other control plane) call this over HTTP via miragen.daemon.api.

Dependencies are injected (docker client, subprocess runner, environ) so the
whole core is testable without a Docker socket; errors are typed
(DaemonError subclasses carrying an HTTP status and a machine-readable code)
instead of the "ERROR: ..." strings the MCP tools speak — the MCP adapter
maps codes back to its LLM-facing guidance.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import logging
import time
import tarfile
import tempfile
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from miragen.load import load_profile
from miragen.profile_contract import (
    PROFILE_CONTRACTS_LABEL,
    parse_contracts_label,
)

logger = logging.getLogger("miragen.daemon")

# Agent names double as directory names, compose service names, and Docker
# container names — restrict them accordingly (also blocks path traversal).
AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"
_AGENT_NAME_RE = re.compile(AGENT_NAME_PATTERN)

# Names an agent may never take: they collide with the management containers
# on miragen-net (container-name DNS), letting an agent shadow the daemon.
RESERVED_AGENT_NAMES = frozenset({"miragend", "miragen-mcp"})

# Agent export/import. Exports are tarballs of an agent workspace, excluding
# run history and caches; imports must come from the workspace exports/
# directory and extract with tarfile's "data" filter (rejects absolute paths,
# traversal, and links).
EXPORT_EXCLUDE_DIRS = frozenset({"runs", "__pycache__"})
EXPORT_EXCLUDE_FILES = frozenset({"history.json"})
MAX_EXPORT_FILE_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024

# Default cap on concurrently-managed agents/containers, and default
# per-container resource limits applied to every managed agent's compose
# service. All three are overridable per-deployment via environment
# variables so hosts with more (or less) headroom can tune them without a
# code change — see create_agent() and _compose_add_service() below.
DEFAULT_MAX_AGENTS = 10
DEFAULT_AGENT_CPU_LIMIT = "1.0"
DEFAULT_AGENT_MEM_LIMIT = "512m"

# How long create/update watches a freshly (re)started container before
# declaring first boot good — long enough to catch a profile the runtime
# rejects at startup (that dies in ~1-2s), short enough not to hold the
# API. 0 means a single immediate status check (used by tests).
DEFAULT_BOOT_PROBE_SECONDS = 10
# How long the container must hold 'running' before first boot counts as
# good, and how often that is checked. The settle window is the latency a
# healthy create pays; the probe budget above is only spent on a container
# that keeps failing to settle.
BOOT_SETTLE_SECONDS = 2.0
BOOT_POLL_SECONDS = 0.5

# Subscription credentials a vendor runtime is known to read from a single
# env variable, keyed by the executor kind that consumes them. These are
# auto-forwarded — no MIRAGEND_AGENT_ENV_PASSTHROUGH entry — but only into
# agents whose profile declares that executor kind, so an agent that never
# runs claude-code never sees the subscription token. claude-code is the
# only kind here on purpose: the other products authenticate via home
# volumes (docs/design/subscription-homes.md), not a portable env token.
EXECUTOR_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "claude-code": ("CLAUDE_CODE_OAUTH_TOKEN",),
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DaemonError(Exception):
    """Base for all lifecycle errors. Serialized by the API layer as
    {"detail": str(exc), "code": exc.code} with HTTP status exc.status."""

    status = 500
    code = "internal_error"

    def __init__(self, detail: str, **extra: object) -> None:
        super().__init__(detail)
        self.extra = extra


class InvalidAgentName(DaemonError):
    status = 400
    code = "invalid_agent_name"


class InvalidPath(DaemonError):
    status = 400
    code = "invalid_path"


class InvalidInput(DaemonError):
    status = 400
    code = "invalid_input"


class AgentNotFound(DaemonError):
    status = 404
    code = "agent_not_found"


class WorkspaceFileNotFound(DaemonError):
    status = 404
    code = "file_not_found"


class ToolNotFound(DaemonError):
    status = 404
    code = "tool_not_found"


class ArchiveNotFound(DaemonError):
    status = 404
    code = "archive_not_found"


class ContainerNotFound(DaemonError):
    status = 404
    code = "container_not_found"


class AgentExists(DaemonError):
    status = 409
    code = "agent_exists"


class EditConflict(DaemonError):
    """old_str missing or ambiguous in a scoped string replacement."""

    status = 409
    code = "edit_conflict"


class NameMismatch(DaemonError):
    status = 422
    code = "name_mismatch"


class ValidationFailed(DaemonError):
    status = 422
    code = "validation_failed"


class ToolSourceInvalid(DaemonError):
    status = 422
    code = "tool_source_invalid"


class ArchiveInvalid(DaemonError):
    status = 422
    code = "archive_invalid"


class ArchiveTooLarge(DaemonError):
    status = 422
    code = "archive_too_large"


class AgentLimitExceeded(DaemonError):
    status = 429
    code = "agent_limit_exceeded"


class ContainerOperationFailed(DaemonError):
    status = 502
    code = "container_operation_failed"


class ImageContractUnsupported(DaemonError):
    """The runtime image this daemon would spawn does not declare support
    for the profile's required contract (#75) — refused at create, in the
    caller's face, instead of a crash loop discovered in container logs."""

    status = 409
    code = "image_contract_unsupported"


class AgentBootFailed(DaemonError):
    """The container started but the runtime died during first boot. The
    create is rolled back and the boot log excerpt rides in the error, so
    the caller learns synchronously what the runtime rejected."""

    status = 502
    code = "agent_boot_failed"


class RestartFailed(DaemonError):
    status = 502
    code = "restart_failed"


# ---------------------------------------------------------------------------
# Validation (native — this process IS miragen)
# ---------------------------------------------------------------------------


def format_validation_error(exc: ValidationError) -> str:
    """The same rendering `miragen validate` prints, without click/colors."""
    lines = [f"Invalid profile — {exc.error_count()} error(s):"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        msg = err["msg"]
        if err["type"] == "extra_forbidden":
            msg = "unknown field — check spelling against the profile reference in the README"
        lines.append(f"  {loc}: {msg}")
    return "\n".join(lines)


def validate_profile_text(source: str) -> dict:
    """Validate an agent profile YAML string without touching any agent.

    Returns a summary dict on success; raises ValidationFailed with the
    formatted error text otherwise. Runs load_profile in-process — the same
    code path `miragen validate` uses (the CLI subprocess the MCP used to
    shell out to never loaded agent tools either: its cwd had no tools.py).
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(source)
        tmp = Path(f.name)
    try:
        profile = load_profile(tmp)
    except ValidationError as exc:
        raise ValidationFailed(format_validation_error(exc)) from exc
    except Exception as exc:
        raise ValidationFailed(f"Invalid profile: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)

    summary: dict = {
        "name": profile.name,
        "mode": profile.mode,
        "triggers": [t.type for t in profile.triggers],
        "tools": list(profile.tools or []),
        # Named @register_handler targets this profile depends on. post_to is
        # excluded on purpose: it is a plain webhook URL, not a registry
        # lookup, so it cannot be unsatisfied by a missing tools.py.
        "handlers": [
            name
            for name in (
                (profile.on_complete.log_to, profile.on_complete.notify)
                if profile.on_complete
                else ()
            )
            if name
        ],
    }
    if profile.is_executor:
        summary["executor"] = profile.executor.executor
    else:
        summary["model"] = profile.spec.model
        summary["capabilities"] = list(profile.spec.capabilities or [])
    # The contract level a runtime must support to execute this profile —
    # what miragend checks the spawn image against (#75).
    summary["profile_contract"] = profile.required_contract()
    if profile.api_version:
        summary["api_version"] = profile.api_version
    return summary


# ---------------------------------------------------------------------------
# Tool-source parsing (AST — moved verbatim from miragen-mcp)
# ---------------------------------------------------------------------------


def parse_registered_tools(source: str) -> list[dict]:
    """Return metadata for every @register-decorated async def in source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    tools = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            tool_name = node.name
            if isinstance(dec, ast.Name) and dec.id == "register":
                pass
            elif (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Name)
                and dec.func.id == "register"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                tool_name = dec.args[0].value
            else:
                continue
            args = [a.arg for a in node.args.args]
            tools.append(
                {
                    "name": tool_name,
                    "description": ast.get_docstring(node) or "",
                    "signature": f"({', '.join(args)})",
                }
            )
            break
    return tools


def parse_registered_handlers(source: str) -> list[str]:
    """Return every name registered with @register_handler("name") in source.

    Counterpart to parse_registered_tools, for the on_complete side. Accepts
    both `def` and `async def`: the dispatcher awaits handlers, but a sync one
    is a mistake worth catching at load rather than hiding here by refusing to
    see it.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Name)
                and dec.func.id == "register_handler"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                names.append(dec.args[0].value)
                break
    return names


def _assert_profile_satisfied_by_tools(summary: dict, tools_source: str | None) -> None:
    """Refuse to create an agent the supplied tools source cannot support.

    Creation writes agent.yaml, writes tools.py, and starts the container in
    one breath. Before this check, a profile naming tools or on_complete
    handlers could be created against the empty placeholder tools.py, and the
    daemon would answer 201 for an agent that then failed on every single boot:
    `_inject_tools` raises on unknown tool names, and the lifespan now raises on
    unregistered handler names. The caller saw success; the agent was dead.

    So the profile and its tools are checked together, before anything starts.
    Either the pair is coherent and the agent runs, or creation is refused with
    the specific names that are missing — no half-provisioned agent in between.

    Deliberately a check against the source actually being written, not against
    the live registry: this runs in the daemon, which never imports an agent's
    tools.py.
    """
    source = tools_source or ""
    available_tools = {t["name"] for t in parse_registered_tools(source)}
    available_handlers = set(parse_registered_handlers(source))

    missing_tools = [t for t in summary.get("tools", []) if t not in available_tools]
    missing_handlers = [
        h for h in summary.get("handlers", []) if h not in available_handlers
    ]
    if not missing_tools and not missing_handlers:
        return

    parts = []
    if missing_tools:
        parts.append(f"tools {missing_tools}")
    if missing_handlers:
        parts.append(f"on_complete handlers {missing_handlers}")
    supplied = (
        f"the supplied tools source registers tools {sorted(available_tools)} "
        f"and handlers {sorted(available_handlers)}"
        if tools_source is not None
        else "no tools source was supplied, so tools.py would be an empty placeholder"
    )
    raise InvalidInput(
        f"profile requires {' and '.join(parts)}, but {supplied}. "
        "Pass `tools_source` with the matching @register / @register_handler "
        "definitions, or remove the references from the profile — creating this "
        "agent would produce one that fails on every boot."
    )


def _registered_name(node: ast.AsyncFunctionDef) -> str | None:
    """The name a @register-decorated async def is registered under, or None."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "register":
            return node.name
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Name)
            and dec.func.id == "register"
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
        ):
            return dec.args[0].value
    return None


def find_function_span(source: str, tool_name: str) -> tuple[int, int] | None:
    """Return (start_line_0indexed, end_line_exclusive) for the top-level async
    def whose registered tool name — the alias in @register("alias"), else the
    function identifier — equals tool_name. Falls back to a plain function-name
    match so undecorated helpers remain addressable."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    fallback: tuple[int, int] | None = None
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        start = node.decorator_list[0].lineno - 1 if node.decorator_list else node.lineno - 1
        span = (start, node.end_lineno)
        if _registered_name(node) == tool_name:
            return span
        if node.name == tool_name and fallback is None:
            fallback = span
    return fallback


# ---------------------------------------------------------------------------
# The core
# ---------------------------------------------------------------------------


def _default_not_found() -> type[Exception]:
    try:
        import docker.errors

        return docker.errors.NotFound
    except ImportError:  # pragma: no cover - tests always inject
        class _NeverRaised(Exception):
            pass

        return _NeverRaised


class LifecycleCore:
    """All swarm lifecycle operations, behind injected Docker/subprocess seams.

    `docker_client` needs: .containers.get(name) -> obj with .status/.stop()/
    .remove()/.restart()/.logs(tail=, stream=False); .networks.get/.create.
    `runner` is subprocess.run-compatible and receives the `docker compose`
    invocations. `not_found` is the exception .containers.get raises for a
    missing container (docker.errors.NotFound in production).
    """

    def __init__(
        self,
        workspace: Path,
        docker_client,
        *,
        base_image: str,
        internal_token: str = "",
        environ: Mapping[str, str] | None = None,
        runner: Callable = subprocess.run,
        not_found: type[Exception] | None = None,
        sleep: Callable = time.sleep,
    ) -> None:
        self.workspace = Path(workspace)
        self.agents_dir = self.workspace / "agents"
        self.compose_file = self.workspace / "compose.yml"
        self.exports_dir = self.workspace / "exports"
        self._docker = docker_client
        self._base_image = base_image
        self._internal_token = internal_token
        self._environ = os.environ if environ is None else environ
        self._run = runner
        self._sleep = sleep
        self._not_found = not_found or _default_not_found()
        # create_agent/import_agent both do a check-then-act against
        # _agent_count() before committing a new agent (mkdir / shutil.move).
        # The daemon's request handlers can run concurrently in separate
        # worker threads, so without serializing that check+commit pair two
        # racing requests can both observe the same under-cap count and both
        # proceed — even with MIRAGEND_MAX_AGENTS=1. This lock closes that
        # window; hold it only across the count check through the step that
        # actually commits the new agent, not the slower I/O around it.
        self._agent_creation_lock = threading.Lock()

    # -- naming / paths -----------------------------------------------------

    def check_name(self, name: str) -> None:
        if not _AGENT_NAME_RE.fullmatch(name):
            raise InvalidAgentName(
                f"invalid agent name '{name}': must match {AGENT_NAME_PATTERN} "
                "(lowercase letters, digits, hyphens, underscores; max 63 chars)"
            )
        if name in RESERVED_AGENT_NAMES:
            raise InvalidAgentName(
                f"'{name}' is reserved for the miragen management containers"
            )

    def _agent_dir(self, name: str) -> Path:
        return self.agents_dir / name

    def _require_agent(self, name: str) -> Path:
        self.check_name(name)
        d = self._agent_dir(name)
        if not d.exists():
            raise AgentNotFound(f"agent '{name}' not found")
        return d

    def _safe_path(self, agent: str, rel: str) -> Path:
        if ".." in rel:
            raise InvalidPath("path traversal not allowed")
        p = Path(rel)
        if p.is_absolute():
            raise InvalidPath("path traversal not allowed")
        base = self._agent_dir(agent).resolve()
        full = (base / p).resolve()
        if not str(full).startswith(str(base) + os.sep) and full != base:
            raise InvalidPath("path traversal not allowed")
        return full

    def _safe_export_path(self, archive_path: str) -> Path:
        if ".." in archive_path:
            raise InvalidPath("path traversal not allowed")
        base = self.exports_dir.resolve()
        p = Path(archive_path)
        if p.is_absolute():
            full = p.resolve()
        else:
            rel = p
            if rel.parts and rel.parts[0] == "exports":
                rel = Path(*rel.parts[1:])
            full = (base / rel).resolve()
        if full != base and not str(full).startswith(str(base) + os.sep):
            raise InvalidPath(
                "archive_path must be inside the workspace exports/ directory"
            )
        return full

    # -- container helpers --------------------------------------------------

    def container_status(self, name: str) -> str:
        try:
            return self._docker.containers.get(name).status
        except self._not_found:
            return "not found"
        except Exception as exc:
            return f"error: {exc}"

    def ensure_network(self) -> None:
        try:
            self._docker.networks.get("miragen-net")
        except self._not_found:
            self._docker.networks.create("miragen-net", driver="bridge", attachable=True)

    def _compose_up(self, name: str) -> None:
        result = self._run(
            ["docker", "compose", "up", "-d", name],
            capture_output=True,
            text=True,
            cwd=self.workspace,
        )
        if result.returncode != 0:
            raise ContainerOperationFailed(
                f"container failed to start: {result.stderr.strip()}"
            )

    # -- profile↔runtime contract (#75) -------------------------------------
    #
    # One rule governs this whole section: the contract is judged against the
    # exact image that will execute the profile, and that same image object
    # is what gets pinned or restarted. Re-looking-up a tag between the check
    # and the spawn would reintroduce the swap the digest pin exists to
    # prevent (Codex review, PR #76).

    def _pull_ref(self, ref: str) -> None:
        try:
            self._docker.images.pull(ref)
        except Exception as exc:
            raise ImageContractUnsupported(
                f"cannot pull runtime image '{ref}': {exc} — the profile "
                "contract cannot be verified against an image the daemon "
                "cannot see"
            ) from exc

    def _image_for_ref(self, ref: str, *, refresh: bool = False):
        """The image object for `ref`. Pulled when absent — the contract must
        be judged against what will actually run, and an image not on the
        host yet would otherwise be judged blind — or when `refresh` asks for
        the registry's current answer."""
        if refresh:
            self._pull_ref(ref)
        try:
            return self._docker.images.get(ref)
        except self._not_found:
            pass
        self._pull_ref(ref)
        try:
            return self._docker.images.get(ref)
        except Exception as exc:
            raise ImageContractUnsupported(
                f"cannot inspect runtime image '{ref}': {exc} — the profile "
                "contract cannot be verified against an image the daemon "
                "cannot see"
            ) from exc

    @staticmethod
    def _is_mutable_ref(ref: str) -> bool:
        """A tag can be re-pointed upstream; a digest cannot. Only a mutable
        reference is worth re-pulling when the local copy disappoints."""
        return "@" not in ref

    def _contracts_of(self, image, ref: str) -> set[int]:
        """Contract levels an image declares (its OCI label). An unlabeled
        image predates contract labeling: a pre-executor-tier runtime,
        contract 1 only."""
        labels = image.labels or {}
        try:
            return parse_contracts_label(labels.get(PROFILE_CONTRACTS_LABEL))
        except ValueError as exc:
            raise ImageContractUnsupported(
                f"runtime image '{ref}' carries a malformed "
                f"{PROFILE_CONTRACTS_LABEL} label: {exc}"
            ) from exc

    def _validated_image(self, ref: str, required: int):
        """Return the image object at `ref` after proving it can execute a
        profile requiring `required` — the caller pins or restarts THIS
        object, never a fresh lookup of the same tag."""
        image = self._image_for_ref(ref)
        supported = self._contracts_of(image, ref)
        if required not in supported and self._is_mutable_ref(ref):
            # A locally cached tag that disappoints is far more often stale
            # than genuinely incapable — `latest` moves upstream, Compose
            # trusts the cache, and the host silently keeps last month's
            # runtime. That exact staleness caused the failure #75 exists to
            # prevent, so refresh once from the registry before refusing:
            # the daemon repairs what it can and only reports what it can't.
            logger.info(
                "runtime image '%s' does not satisfy profile contract %d — "
                "re-pulling in case the local copy is stale",
                ref,
                required,
            )
            image = self._image_for_ref(ref, refresh=True)
            supported = self._contracts_of(image, ref)
        if required not in supported:
            mutable = self._is_mutable_ref(ref)
            raise ImageContractUnsupported(
                f"runtime image '{ref}' declares profile contract(s) "
                f"{sorted(supported)} but this profile requires contract "
                f"{required}"
                + (
                    " — and this is the registry's current answer, not a "
                    "stale local copy (the daemon re-pulled before refusing)"
                    if mutable
                    else " (a digest-pinned image; its contract cannot change)"
                )
                + ". Point MIRAGEN_BASE_IMAGE at a runtime that supports the "
                "contract, or lower the profile's requirement; spawning "
                "anyway would crash-loop at boot (#75).",
                required=required,
                supported=sorted(supported),
                image=ref,
            )
        return image

    def _digest_ref_for(self, image, ref: str) -> str:
        """The immutable reference for an image object: what was validated is
        what runs. Falls back to `ref` for images without a matching repo
        digest (e.g. built locally, never pushed)."""
        if not self._is_mutable_ref(ref):
            return ref
        repo = self._repo_of(ref)
        for digest_ref in (image.attrs or {}).get("RepoDigests") or []:
            if digest_ref.startswith(repo + "@"):
                return digest_ref
        return ref

    def _validated_spawn_ref(self, required: int) -> str:
        """Create/import path: prove the configured base image can run the
        profile, then return the reference to write into the compose
        service — derived from the very image object that passed."""
        image = self._validated_image(self._base_image, required)
        return self._digest_ref_for(image, self._base_image)

    def _service_image(self, name: str) -> str | None:
        """The image reference pinned in an existing agent's compose service
        — the image that agent actually runs, which is what an in-place
        restart will re-execute."""
        if not self.compose_file.exists():
            return None
        service = (self._compose_load().get("services") or {}).get(name) or {}
        return service.get("image")

    @staticmethod
    def _repo_of(ref: str) -> str:
        """Image reference minus its tag ('ghcr.io/x/y:latest' -> the repo).
        A colon inside the last path segment is a tag; a colon before a
        slash is a registry port and is left alone."""
        head, sep, tail = ref.rpartition(":")
        if sep and "/" not in tail:
            return head
        return ref

    def _teardown_container(self, name: str) -> None:
        try:
            container = self._docker.containers.get(name)
            container.stop()
            container.remove()
        except self._not_found:
            pass
        except Exception:
            # Rollback best-effort: the compose service removal and the
            # workspace removal still proceed; an orphaned stopped
            # container is recoverable, a half-registered agent is worse.
            logger.warning("could not remove container '%s' during rollback", name)

    def _watch_first_boot(self, name: str) -> None:
        """Watch a freshly (re)started container until it has held `running`
        long enough to call the boot good, or until it dies. A runtime that
        rejects the profile exits within a second or two and the restart
        policy flips the container to 'restarting' — that is a boot failure
        to surface with its logs, not a success to discover later in
        `docker logs` (#75).

        The settle window is what keeps this cheap: a healthy create returns
        as soon as the container has been up for BOOT_SETTLE_SECONDS rather
        than holding the API open for the whole probe budget, which is only
        spent on a container that never settles.
        """
        probe_seconds = float(
            self._environ.get("MIRAGEND_BOOT_PROBE_SECONDS", DEFAULT_BOOT_PROBE_SECONDS)
        )
        settle_seconds = min(BOOT_SETTLE_SECONDS, probe_seconds)
        waited = 0.0
        running_for = 0.0
        last_step = 0.0
        while True:
            status = self.container_status(name)
            if status in ("exited", "dead", "restarting"):
                try:
                    excerpt = self.agent_logs(name, tail=40)
                except Exception:
                    excerpt = "(logs unavailable)"
                raise AgentBootFailed(
                    f"agent '{name}' failed first boot (container status: "
                    f"{status}). Last log lines:\n{excerpt}",
                    container_status=status,
                )
            running_for = running_for + last_step if status == "running" else 0.0
            if status == "running" and running_for >= settle_seconds:
                return
            if waited >= probe_seconds:
                return
            last_step = min(BOOT_POLL_SECONDS, probe_seconds - waited)
            self._sleep(last_step)
            waited += last_step

    def _read_yaml(self, path: Path) -> dict:
        with open(path) as f:
            return yaml.safe_load(f) or {}

    def _write_yaml(self, path: Path, data: dict) -> None:
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def _secret_names(self) -> list[str]:
        """Docker secret names derived from non-empty *_API_KEY_FILE env vars."""
        return [
            Path(v).name
            for k, v in self._environ.items()
            if k.endswith("_API_KEY_FILE") and v
        ]

    def _compose_load(self) -> dict:
        if self.compose_file.exists():
            return self._read_yaml(self.compose_file)
        return {
            "secrets": {k: {"external": True} for k in self._secret_names()},
            "services": {},
            "networks": {"miragen-net": {"external": True}},
        }

    def _agent_executor_kind(self, name: str) -> str:
        """Executor kind from the agent's on-disk profile — every caller of
        _compose_add_service (create, import, config update) has written
        agent.yaml before the service is composed.

        Resolved through validate_profile_text, i.e. the loader, NOT a raw
        YAML read: `executor: ${EXECUTOR_KIND:-claude-code}` is a valid
        profile, and interpolate_env expands it before Pydantic ever sees
        it. Reading the unprocessed file returns the literal placeholder and
        the credential lookup silently misses. Interpolation resolves against
        this daemon's own os.environ — the same environment the forwarded
        value is read from, so the two cannot disagree.

        Model-tier profiles and unreadable YAML yield "", which maps to no
        auto-forwarded credential."""
        try:
            text = (self._agent_dir(name) / "agent.yaml").read_text()
            return str(validate_profile_text(text).get("executor") or "")
        except Exception:
            return ""

    def _compose_add_service(self, name: str, image_ref: str | None = None) -> None:
        self.ensure_network()
        secret_names = self._secret_names()
        env = {"AGENT_PROFILE": "agent.yaml"}
        if self._internal_token:
            # Enable the agent's own /run* guard with the same shared token
            # this daemon (and its clients) authenticate with. Without it a
            # managed agent boots unprotected while callers send
            # X-Miragen-Token — the header is required by no one.
            env["MIRAGEN_INTERNAL_TOKEN"] = self._internal_token
        for k, v in self._environ.items():
            if (k.endswith("_API_KEY_FILE") or k.endswith("_API_KEY")) and v:
                env[k] = v
        # Operator-named passthrough for credentials the suffix filter above
        # cannot see — subscription OAuth env tokens foremost (e.g. the
        # long-lived token Claude Code mints via `claude setup-token`, which
        # is the subscription-primary path of docs/design/
        # subscription-homes.md delivered as a single variable instead of a
        # shared home volume). Names, never values: a variable is forwarded
        # only when the operator listed it here AND it is set non-empty in
        # the daemon's own environment, so the daemon still cannot be talked
        # into minting credentials it does not hold.
        passthrough = self._environ.get("MIRAGEND_AGENT_ENV_PASSTHROUGH", "")
        for raw_name in passthrough.split(","):
            key = raw_name.strip()
            if key and self._environ.get(key):
                env[key] = self._environ[key]
        # Executor-kind-scoped auto-forward: the profile already names the
        # vendor runtime it needs, so the well-known credential for that
        # runtime is delivered without any passthrough entry — the operator
        # surface is "set the variable", nothing else. Scoped by kind, not
        # broadcast: only agents whose profile declares the consuming
        # executor receive it.
        for cred in EXECUTOR_CREDENTIAL_ENV.get(self._agent_executor_kind(name), ()):
            if self._environ.get(cred):
                env.setdefault(cred, self._environ[cred])

        # Every managed agent gets a default resource ceiling so a single
        # runaway container can't starve the host — overridable per-deployment
        # via MIRAGEND_AGENT_CPU_LIMIT / MIRAGEND_AGENT_MEM_LIMIT (see #57).
        cpu_limit = self._environ.get("MIRAGEND_AGENT_CPU_LIMIT", DEFAULT_AGENT_CPU_LIMIT)
        mem_limit = self._environ.get("MIRAGEND_AGENT_MEM_LIMIT", DEFAULT_AGENT_MEM_LIMIT)

        data = self._compose_load()
        data["networks"] = {"miragen-net": {"external": True}}
        data.setdefault("secrets", {}).update({s: {"external": True} for s in secret_names})
        data.setdefault("services", {})[name] = {
            # The digest of the very image the contract check validated —
            # what was validated is what runs; a re-tagged `latest` cannot
            # swap the runtime under a validated profile, and this is not a
            # second lookup of the tag (#75, Codex review on #76).
            "image": image_ref or self._base_image,
            "container_name": name,
            "restart": "unless-stopped",
            "secrets": secret_names,
            "environment": env,
            "volumes": [f"./agents/{name}:/agent"],
            "networks": ["miragen-net"],
            "cpus": cpu_limit,
            "mem_limit": mem_limit,
            # Managed-by markers for host tooling: miragend's containers
            # belong to no compose stack a scoped operator can see, so at
            # minimum they identify themselves (#75 companion).
            "labels": {
                "io.miragen.agent": "true",
                "io.miragen.managed-by": "miragend",
            },
        }
        self._write_yaml(self.compose_file, data)

    def _compose_remove_service(self, name: str) -> None:
        if not self.compose_file.exists():
            return
        data = self._compose_load()
        data.get("services", {}).pop(name, None)
        self._write_yaml(self.compose_file, data)

    # -- registry -----------------------------------------------------------

    def list_agents(self) -> list[dict]:
        result = []
        if self.agents_dir.exists():
            for entry in sorted(self.agents_dir.iterdir()):
                if not entry.is_dir():
                    continue
                name = entry.name
                mode = model = ""
                yaml_path = entry / "agent.yaml"
                if yaml_path.exists():
                    try:
                        data = self._read_yaml(yaml_path)
                        mode = data.get("mode", "")
                        model = (data.get("spec") or {}).get("model", "")
                    except Exception:
                        pass
                result.append(
                    {
                        "name": name,
                        "status": self.container_status(name),
                        "mode": mode,
                        "model": model,
                        # Reachable by any container on miragen-net
                        # (container-name DNS); identity comes from this
                        # daemon's own state, never from the agent.
                        "endpoint": f"http://{name}:8000",
                    }
                )
        return result

    def get_agent(self, name: str) -> dict:
        d = self._require_agent(name)
        yaml_path = d / "agent.yaml"
        return {
            "name": name,
            "yaml": yaml_path.read_text() if yaml_path.exists() else "",
            "status": self.container_status(name),
            "has_tools": (d / "tools.py").exists(),
            "endpoint": f"http://{name}:8000",
        }

    # -- lifecycle ----------------------------------------------------------

    def _agent_count(self) -> int:
        """Number of agent workspaces currently managed by this daemon —
        the same population create_agent() checks for name collisions, used
        here as the concurrency count for the MIRAGEND_MAX_AGENTS cap."""
        if not self.agents_dir.exists():
            return 0
        return sum(1 for entry in self.agents_dir.iterdir() if entry.is_dir())

    def create_agent(
        self, name: str, yaml_source: str, tools_source: str | None = None
    ) -> None:
        self.check_name(name)
        d = self._agent_dir(name)
        if d.exists():
            raise AgentExists(f"agent '{name}' already exists")

        # Validate before touching the cap/lock: it's pure computation (no
        # effect on _agent_count()), so it belongs outside the critical
        # section — the lock below should cover only the check-then-act pair.
        profile_name = None
        try:
            summary = validate_profile_text(yaml_source)
            profile_name = summary.get("name")
        except ValidationFailed:
            raise
        if profile_name != name:
            raise NameMismatch(
                f"profile 'name' field is '{profile_name}' but the agent is being "
                f"created as '{name}' — set 'name: {name}' in the YAML"
            )

        _assert_profile_satisfied_by_tools(summary, tools_source)

        # The contract is judged against the image that will actually run
        # the profile — never against this daemon's own bundled miragen,
        # which is the two-interpreters gap of the motivating failure (#75).
        # The returned reference IS the validated image, carried to the
        # compose service rather than re-resolved.
        spawn_ref = self._validated_spawn_ref(summary["profile_contract"])

        max_agents = int(self._environ.get("MIRAGEND_MAX_AGENTS", DEFAULT_MAX_AGENTS))
        # Check-then-act: reading the count and creating the directory that
        # count is derived from must be atomic, or two concurrent callers can
        # both pass the check before either directory exists (see #61 review).
        with self._agent_creation_lock:
            current = self._agent_count()
            if current >= max_agents:
                raise AgentLimitExceeded(
                    f"agent limit reached ({current}/{max_agents}) — delete an existing "
                    "agent or raise MIRAGEND_MAX_AGENTS before creating another",
                    limit=max_agents,
                    current=current,
                )
            try:
                d.mkdir(parents=True)
            except Exception as exc:
                raise DaemonError(str(exc)) from exc

        try:
            (d / "agent.yaml").write_text(yaml_source)
            (d / "tools.py").write_text(
                tools_source
                if tools_source is not None
                else f"from miragen import register\n\n# Tools for {name}\n"
            )
            self._compose_add_service(name, spawn_ref)
            try:
                self._compose_up(name)
                self._watch_first_boot(name)
            except AgentBootFailed:
                # The container came up and died: tear it down so the
                # rollback leaves nothing crash-looping behind the error.
                self._teardown_container(name)
                self._compose_remove_service(name)
                raise
            except ContainerOperationFailed:
                self._compose_remove_service(name)
                raise
        except DaemonError:
            shutil.rmtree(d, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(d, ignore_errors=True)
            raise DaemonError(str(exc)) from exc

    def update_agent_config(self, name: str, yaml_source: str) -> dict:
        d = self._require_agent(name)
        yaml_path = d / "agent.yaml"
        if not yaml_path.exists():
            raise AgentNotFound(f"agent '{name}' has no agent.yaml")

        summary = validate_profile_text(yaml_source)
        if summary.get("name") != name:
            raise NameMismatch(
                f"profile 'name' field is '{summary.get('name')}' but the agent "
                f"being updated is '{name}' — set 'name: {name}' in the YAML"
            )

        old_kind_for_contract = self._agent_executor_kind(name)
        will_recreate = str(summary.get("executor") or "") != old_kind_for_contract

        # Same boundary as create, against the RIGHT image — which depends
        # on what this update will actually do. A kind change recreates the
        # container from the base image (validate and pin that, exactly like
        # create); anything else restarts in place and re-executes the image
        # pinned in the service, so THAT is what must satisfy the contract.
        # Checking the base tag on the restart path would both reject
        # updates the running image supports and admit updates that crash
        # it (Codex review, PR #76). Falls back to the base image for agents
        # created before service pinning existed.
        if will_recreate:
            spawn_ref = self._validated_spawn_ref(summary["profile_contract"])
        else:
            spawn_ref = None
            running_ref = self._service_image(name) or self._base_image
            self._validated_image(running_ref, summary["profile_contract"])

        original = yaml_path.read_text()
        try:
            old_data = yaml.safe_load(original)
        except Exception:
            old_data = {}
        if not isinstance(old_data, dict):
            old_data = {}
        try:
            new_data = yaml.safe_load(yaml_source)
        except Exception:
            new_data = {}
        if not isinstance(new_data, dict):
            new_data = {}

        # A change of executor kind changes which credentials the service is
        # entitled to, and container environment is fixed at create time --
        # restart() preserves it. Recompose and let compose recreate the
        # container instead, or an agent switched TO claude-code runs without
        # its subscription token while one switched AWAY keeps holding it.
        old_kind = self._agent_executor_kind(name)
        new_kind = str(summary.get("executor") or "")

        yaml_path.write_text(yaml_source)
        try:
            self._apply_config(
                name, recreate=new_kind != old_kind, image_ref=spawn_ref
            )
            self._watch_first_boot(name)
        except DaemonError as exc:
            yaml_path.write_text(original)
            try:
                self._apply_config(name, recreate=new_kind != old_kind)
            except DaemonError:
                pass
            raise RestartFailed(
                "new config applied but restart failed — previous config restored: "
                f"{exc}"
            ) from exc

        changed = sorted(
            k for k in (set(old_data) | set(new_data)) if old_data.get(k) != new_data.get(k)
        )
        return {"changed_keys": changed}

    # Every Docker operation below gates on _require_agent first: the daemon
    # may only manage containers whose workspace it owns. Without the gate, a
    # client could drive restart/stop/logs/delete against ANY container on the
    # host whose name fits the agent-name grammar (e.g. 'postgres') — Docker
    # resolves by container name, not by who created the container.

    def start_agent(self, name: str) -> None:
        self._require_agent(name)
        self._compose_up(name)

    def _apply_config(
        self, name: str, *, recreate: bool, image_ref: str | None = None
    ) -> None:
        """Put a rewritten agent.yaml into effect. A plain restart re-reads
        the mounted profile, which is all most edits need; `recreate` also
        regenerates the compose service so `compose up` replaces the
        container, picking up an environment the running one cannot change.

        `image_ref` is the runtime the caller validated for the new profile
        — required on the recreate path, because regenerating the service
        without it would rewrite the mutable tag and discard the digest pin
        the contract check established (#75 + Codex review on #76)."""
        if recreate:
            self._compose_add_service(name, image_ref)
            self._compose_up(name)
        else:
            self.restart_agent(name)

    def restart_agent(self, name: str) -> None:
        self._require_agent(name)
        try:
            self._docker.containers.get(name).restart()
        except self._not_found:
            raise ContainerNotFound(f"container '{name}' not found") from None
        except Exception as exc:
            raise ContainerOperationFailed(str(exc)) from exc

    def stop_agent(self, name: str) -> None:
        self._require_agent(name)
        try:
            self._docker.containers.get(name).stop()
        except self._not_found:
            raise ContainerNotFound(f"container '{name}' not found") from None
        except Exception as exc:
            raise ContainerOperationFailed(str(exc)) from exc

    def delete_agent(self, name: str) -> None:
        d = self._require_agent(name)
        try:
            container = self._docker.containers.get(name)
            container.stop()
            container.remove()
        except self._not_found:
            pass
        except Exception as exc:
            raise ContainerOperationFailed(str(exc)) from exc
        self._compose_remove_service(name)
        if d.exists():
            shutil.rmtree(d)

    def agent_logs(self, name: str, tail: int = 50) -> str:
        self._require_agent(name)
        try:
            logs = self._docker.containers.get(name).logs(
                tail=min(max(tail, 1), 1000), stream=False
            )
        except self._not_found:
            raise ContainerNotFound(f"container '{name}' not found") from None
        except Exception as exc:
            raise ContainerOperationFailed(str(exc)) from exc
        return logs.decode("utf-8", errors="replace")

    # -- tool management ----------------------------------------------------

    def list_tools(self, agent: str) -> list[dict]:
        d = self._require_agent(agent)
        p = d / "tools.py"
        if not p.exists():
            return []
        return parse_registered_tools(p.read_text())

    def tool_source(self, agent: str, tool_name: str) -> str:
        d = self._require_agent(agent)
        p = d / "tools.py"
        if not p.exists():
            raise WorkspaceFileNotFound(f"tools.py not found for agent '{agent}'")
        source = p.read_text()
        span = find_function_span(source, tool_name)
        if span is None:
            raise ToolNotFound(f"tool '{tool_name}' not found in {agent}/tools.py")
        lines = source.splitlines(keepends=True)
        return "".join(lines[span[0] : span[1]])

    def register_tool(self, agent: str, tool_name: str, source: str) -> None:
        d = self._require_agent(agent)
        tools_path = d / "tools.py"
        yaml_path = d / "agent.yaml"
        if not tools_path.exists():
            raise WorkspaceFileNotFound(f"tools.py not found for agent '{agent}'")

        try:
            ast.parse(source)
        except SyntaxError as exc:
            raise ToolSourceInvalid(f"source is not valid Python: {exc}") from exc
        parsed = parse_registered_tools(source)
        if not any(t["name"] == tool_name for t in parsed):
            found = [t["name"] for t in parsed] or "none"
            raise ToolSourceInvalid(
                "source does not define a @register-decorated async function "
                f"registered as '{tool_name}' (found: {found})"
            )

        original_tools = tools_path.read_text()
        original_yaml = yaml_path.read_text() if yaml_path.exists() else None
        try:
            tools_path.write_text(
                original_tools.rstrip("\n") + "\n\n" + source.strip() + "\n"
            )
            if yaml_path.exists():
                data = self._read_yaml(yaml_path)
                tools_list: list = data.get("tools", [])
                if tool_name not in tools_list:
                    tools_list.append(tool_name)
                    data["tools"] = tools_list
                    self._write_yaml(yaml_path, data)
            self.restart_agent(agent)
        except DaemonError as exc:
            tools_path.write_text(original_tools)
            if original_yaml is not None:
                yaml_path.write_text(original_yaml)
            raise RestartFailed(
                f"tool written but restart failed (rolled back): {exc}"
            ) from exc
        except Exception as exc:
            tools_path.write_text(original_tools)
            if original_yaml is not None:
                yaml_path.write_text(original_yaml)
            raise DaemonError(str(exc)) from exc

    def edit_tool(self, agent: str, tool_name: str, old_str: str, new_str: str) -> None:
        d = self._require_agent(agent)
        tools_path = d / "tools.py"
        if not tools_path.exists():
            raise WorkspaceFileNotFound(f"tools.py not found for agent '{agent}'")
        source = tools_path.read_text()
        span = find_function_span(source, tool_name)
        if span is None:
            raise ToolNotFound(f"tool '{tool_name}' not found")

        lines = source.splitlines(keepends=True)
        before = "".join(lines[: span[0]])
        target = "".join(lines[span[0] : span[1]])
        after = "".join(lines[span[1] :])

        count = target.count(old_str)
        if count == 0:
            raise EditConflict(
                f"old_str not found within tool '{tool_name}'", occurrences=0
            )
        if count > 1:
            raise EditConflict(
                f"old_str appears {count} times within tool '{tool_name}' — must be unique",
                occurrences=count,
            )
        tools_path.write_text(before + target.replace(old_str, new_str, 1) + after)
        self.restart_agent(agent)

    def delete_tool(self, agent: str, tool_name: str) -> None:
        d = self._require_agent(agent)
        tools_path = d / "tools.py"
        yaml_path = d / "agent.yaml"
        if not tools_path.exists():
            raise WorkspaceFileNotFound(f"tools.py not found for agent '{agent}'")
        source = tools_path.read_text()
        span = find_function_span(source, tool_name)
        if span is None:
            raise ToolNotFound(f"tool '{tool_name}' not found")

        lines = source.splitlines(keepends=True)
        tools_path.write_text("".join(lines[: span[0]] + lines[span[1] :]))

        if yaml_path.exists():
            data = self._read_yaml(yaml_path)
            tools_list: list = data.get("tools", [])
            if tool_name in tools_list:
                tools_list.remove(tool_name)
                data["tools"] = tools_list
                self._write_yaml(yaml_path, data)
        self.restart_agent(agent)

    # -- workspace files ----------------------------------------------------

    def read_file(self, agent: str, path: str) -> str:
        self._require_agent(agent)
        full = self._safe_path(agent, path)
        try:
            return full.read_text()
        except FileNotFoundError:
            raise WorkspaceFileNotFound(f"file not found: {path}") from None

    def write_file(self, agent: str, path: str, content: str) -> None:
        self._require_agent(agent)
        full = self._safe_path(agent, path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    def edit_file(self, agent: str, path: str, old_str: str, new_str: str) -> None:
        self._require_agent(agent)
        full = self._safe_path(agent, path)
        try:
            content = full.read_text()
        except FileNotFoundError:
            raise WorkspaceFileNotFound(f"file not found: {path}") from None
        count = content.count(old_str)
        if count == 0:
            raise EditConflict("old_str not found", occurrences=0)
        if count > 1:
            raise EditConflict(
                f"old_str appears {count} times — must be unique", occurrences=count
            )
        full.write_text(content.replace(old_str, new_str, 1))

    # -- export / import ----------------------------------------------------

    def export_agent(self, agent: str, *, now: datetime | None = None) -> dict:
        d = self._require_agent(agent)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
        archive_path = self.exports_dir / f"{agent}-{timestamp}.tar.gz"

        included: list[str] = []
        skipped: list[dict] = []
        members: list[tuple[Path, str]] = []
        for root, dirs, files in os.walk(d):
            dirs[:] = [sub for sub in dirs if sub not in EXPORT_EXCLUDE_DIRS]
            for fname in files:
                fp = Path(root) / fname
                rel = fp.relative_to(d)
                if fname in EXPORT_EXCLUDE_FILES:
                    skipped.append({"path": str(rel), "reason": "excluded (run history)"})
                    continue
                if fp.is_symlink():
                    skipped.append({"path": str(rel), "reason": "symlink not exported"})
                    continue
                size = fp.stat().st_size
                if size > MAX_EXPORT_FILE_BYTES:
                    skipped.append(
                        {"path": str(rel), "reason": f"exceeds 10 MB cap ({size} bytes)"}
                    )
                    continue
                members.append((fp, f"{agent}/{rel.as_posix()}"))

        with tarfile.open(archive_path, "w:gz") as tar:
            for fp, arcname in members:
                tar.add(fp, arcname=arcname, recursive=False)
                included.append(arcname[len(agent) + 1 :])

        size_bytes = archive_path.stat().st_size
        if size_bytes > MAX_ARCHIVE_BYTES:
            archive_path.unlink(missing_ok=True)
            raise ArchiveTooLarge(
                f"export archive would be {size_bytes} bytes, over the 50 MB cap"
            )

        return {
            "agent": agent,
            "archive_path": str(archive_path),
            "included": sorted(included),
            "skipped": skipped,
            "size_bytes": size_bytes,
        }

    def import_agent(self, name: str, archive_path: str, *, start: bool = True) -> None:
        self.check_name(name)
        d = self._agent_dir(name)
        if d.exists():
            raise AgentExists(f"agent '{name}' already exists")

        max_agents = int(self._environ.get("MIRAGEND_MAX_AGENTS", DEFAULT_MAX_AGENTS))

        # Optimistic, lock-free cap check, as early as possible — before the
        # uploaded archive is even opened. Extraction below is real work
        # (opening a gzip tar, walking and writing out every member up to
        # MAX_ARCHIVE_BYTES) that's entirely wasted when the daemon is
        # already at/over MIRAGEND_MAX_AGENTS, since the authoritative check
        # further down is guaranteed to reject it anyway. This check reads
        # _agent_count() with no lock, so it's inherently racy (a concurrent
        # import/create can land between this read and the real check) —
        # it exists purely to short-circuit the common case, not to replace
        # the race-safe check-then-act guard inside _agent_creation_lock
        # right before the commit (shutil.move) below.
        current = self._agent_count()
        if current >= max_agents:
            raise AgentLimitExceeded(
                f"agent limit reached ({current}/{max_agents}) — delete an "
                "existing agent or raise MIRAGEND_MAX_AGENTS before "
                "importing another",
                limit=max_agents,
                current=current,
            )

        full = self._safe_export_path(archive_path)
        if not full.exists():
            raise ArchiveNotFound(f"archive not found: {archive_path}")

        staging = None
        created_dir = False
        added_service = False
        started = False
        try:
            self.exports_dir.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(dir=self.exports_dir, prefix=f".import-{name}-")
            )
            try:
                # filter="data" (Python 3.12+) is the stdlib mitigation for
                # exactly this rule's vulnerability class: it rejects absolute
                # paths, ".." traversal, links, and device files. The archive
                # path itself is already confined to exports/ by
                # _safe_export_path above.
                with tarfile.open(full, "r:gz") as tar:  # nosemgrep: trailofbits.python.tarfile-extractall-traversal.tarfile-extractall-traversal
                    tar.extractall(path=staging, filter="data")
            except (tarfile.TarError, ValueError, OSError) as exc:
                raise ArchiveInvalid(
                    f"could not safely extract '{archive_path}': {exc}"
                ) from exc

            entries = list(staging.iterdir())
            top_dirs = [p for p in entries if p.is_dir()]
            src_root = top_dirs[0] if len(entries) == 1 and len(top_dirs) == 1 else staging

            yaml_src = src_root / "agent.yaml"
            if not yaml_src.exists():
                raise ArchiveInvalid(
                    "archive has no agent.yaml at its root — it does not look like "
                    "a miragen export tarball"
                )

            original_text = yaml_src.read_text()
            rewritten, n = re.subn(
                r"(?m)^name:.*$", f"name: {name}", original_text, count=1
            )
            if n == 0:
                rewritten = f"name: {name}\n" + original_text
            yaml_src.write_text(rewritten)

            summary = validate_profile_text(rewritten)

            # An import is a create by another door: the archive's profile
            # meets the same contract gate, or it lands as the crash loop
            # #75 exists to prevent (Codex review, PR #76).
            spawn_ref = self._validated_spawn_ref(summary["profile_contract"])

            d.parent.mkdir(parents=True, exist_ok=True)
            # Everything above operates on `staging`, not agents_dir, so it
            # doesn't affect _agent_count() and doesn't need the lock. The
            # move is the commit step — pair it tightly with the cap check
            # so two concurrent imports can't both pass the check before
            # either agent directory actually exists (see #61 review).
            with self._agent_creation_lock:
                current = self._agent_count()
                if current >= max_agents:
                    raise AgentLimitExceeded(
                        f"agent limit reached ({current}/{max_agents}) — delete an "
                        "existing agent or raise MIRAGEND_MAX_AGENTS before "
                        "importing another",
                        limit=max_agents,
                        current=current,
                    )
                shutil.move(str(src_root), str(d))
                created_dir = True

            self._compose_add_service(name, spawn_ref)
            added_service = True

            if start:
                self._compose_up(name)
                started = True
                self._watch_first_boot(name)
        except DaemonError:
            if started:
                self._teardown_container(name)
            if added_service:
                self._compose_remove_service(name)
            if created_dir:
                shutil.rmtree(d, ignore_errors=True)
            raise
        except Exception as exc:
            if started:
                self._teardown_container(name)
            if added_service:
                self._compose_remove_service(name)
            if created_dir:
                shutil.rmtree(d, ignore_errors=True)
            raise DaemonError(str(exc)) from exc
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
