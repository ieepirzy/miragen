"""voice.instructions_file: renderer guidance rides the speak tool, not the
system prompt."""

from __future__ import annotations

import pytest

from miragen.models import VoiceSpec
from miragen.voice import load_speak_guidance, with_speak_guidance
from miragen.voice_mcp import build_voice_mcp


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
    assert with_speak_guidance("Say it.", None) == "Say it."


async def test_voice_mcp_speak_description_carries_guidance():
    mcp = build_voice_mcp(lambda: (None, None), speak_guidance="Tags: [laugh]")
    tool = next(t for t in await mcp.list_tools() if t.name == "speak")
    assert "Tags: [laugh]" in tool.description and "speak" in tool.name
