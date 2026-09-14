"""Project identity and scope resolution.

A working directory resolves to a repository (git toplevel + remote) or,
failing that, to itself. The identity is what the scope policy maps to a
Loimi scope; the mapping is the daemon's, never the session's.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from miragen.daemon.sessions.config import ProjectBinding, ScopePolicy
from miragen.daemon.sessions.models import ProjectIdentity

_GIT_TIMEOUT_S = 3.0
_SLUG_MAX = 90


def normalize_remote(url: str) -> str:
    """'git@github.com:org/repo.git' / 'https://user@github.com/org/repo/'
    → 'github.com/org/repo'."""
    value = url.strip()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)          # scheme
    value = re.sub(r"^[^@/]+@", "", value)                        # user@
    value = re.sub(r"^([^/:]+):\d+/", r"\1/", value, count=1)     # host:port/
    value = value.replace(":", "/", 1) if re.match(r"^[^/]+:", value) else value  # scp-style
    value = re.sub(r"/+$", "", value)
    value = re.sub(r"\.git$", "", value)
    return value.lower()


def project_slug(project_id: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", project_id.lower()).strip("-.")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:_SLUG_MAX].rstrip("-.") or "unknown"


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def resolve_project(cwd: str | None) -> ProjectIdentity:
    """cwd → identity. Remote wins (stable across clones and worktrees);
    otherwise the git toplevel; otherwise the directory itself."""
    directory = Path(cwd or ".").expanduser()
    try:
        directory = directory.resolve()
    except OSError:
        pass
    root = _git(["rev-parse", "--show-toplevel"], directory) if directory.is_dir() else None
    remote = _git(["remote", "get-url", "origin"], directory) if root else None
    if remote:
        project_id = normalize_remote(remote)
    elif root:
        project_id = f"path:{root}"
    else:
        project_id = f"path:{directory}"
    name = project_id.rstrip("/").rsplit("/", 1)[-1] or project_id
    return ProjectIdentity(
        id=project_id, slug=project_slug(project_id), name=name,
        root=root or str(directory), remote=remote,
    )


class ScopeAssignment:
    """The scopes one project's sessions get: read set, write scope, and
    whether the write scope is the templated project scope (which may
    still need provisioning) or an explicit/fallback one."""

    def __init__(self, *, read: list[str], write: str, templated: bool) -> None:
        self.read = read
        self.write = write
        self.templated = templated

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ScopeAssignment(read={self.read}, write={self.write!r}, templated={self.templated})"


def _binding_matches(binding: ProjectBinding, project: ProjectIdentity) -> bool:
    match = binding.match.rstrip("/")
    if match.startswith("/"):
        root = project.root.rstrip("/")
        return root == match or root.startswith(match + "/")
    return project.id == match.lower() or project.id == normalize_remote(match)


def assign_scopes(
    policy: ScopePolicy, bindings: list[ProjectBinding], project: ProjectIdentity,
) -> ScopeAssignment:
    for binding in bindings:
        if _binding_matches(binding, project):
            read = _dedupe([*policy.shared_read, *binding.read, binding.scope])
            return ScopeAssignment(read=read, write=binding.scope, templated=False)
    write = policy.project_scope.format(slug=project.slug)
    read = _dedupe([*policy.shared_read, write])
    return ScopeAssignment(read=read, write=write, templated=True)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
