"""A full Claude model id after `claude-code:` reaches the CLI unchanged, in
both postures: the example lane pinned to Claude Opus 4.8
(examples/profiles/opus-4-8-engineer.yaml) launches `claude --model
claude-opus-4-8`, never the bare `opus` alias (which resolves to the newest
Opus)."""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")

from miragen.harness.claude_code import ClaudeCodeHarness, ClaudeCodeSettings  # noqa: E402
from miragen.harness.gateway import ToolGateway  # noqa: E402
from miragen.load import load_profile  # noqa: E402
from tests import claude_code_snapshot as snap  # noqa: E402
from tests.test_claude_code_engineer import hermetic  # noqa: E402,F401

EXAMPLE = Path(__file__).parent.parent / "examples" / "profiles" / "opus-4-8-engineer.yaml"
PINNED = "claude-opus-4-8"


def _launch(prof, root: Path) -> dict:
    settings = ClaudeCodeSettings(claude_home=root / "home", workdirs=root / "work",
                                  gateway_url="http://127.0.0.1:9/mcp/gateway/",
                                  cli_path="/usr/bin/env")
    h = ClaudeCodeHarness(prof, ToolGateway(
        prof, native_capabilities=ClaudeCodeHarness.native_capabilities), settings)
    o = h._options("chat", root / "work" / "chat", resume=None, new_id="x", stderr=deque())
    return snap.render(h, o, root)


def _model_arg(argv: list[str]) -> str:
    i = argv.index("--model")
    return argv[i + 1]


def test_the_example_profile_pins_opus_4_8_in_engineer_posture(tmp_path, hermetic):  # noqa: F811
    prof = load_profile(EXAMPLE)
    assert prof.spec.model == f"claude-code:{PINNED}"
    view = _launch(prof, tmp_path)
    assert view["options"]["model"] == PINNED
    assert _model_arg(view["argv"]) == PINNED
    # Engineer posture: Claude Code's own tools, so the Agent tool is there
    # and subagents run in this same process under this --model.
    assert view["options"]["tools"] == {"type": "preset", "preset": "claude_code"}
    assert view["options"]["permission_mode"] == "bypassPermissions"
    # No fallback model: an overloaded Opus 4.8 fails the turn rather than
    # silently switching models.
    assert view["options"]["fallback_model"] is None
    assert "--fallback-model" not in view["argv"]


def test_a_full_model_id_passes_through_the_locked_posture_too(tmp_path, hermetic):  # noqa: F811
    prof = snap.profile({}, model=f"claude-code:{PINNED}")
    view = _launch(prof, tmp_path)
    assert view["options"]["model"] == PINNED
    assert _model_arg(view["argv"]) == PINNED
