"""voice.instructions_file: renderer guidance kept in its own file and
appended to the agent's system instructions (every base-tier harness)."""

from __future__ import annotations

import pytest

from miragen.factory import build_agent
from miragen.models import AgentProfile, VoiceSpec
from miragen.voice import load_speak_guidance, with_voice_guidance


def test_guidance_loaded_relative_to_profile(tmp_path):
    (tmp_path / "speak-instructions.md").write_text("Use [pause] and <whisper>…</whisper>.\n")
    spec = VoiceSpec(provider="http", url="http://tts/speak", instructions_file="speak-instructions.md")
    assert load_speak_guidance(spec, str(tmp_path / "agent.yaml")) == \
        "Use [pause] and <whisper>…</whisper>."


@pytest.mark.parametrize("content", [None, "   \n"])
def test_missing_or_empty_guidance_fails_boot(tmp_path, content):
    if content is not None:
        (tmp_path / "g.md").write_text(content)
    spec = VoiceSpec(provider="http", url="http://tts/speak", instructions_file="g.md")
    with pytest.raises(ValueError):
        load_speak_guidance(spec, str(tmp_path / "agent.yaml"))


def test_no_file_no_guidance():
    assert load_speak_guidance(VoiceSpec(provider="http", url="http://t"), None) is None
    assert with_voice_guidance("You are Mira.", None) == "You are Mira."


def test_guidance_is_appended_to_the_system_instructions():
    assert with_voice_guidance("You are Mira.\n", "Tags: [laugh]") == \
        "You are Mira.\n\n## Speaking aloud\n\nTags: [laugh]"
    profile = AgentProfile.model_validate({
        "name": "t", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "test", "instructions": "You are Mira."}})
    agent, _ = build_agent(profile, system_guidance="Tags: [laugh]", extra_instructions="MEM")
    text = "\n".join(str(i) for i in agent._instructions)
    assert text.index("You are Mira.") < text.index("## Speaking aloud") < text.index("MEM")
