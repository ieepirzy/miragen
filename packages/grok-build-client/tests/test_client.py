"""Unit tests for grok-build-client (no live grok required)."""

from __future__ import annotations

from grok_build_client import build_headless_argv, normalize_acp_update, normalize_headless_event


def test_normalize_headless_text_end_error():
    assert normalize_headless_event({"type": "text", "data": "hi"})[0]["type"] == "text"
    end = normalize_headless_event({
        "type": "end",
        "sessionId": "s",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })[0]
    assert end["type"] == "end" and end["sessionId"] == "s"
    assert normalize_headless_event({"type": "error", "message": "x"})[0]["message"] == "x"


def test_normalize_acp_message_chunk():
    events = normalize_acp_update({
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "hello"},
        }
    })
    assert events == [{"type": "text", "data": "hello", "raw": events[0]["raw"]}]


def test_build_headless_argv_resume_and_flags():
    argv = build_headless_argv(
        "grok", "do it", cwd="/ws", session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        resume=False, always_approve=True, web_search=False,
    )
    assert "-s" in argv and "--always-approve" in argv and "--disable-web-search" in argv
    argv_r = build_headless_argv(
        "grok", "more", cwd="/ws", session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        resume=True, always_approve=False, web_search=True,
    )
    assert "-r" in argv_r and "--always-approve" not in argv_r


def test_build_headless_argv_tool_surface_flags():
    argv = build_headless_argv(
        "grok", "go", cwd="/ws", always_approve=True,
        tools=["search_tool", "use_tool"],
        disallowed_tools=["Agent"],
        permission_mode="dontAsk",
        allow=["MCPTool(probe__get_magic_word)", "MCPTool(probe__ping)"],
        deny=["Bash", "Read"],
        max_turns=6,
    )
    # permission_mode replaces the always-approve shorthand, never both.
    assert "--always-approve" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--tools") + 1] == "search_tool,use_tool"
    assert argv[argv.index("--disallowed-tools") + 1] == "Agent"
    allows = [argv[i + 1] for i, a in enumerate(argv) if a == "--allow"]
    denies = [argv[i + 1] for i, a in enumerate(argv) if a == "--deny"]
    assert allows == ["MCPTool(probe__get_magic_word)", "MCPTool(probe__ping)"]
    assert denies == ["Bash", "Read"]
    assert argv[argv.index("--max-turns") + 1] == "6"


def test_build_headless_argv_defaults_unchanged():
    argv = build_headless_argv("grok", "go", cwd="/ws")
    for flag in ("--tools", "--disallowed-tools", "--permission-mode", "--allow",
                 "--deny", "--max-turns"):
        assert flag not in argv
    assert "--always-approve" in argv


def test_build_headless_argv_refuses_ambiguous_tool_lists():
    import pytest

    with pytest.raises(ValueError):
        build_headless_argv("grok", "go", cwd="/ws", tools=[])
    with pytest.raises(ValueError):
        build_headless_argv("grok", "go", cwd="/ws", tools=["read_file,run_terminal_cmd"])
    with pytest.raises(TypeError):
        build_headless_argv("grok", "go", cwd="/ws", tools="search_tool")
    with pytest.raises(ValueError):
        build_headless_argv("grok", "go", cwd="/ws", max_turns=0)


async def test_headless_session_passes_flags_and_env_to_the_process(tmp_path):
    """The real spawn path: a fake `grok` records argv + env, emits an end."""
    import json
    import os
    import stat

    from grok_build_client import HeadlessSession

    record = tmp_path / "record.json"
    fake = tmp_path / "grok"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ)}}, open({str(record)!r}, 'w'))\n"
        "print(json.dumps({'type': 'end', 'sessionId': 's', 'usage': {}}))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    session = HeadlessSession(
        prompt="go", cwd=str(tmp_path), grok_bin=str(fake), grok_home="/gh",
        env={"PATH": os.environ.get("PATH", ""), "ONLY": "1"},
        tools=["search_tool", "use_tool"], permission_mode="dontAsk",
        deny=["Bash"], max_turns=3,
    )
    events = [e async for e in session.run()]
    assert events[-1]["type"] == "end"
    seen = json.loads(record.read_text())
    assert seen["env"]["ONLY"] == "1" and seen["env"]["GROK_HOME"] == "/gh"
    assert seen["argv"][seen["argv"].index("--tools") + 1] == "search_tool,use_tool"
    assert "--always-approve" not in seen["argv"]
    assert seen["argv"][seen["argv"].index("--max-turns") + 1] == "3"
