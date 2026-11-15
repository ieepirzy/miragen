"""Catalog skills pinned into a profile (`spec.skills`, profile contract v7, #233).

MiraRun pins each skill a profile names when it stores the profile for a
workload (mirarun ADR-028 addendum): source, name, revision, sha256,
description and the SKILL.md content itself. The profile reaches the agent
as it always has, so a run fetches nothing and its pinned profile revision is
the record of exactly which skill text it had.

How a run sees them:

- **claude-code** (both postures): the skills are written read-only as one
  local plugin, ``mirarun-skills``, and named in ``skills=[...]``. The locked
  posture keeps ``setting_sources=[]``, the gateway-only MCP and no other
  built-in tool: only Claude Code's ``Skill`` tool is added, and the harness
  admits it for the named skills only. Skills are instructions, not tools.
- **pydantic-ai**: each skill's name and description go into the system
  prompt, and a ``read_skill(name)`` tool returns the full SKILL.md on demand
  (Claude Code's progressive disclosure).

Other harnesses (grok-build, codex) refuse a profile that names skills at
load rather than silently running without them.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from miragen.models import PinnedSkill

PLUGIN_NAME = "mirarun-skills"
SKILL_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
# Claude Code runs !`command` when a skill loads (dynamic context injection).
# Both forms Claude Code runs when a skill loads: !`command` inline and a
# ```! fenced block (the CLI matches /```!\s*\n?[\s\S]*?\n?```/).
DYNAMIC_CONTEXT = ("!`", "```!")
MAX_SKILL_CHARS = 256 * 1024
# The only frontmatter keys a pinned skill may carry: everything else
# (allowed-tools, hooks, context, agent, model, ...) changes what a run may do.
FRONTMATTER_KEYS = frozenset({"name", "description"})
_MANIFEST = json.dumps({"name": PLUGIN_NAME, "version": "1.0.0",
                        "description": "Skills pinned by MiraRun for this agent"})


def _frontmatter(content: str) -> dict | None:
    import yaml

    if not content.startswith("---\n"):
        return None
    end = content.find("\n---\n", 4)
    if end == -1 and content.endswith("\n---"):
        end = len(content) - 4
    if end == -1:
        return None
    try:
        parsed = yaml.safe_load(content[4:end])
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def skill_text_problem(name: str, description: str, content: str) -> str | None:
    """Why this text may not run as a pinned skill, or None: no shell syntax,
    and a frontmatter of exactly `name` (this skill's) and `description`
    (the pinned one). Mirrors mirarun's pinner, so a profile edited after
    pinning is refused here too."""
    if any(marker in content for marker in DYNAMIC_CONTEXT):
        return (f"'{name}' uses dynamic context (!`...` or ```!), "
                "which runs a command when the skill loads")
    front = _frontmatter(content)
    if front is None:
        return f"'{name}' has no SKILL.md frontmatter (---, name, description, ---)"
    extra = sorted(set(front) - FRONTMATTER_KEYS)
    if extra:
        return f"'{name}' frontmatter may hold only name and description, not {', '.join(extra)}"
    if front.get("name") != name:
        return f"'{name}' frontmatter names '{front.get('name')}'"
    if not isinstance(front.get("description"), str) \
            or " ".join(front["description"].split()) != " ".join(description.split()):
        return f"'{name}' frontmatter description differs from the pinned description"
    return None


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def qualified(name: str) -> str:
    """The name Claude Code knows a plugin skill by."""
    return f"{PLUGIN_NAME}:{name}"


def bundle_digest(skills: Sequence["PinnedSkill"]) -> str:
    joined = "\n".join(f"{s.name}={s.sha256}" for s in skills)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _expected_files(skills: Sequence["PinnedSkill"]) -> dict[str, bytes]:
    files = {".claude-plugin/plugin.json": _MANIFEST.encode(), ".complete": b""}
    for skill in skills:
        files[f"skills/{skill.name}/SKILL.md"] = skill.content.encode("utf-8")
    return files


def _intact(directory: Path, expected: dict[str, bytes]) -> bool:
    """Exactly the expected files with exactly the pinned bytes: no extra
    file, no symlink. Checked on every reuse, so a copy someone edited (an
    engineer-posture session can chmod and write) is never served."""
    if directory.is_symlink() or not directory.is_dir():
        return False
    found: dict[str, bytes] = {}
    try:
        for current, dirs, files in os.walk(directory):
            for name in dirs + files:
                if (Path(current) / name).is_symlink():
                    return False
            for name in files:
                path = Path(current) / name
                found[path.relative_to(directory).as_posix()] = path.read_bytes()
    except OSError:
        return False
    return found == expected


def write_plugin(skills: Sequence["PinnedSkill"], root: Path) -> Path:
    """The skills as one read-only local plugin under ``root``. The canonical
    directory is named by the skills' digests and reused only while intact;
    otherwise a fresh copy is written and returned. The returned directory
    always exists and always holds exactly the pinned text."""
    expected = _expected_files(skills)
    directory = root / f"{PLUGIN_NAME}-{bundle_digest(skills)[:16]}"
    if _intact(directory, expected):
        return directory
    # A fresh name every time: a staging directory never needs removing.
    staging = root / f".{directory.name}.{uuid.uuid4().hex}"
    for relative, data in expected.items():
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _read_only(staging)
    if directory.exists() or directory.is_symlink():
        # A tampered or partial copy holds the canonical name: never use it.
        return staging
    try:
        staging.rename(directory)
    except OSError:
        # Another process won the rename: use its copy only if it is intact.
        return directory if _intact(directory, expected) else staging
    return directory


def _read_only(path: Path) -> None:
    for current, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.chmod(Path(current) / name, 0o444)
        for name in dirs:
            os.chmod(Path(current) / name, 0o555)
    os.chmod(path, 0o555)


def skills_instructions(skills: Sequence["PinnedSkill"]) -> str:
    """The system-prompt section a pydantic-ai run gets: what each skill is
    for, and how to read it."""
    lines = [
        "## Skills",
        "",
        "These skills hold the rules and procedures for your work. Before you do "
        "work a skill covers, read it with `read_skill(name)` and follow it.",
        "",
    ]
    lines += [f"- `{s.name}`: {' '.join(s.description.split())}" for s in skills]
    return "\n".join(lines)


def read_skill_tool(skills: Sequence["PinnedSkill"]) -> Callable[[str], str]:
    by_name = {s.name: s.content for s in skills}

    def read_skill(name: str) -> str:
        """Read one of your skills in full (its SKILL.md). `name` is the skill's
        name exactly as listed under "Skills" in your instructions."""
        content = by_name.get(name.strip())
        if content is None:
            return f"No skill named '{name}'. Your skills: {', '.join(sorted(by_name))}."
        return content

    return read_skill
