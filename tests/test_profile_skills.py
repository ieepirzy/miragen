"""Catalog skills pinned into a profile (`spec.skills`, contract v7, #233):
the profile model, the loader, the claude-code locked posture and the
pydantic-ai tool. Fixture skills have the Some plugin's layout."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest
import yaml

from miragen.load import load_profile
from miragen.models import AgentProfile
from miragen.profile_contract import SUPPORTED_PROFILE_CONTRACTS
from miragen.skills import (
    PLUGIN_NAME,
    qualified,
    read_skill_tool,
    skills_instructions,
    write_plugin,
)

DRAFTING = (
    "---\nname: some-drafting\ndescription: Draft one post for a channel.\n---\n\n"
    "# Drafting\n\nCall variant_claim first. Prices only from ${COMPANY_FACT}.\n"
)
VOICE = "---\nname: brand-voice-fi\ndescription: The brand voice.\n---\n\nWrite plainly.\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pinned(name: str, content: str, **changes) -> dict:
    entry = {
        "source": "some",
        "name": name,
        "revision": "abc123",
        "sha256": _sha(content),
        "description": f"{name} description",
        "content": content,
    }
    entry.update(changes)
    return entry


SKILLS = [pinned("some-drafting", DRAFTING), pinned("brand-voice-fi", VOICE)]


def profile_doc(model: str = "claude-code:sonnet", skills=None) -> dict:
    return {
        "name": "some-drafter",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "inject_timestamp": False,
        "spec": {
            "model": model,
            "instructions": "Draft one post.",
            "skills": SKILLS if skills is None else skills,
        },
    }


def test_skills_imply_contract_v7_and_the_runtime_supports_it():
    assert 7 in SUPPORTED_PROFILE_CONTRACTS
    prof = AgentProfile.model_validate(profile_doc())
    assert prof.inferred_contract() == 7
    assert [s.name for s in prof.spec.skills] == ["some-drafting", "brand-voice-fi"]
    # Declaring an older apiVersion understates the requirement.
    with pytest.raises(ValueError, match="v7"):
        AgentProfile.model_validate({**profile_doc(), "apiVersion": "miragen/v6"})
    # No skills: unchanged, and a dump leaves the key out.
    doc = profile_doc()
    del doc["spec"]["skills"]
    plain = AgentProfile.model_validate(doc)
    assert plain.inferred_contract() == 1
    assert "skills" not in plain.model_dump(by_alias=True, exclude_none=False)["spec"]


@pytest.mark.parametrize(
    ("skills", "message"),
    [
        ([pinned("some-drafting", DRAFTING, sha256="0" * 64)], "does not match its sha256"),
        ([pinned("some-drafting", DRAFTING + "!`id`\n")], "dynamic context"),
        ([pinned("some-drafting", DRAFTING), pinned("some-drafting", DRAFTING)], "twice"),
        ([pinned("Bad_Name", DRAFTING)], "name"),
        ([{**pinned("some-drafting", DRAFTING), "url": "https://x.test"}], "url"),
    ],
)
def test_the_profile_refuses_tampered_or_unsafe_skills(skills, message):
    with pytest.raises(ValueError, match=message):
        AgentProfile.model_validate(profile_doc(skills=skills))


def test_the_loader_never_interpolates_skill_text(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(profile_doc()))
    prof = load_profile(path, environ={"COMPANY_FACT": "SECRET-VALUE"})
    assert prof.spec.skills[0].content == DRAFTING
    assert "SECRET-VALUE" not in prof.spec.skills[0].content


def test_a_harness_without_skill_delivery_refuses_them(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(profile_doc(model="grok-build:grok-4.6")))
    with pytest.raises(ValueError, match="would run without them"):
        load_profile(path, environ={})


def test_the_plugin_is_read_only_and_reused(tmp_path):
    prof = AgentProfile.model_validate(profile_doc())
    first = write_plugin(prof.spec.skills, tmp_path)
    assert write_plugin(prof.spec.skills, tmp_path) == first
    manifest = (first / ".claude-plugin" / "plugin.json").read_text()
    assert f'"name": "{PLUGIN_NAME}"' in manifest
    skill_md = first / "skills" / "some-drafting" / "SKILL.md"
    assert skill_md.read_text() == DRAFTING
    assert not os.stat(skill_md).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert not os.stat(first).st_mode & stat.S_IWUSR
    # A different set never overwrites this one.
    other = write_plugin(prof.spec.skills[:1], tmp_path)
    assert other != first and (first / "skills" / "brand-voice-fi").is_dir()


def test_pydantic_ai_gets_descriptions_and_reads_bodies_on_demand():
    prof = AgentProfile.model_validate(profile_doc(model="test"))
    section = skills_instructions(prof.spec.skills)
    assert "`some-drafting`: some-drafting description" in section
    assert "read_skill" in section and "Write plainly" not in section
    read_skill = read_skill_tool(prof.spec.skills)
    assert read_skill("brand-voice-fi") == VOICE
    assert "No skill named 'nope'" in read_skill("nope")


def test_build_agent_adds_the_section_and_the_tool():
    from unittest.mock import patch

    from miragen.factory import build_agent

    prof = AgentProfile.model_validate(profile_doc(model="anthropic:claude-haiku-4-5"))
    with patch("miragen.factory.Agent") as MockAgent, \
         patch("miragen.factory.resolve_capabilities", return_value=[]):
        build_agent(prof)
    instructions = MockAgent.call_args.kwargs["instructions"]
    assert instructions.startswith("Draft one post.\n\n## Skills")
    (tool,), _ = MockAgent.return_value.tool_plain.call_args
    assert tool.__name__ == "read_skill" and tool("brand-voice-fi") == VOICE


sdk = pytest.importorskip("claude_agent_sdk")


async def test_locked_claude_code_loads_only_the_named_skills(tmp_path):
    from miragen.harness.claude_code import SKILL_TOOL
    from tests.test_claude_code_harness import Env, FakeClient, turn

    env = Env(tmp_path)
    try:
        h = env.harness(AgentProfile.model_validate(profile_doc()))
        await h.run(turn("hi"))
        o = FakeClient.instances[0].options
        # Still locked: no settings, gateway-only MCP, default permissions;
        # the only built-in is the Skill tool.
        assert o.setting_sources == [] and o.strict_mcp_config
        assert list(o.mcp_servers) == ["gw"] and o.permission_mode == "default"
        assert o.tools == [SKILL_TOOL]
        assert o.skills == [qualified("some-drafting"), qualified("brand-voice-fi")]
        (plugin,) = o.plugins
        assert plugin["type"] == "local"
        assert Path(plugin["path"]).parent == tmp_path / "claude-home" / "skill-plugins"
        assert (Path(plugin["path"]) / "skills" / "some-drafting" / "SKILL.md").read_text() == DRAFTING
        allow = await h._permission(SKILL_TOOL, {"skill": qualified("some-drafting")}, None)
        deny = await h._permission(SKILL_TOOL, {"skill": "some-other:skill"}, None)
        bash = await h._permission("Bash", {"command": "id"}, None)
        assert type(allow).__name__ == "PermissionResultAllow"
        assert type(deny).__name__ == "PermissionResultDeny"
        assert type(bash).__name__ == "PermissionResultDeny"
    finally:
        for harness in env.harnesses:
            await harness.aclose()


async def test_a_profile_without_skills_launches_exactly_as_before(tmp_path):
    from tests.test_claude_code_harness import Env, FakeClient, profile, turn

    env = Env(tmp_path)
    try:
        h = env.harness(profile())
        await h.run(turn("hi"))
        o = FakeClient.instances[0].options
        assert o.tools == [] and o.skills == [] and o.plugins == []
        assert not (tmp_path / "claude-home" / "skill-plugins").exists()
    finally:
        for harness in env.harnesses:
            await harness.aclose()
