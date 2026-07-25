"""Backend-owned whole-run outcome classification from harvested diffs.

MiraGen classifies the final deliverable from path analysis of the harvested
patch. It never reads model free-text for this. MiraRun (and other control
planes) must project these fields as-is.

Semantics:

- ``None`` — outcome unknown (no harvest yet, failed/suspended turn, or a
  pre-feature record). Distinct from empty.
- ``[]`` — harvest completed and nothing was affected / no categories apply
  (empty patch, or classic workspace without named repositories for the
  repository list).
"""

from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Iterable, Literal

ChangeCategory = Literal["documentation", "code", "structural"]

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_PLUS_FILE_RE = re.compile(r"^\+\+\+ (?:b/(.+)|/dev/null)$")
_REPO_SECTION_RE = re.compile(
    r"^# === miragen repository: (?P<name>[^ ]+) \(mount: .+\) ===$"
)

_DOC_SUFFIXES = frozenset({
    ".md",
    ".mdx",
    ".rst",
    ".adoc",
    ".txt",
})
_DOC_BASENAMES = frozenset({
    "readme",
    "changelog",
    "changes",
    "license",
    "licence",
    "copying",
    "authors",
    "contributors",
    "contributing",
    "code_of_conduct",
    "security",
    "notice",
    "credits",
})
_DOC_DIRS = frozenset({"docs", "doc", "documentation", "manual", "man"})

_STRUCTURAL_BASENAMES = frozenset({
    "dockerfile",
    "containerfile",
    "makefile",
    "gnumakefile",
    "justfile",
    "procfile",
    "vagrantfile",
    "gemfile",
    "rakefile",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "constraints.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
    "composer.json",
    "composer.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "tsconfig.json",
    "jsconfig.json",
    "webpack.config.js",
    "vite.config.js",
    "vite.config.ts",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    ".editorconfig",
    ".pre-commit-config.yaml",
    "tox.ini",
    "noxfile.py",
    "alembic.ini",
    "manage.py",
})
_STRUCTURAL_SUFFIXES = frozenset({
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".tf",
    ".tfvars",
    ".hcl",
})
_STRUCTURAL_DIRS = frozenset({
    ".github",
    ".gitlab",
    ".circleci",
    ".azure-pipelines",
    ".buildkite",
    "ci",
    "deploy",
    "deployment",
    "deployments",
    "infra",
    "infrastructure",
    "terraform",
    "k8s",
    "kubernetes",
    "helm",
    "charts",
    "ops",
    "ansible",
    "pulumi",
    "nix",
    ".devcontainer",
})

_CATEGORY_ORDER: tuple[ChangeCategory, ...] = (
    "documentation",
    "code",
    "structural",
)


def extract_changed_paths(diff_text: str) -> list[str]:
    """Return unique changed file paths from a unified / multi-repo bundle."""

    paths: list[str] = []
    seen: set[str] = set()
    for line in diff_text.splitlines():
        path: str | None = None
        git_match = _DIFF_GIT_RE.match(line)
        if git_match is not None:
            path = git_match.group(2)
        else:
            plus_match = _PLUS_FILE_RE.match(line)
            if plus_match is not None and plus_match.group(1) is not None:
                path = plus_match.group(1)
        if path is None or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def classify_path(path: str) -> ChangeCategory:
    """Classify one path. Structural wins over documentation for CI/config."""

    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    if not normalized or normalized == "/dev/null":
        return "code"
    pure = PurePosixPath(normalized)
    parts = [part.lower() for part in pure.parts]
    basename = pure.name.lower()

    if any(part in _STRUCTURAL_DIRS for part in parts[:-1]):
        return "structural"
    if basename in _STRUCTURAL_BASENAMES:
        return "structural"
    if pure.suffix.lower() in _STRUCTURAL_SUFFIXES and any(
        part in _STRUCTURAL_DIRS or part in {".github", "deploy", "infra"}
        for part in parts
    ):
        return "structural"
    if basename in _STRUCTURAL_BASENAMES or pure.suffix.lower() in {
        ".tf",
        ".tfvars",
    }:
        return "structural"
    # Common top-level config files that are structural even outside CI dirs.
    if basename in {
        "pyproject.toml",
        "package.json",
        "cargo.toml",
        "go.mod",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    } or basename.startswith("dockerfile"):
        return "structural"
    if pure.suffix.lower() in {".yml", ".yaml", ".toml"} and len(parts) == 1:
        return "structural"

    if any(part in _DOC_DIRS for part in parts[:-1]):
        return "documentation"
    stem = pure.stem.lower()
    if stem in _DOC_BASENAMES or basename in _DOC_BASENAMES:
        return "documentation"
    if pure.suffix.lower() in _DOC_SUFFIXES and (
        stem in _DOC_BASENAMES or any(part in _DOC_DIRS for part in parts)
    ):
        return "documentation"
    if pure.suffix.lower() in _DOC_SUFFIXES and len(parts) == 1:
        return "documentation"

    return "code"


def classify_categories(paths: Iterable[str]) -> list[ChangeCategory]:
    """Return sorted unique categories for the given paths."""

    found: set[ChangeCategory] = set()
    for path in paths:
        found.add(classify_path(path))
    return [category for category in _CATEGORY_ORDER if category in found]


def affected_repositories_from_patches(
    patches_by_name: dict[str, str],
) -> list[str]:
    """Writable-repo names whose harvested patches are non-empty."""

    return sorted(
        name
        for name, patch in patches_by_name.items()
        if patch.strip()
    )


def classify_harvested_diff(
    diff_text: str,
    *,
    patches_by_name: dict[str, str] | None = None,
) -> tuple[list[str], list[ChangeCategory]]:
    """Classify a harvested deliverable.

    When ``patches_by_name`` is provided (multi-repo harvest), affected
    repositories are the non-empty named patches. Otherwise the repository
    list is empty (classic single-workspace layout has no named repositories)
    while categories still come from changed paths.
    """

    paths = extract_changed_paths(diff_text)
    categories = classify_categories(paths)
    if patches_by_name is not None:
        return affected_repositories_from_patches(patches_by_name), categories
    return [], categories
