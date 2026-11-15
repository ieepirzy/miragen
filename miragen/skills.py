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
DYNAMIC_CONTEXT = "!`"
MAX_SKILL_CHARS = 256 * 1024


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def qualified(name: str) -> str:
    """The name Claude Code knows a plugin skill by."""
    return f"{PLUGIN_NAME}:{name}"


def bundle_digest(skills: Sequence["PinnedSkill"]) -> str:
    joined = "\n".join(f"{s.name}={s.sha256}" for s in skills)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def write_plugin(skills: Sequence["PinnedSkill"], root: Path) -> Path:
    """Write the skills as one read-only local plugin under ``root`` and
    return its directory. The directory is named by the skills' digests, so
    the same set reuses it and a different set never overwrites it."""
    directory = root / f"{PLUGIN_NAME}-{bundle_digest(skills)[:16]}"
    if (directory / ".complete").is_file():
        return directory
    # A fresh name every time: a staging directory never needs removing.
    staging = root / f".{directory.name}.{uuid.uuid4().hex}"
    (staging / ".claude-plugin").mkdir(parents=True)
    (staging / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": PLUGIN_NAME, "version": "1.0.0",
                    "description": "Skills pinned by MiraRun for this agent"}),
        encoding="utf-8")
    for skill in skills:
        skill_dir = staging / "skills" / skill.name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(skill.content, encoding="utf-8")
    (staging / ".complete").write_text("", encoding="utf-8")
    _read_only(staging)
    try:
        staging.rename(directory)
    except OSError:
        # Another process won the rename: its copy has the same content, and
        # this read-only staging copy (a few KiB) is left as it is.
        pass
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
