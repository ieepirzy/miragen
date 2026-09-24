"""Locked-down headless Grok: tool-surface fields, hermetic GROK_HOME, and
the subscription-only env (grok 1.0.41 flags and config keys)."""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from miragen.executor.grok_hermetic import (
    OWNER_MARKER,
    PRE_HERMETIC_BACKUP,
    HermeticHomeError,
    hermetic_home_dir,
)
from tests.test_executor_adapters import _grok_executor, _profile

PROBE = {"name": "probe", "url": "http://127.0.0.1:8931/mcp", "bearer_token_env": "PROBE_TOKEN"}
LOCKED = {
    "executor": "grok-build",
    "grok_hermetic": True,
    "grok_auth": "subscription",
    "mcp_servers": [PROBE],
    "grok_tools": ["search_tool", "use_tool"],
    "grok_disallowed_tools": ["Agent"],
    "grok_permission_mode": "dontAsk",
    "grok_allow": ["MCPTool(probe__get_magic_word)"],
    "grok_deny": ["Bash", "Read", "Edit", "Write", "Grep", "WebFetch", "WebSearch"],
    "grok_max_turns": 8,
}


# ── Validation matrix ────────────────────────────────────────────────────────


def test_locked_down_profile_validates():
    spec = _profile(LOCKED).executor
    assert spec.grok_transport == "headless"
    assert spec.grok_tools == ["search_tool", "use_tool"]


def test_grok_auth_defaults_to_auto_on_grok_only():
    assert _profile({"executor": "grok-build"}).executor.grok_auth == "auto"
    assert _profile({"executor": "codex"}).executor.grok_auth is None


@pytest.mark.parametrize("field,value", [
    ("grok_auth", "subscription"),
    ("grok_hermetic", True),
    ("grok_tools", ["use_tool"]),
    ("grok_disallowed_tools", ["Agent"]),
    ("grok_permission_mode", "dontAsk"),
    ("grok_allow", ["MCPTool(x__y)"]),
    ("grok_deny", ["Bash"]),
    ("grok_max_turns", 3),
])
def test_grok_fields_rejected_on_other_executors(field, value):
    with pytest.raises(ValidationError, match=field):
        _profile({"executor": "codex", field: value})


@pytest.mark.parametrize("field,value", [
    ("grok_hermetic", True),
    ("grok_tools", ["use_tool"]),
    ("grok_disallowed_tools", ["Agent"]),
    ("grok_permission_mode", "dontAsk"),
    ("grok_allow", ["MCPTool(x__y)"]),
    ("grok_deny", ["Bash"]),
    ("grok_max_turns", 3),
])
def test_headless_only_fields_rejected_on_acp(field, value):
    with pytest.raises(ValidationError, match="requires grok_transport: headless"):
        _profile({"executor": "grok-build", "grok_transport": "acp", field: value})


def test_grok_auth_is_allowed_on_acp():
    spec = _profile({"executor": "grok-build", "grok_transport": "acp",
                     "grok_auth": "subscription"}).executor
    assert spec.grok_auth == "subscription"


def test_headless_mcp_servers_need_hermetic():
    with pytest.raises(ValidationError, match="grok_hermetic: true"):
        _profile({"executor": "grok-build", "mcp_servers": [PROBE]})
    spec = _profile({"executor": "grok-build", "grok_hermetic": True,
                     "mcp_servers": [PROBE]}).executor
    assert spec.mcp_servers[0].name == "probe"


def test_hermetic_rejects_unsafe_env_names_and_templated_urls():
    with pytest.raises(ValidationError, match="plain env var name"):
        _profile({"executor": "grok-build", "grok_hermetic": True, "mcp_servers": [
            {**PROBE, "bearer_token_env": 'X} " evil'}]})
    with pytest.raises(ValidationError, match="must be literal"):
        _profile({"executor": "grok-build", "grok_hermetic": True, "mcp_servers": [
            {**PROBE, "url": "http://${HOST}/mcp"}]})


def test_empty_tool_lists_and_bad_modes_rejected():
    with pytest.raises(ValidationError):
        _profile({"executor": "grok-build", "grok_tools": []})
    with pytest.raises(ValidationError):
        _profile({"executor": "grok-build", "grok_permission_mode": "yolo"})
    with pytest.raises(ValidationError):
        _profile({"executor": "grok-build", "grok_max_turns": 0})


def test_web_search_with_an_allowlist_that_omits_it_is_dead_config():
    with pytest.raises(ValidationError, match="web_search"):
        _profile({"executor": "grok-build", "web_search": True,
                  "grok_tools": ["search_tool", "use_tool"]})
    _profile({"executor": "grok-build", "web_search": True,
              "grok_tools": ["use_tool", "web_search"]})
    _profile({"executor": "grok-build", "web_search": False,
              "grok_tools": ["search_tool", "use_tool"]})


# ── Options → argv ───────────────────────────────────────────────────────────


async def test_options_carry_the_tool_surface(tmp_path):
    captured = []
    _, executor = _grok_executor(tmp_path, executor_body=LOCKED, captured=captured)
    executor.prepare()
    result = await executor.run_job("go", "lock-1", mcp_secret_env={"PROBE_TOKEN": "run-scoped"})
    assert result.status == "succeeded"
    opts = captured[0]["options"]
    assert opts["tools"] == ["search_tool", "use_tool"]
    assert opts["disallowed_tools"] == ["Agent"]
    assert opts["permission_mode"] == "dontAsk"
    assert opts["allow"] == ["MCPTool(probe__get_magic_word)"]
    assert "WebFetch" in opts["deny"]
    assert opts["max_turns"] == 8
    assert opts["env"]["PROBE_TOKEN"] == "run-scoped"


async def test_default_session_hands_everything_to_the_grok_process(tmp_path, monkeypatch):
    """End to end through the real HeadlessSession with a fake `grok` that
    records its argv and environment."""
    import json

    from miragen.executor.grok_build import GrokBuildExecutor

    record = tmp_path / "record.json"
    fake = tmp_path / "grok"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ)}}, open({str(record)!r}, 'w'))\n"
        "print(json.dumps({'type': 'end', 'sessionId': 's', 'usage': {}}))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("XAI_API_KEY", "xai-must-not-leak")
    monkeypatch.setenv("GROK_CODE_XAI_API_KEY", "xai-must-not-leak-2")
    monkeypatch.setenv("GROK_CLAUDE_MCPS_ENABLED", "true")
    monkeypatch.setenv("GROK_CONFIG", '{"x": 1}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")
    monkeypatch.setenv("PROBE_TOKEN", "static-fallback")

    profile = _profile(LOCKED)
    profile.executor.grok_home = str(tmp_path / "grok-home")
    profile.executor.workspace_root = str(tmp_path / "ws")
    executor = GrokBuildExecutor(profile, runs_root=tmp_path / "runs", grok_bin=str(fake))
    executor.prepare()
    result = await executor.run_job("go", "lock-2", mcp_secret_env={"PROBE_TOKEN": "run-scoped"})
    assert result.status == "succeeded", result.error

    seen = json.loads(record.read_text())
    argv, env = seen["argv"], seen["env"]
    assert argv[argv.index("--tools") + 1] == "search_tool,use_tool"
    assert argv[argv.index("--disallowed-tools") + 1] == "Agent"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--always-approve" not in argv
    assert "--disable-web-search" in argv
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--allow"] == [
        "MCPTool(probe__get_magic_word)"]
    assert argv[argv.index("--max-turns") + 1] == "8"

    assert "XAI_API_KEY" not in env and "GROK_CODE_XAI_API_KEY" not in env
    assert "GROK_CLAUDE_MCPS_ENABLED" not in env and "GROK_CONFIG" not in env
    assert "CLAUDE_CONFIG_DIR" not in env
    assert env["GROK_HOME"] == profile.executor.grok_home
    assert env["HOME"] == str(hermetic_home_dir(Path(profile.executor.grok_home)))
    assert env["PROBE_TOKEN"] == "run-scoped"  # per-run secret beats the static one
    # os.environ itself is untouched (shared by concurrent runs).
    assert os.environ["XAI_API_KEY"] == "xai-must-not-leak"


# ── Env policy ───────────────────────────────────────────────────────────────


def test_auto_auth_keeps_the_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    _, executor = _grok_executor(tmp_path)
    env = executor._grok_env(None, transport="headless")
    assert env["XAI_API_KEY"] == "k"
    assert env["HOME"] == os.environ["HOME"]


def test_subscription_strips_the_api_key_on_acp_too(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    monkeypatch.setenv("GROK_CODE_XAI_API_KEY", "k2")
    _, executor = _grok_executor(
        tmp_path, executor_body={"grok_transport": "acp", "grok_auth": "subscription"})
    env = executor._grok_env(None, transport="acp")
    assert "XAI_API_KEY" not in env and "GROK_CODE_XAI_API_KEY" not in env


def test_missing_mcp_secret_is_not_inherited_by_name(tmp_path, monkeypatch):
    monkeypatch.delenv("PROBE_TOKEN", raising=False)
    _, executor = _grok_executor(tmp_path, executor_body=LOCKED)
    env = executor._grok_env(None, transport="headless")
    assert "PROBE_TOKEN" not in env


async def test_acp_client_reads_the_child_env_for_api_key_auth(monkeypatch):
    """A stripped child env must not be switched to xai.api_key because the
    parent process still has XAI_API_KEY."""
    from grok_build_client import AcpSession

    monkeypatch.setenv("XAI_API_KEY", "parent-only")
    session = AcpSession(env={"PATH": "/usr/bin"})
    calls = []

    async def fake_request(method, params=None):
        calls.append((method, params))
        if method == "initialize":
            return {"authMethods": [{"id": "xai.api_key"}, {"id": "cached_token"}]}
        return {}

    session.request = fake_request  # type: ignore[method-assign]
    await session._handshake()
    assert ("authenticate", {"methodId": "cached_token", "_meta": {"headless": True}}) in calls


# ── Hermetic GROK_HOME ───────────────────────────────────────────────────────


def _hermetic(tmp_path, body=None):
    profile, executor = _grok_executor(tmp_path, executor_body=body or LOCKED)
    return profile, executor, Path(profile.executor.grok_home)


def test_hermetic_config_declares_only_the_spec_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_TOKEN", "sekrit-value")
    _, executor, home = _hermetic(tmp_path, {**LOCKED, "mcp_servers": [
        PROBE, {"name": "loimi", "url": "https://loimi.example/mcp"}]})
    executor.prepare()
    config_text = (home / "config.toml").read_text()
    config = tomllib.loads(config_text)
    assert set(config["mcp_servers"]) == {"probe", "loimi"}
    assert config["mcp_servers"]["probe"] == {
        "url": "http://127.0.0.1:8931/mcp",
        "headers": {"Authorization": "Bearer ${PROBE_TOKEN}"},
    }
    assert "headers" not in config["mcp_servers"]["loimi"]
    assert "sekrit-value" not in config_text
    for vendor, cells in {"claude": 6, "cursor": 6, "codex": 3}.items():
        assert len(config["compat"][vendor]) == cells
        assert not any(config["compat"][vendor].values())
    assert config["cli"] == {"auto_update": False, "use_leader": False}
    assert config["subagents"]["enabled"] is False
    assert config["memory"]["enabled"] is False and config["memory_v2"]["enabled"] is False
    assert config["managed_mcps"]["enabled"] is False
    assert config["features"]["managed_config"] is False
    assert config["telemetry"]["trace_upload"] is False
    assert config["disable_web_search"] is True

    req = tomllib.loads((home / "requirements.toml").read_text())
    assert req["allow_managed_mcp_servers_only"] is True
    assert req["enable_all_project_mcp_servers"] is False
    assert req["allow_managed_hooks_only"] is True
    assert [e["server_url"] for e in req["allowed_mcp_servers"]] == [
        "http://127.0.0.1:8931/mcp", "https://loimi.example/mcp"]
    assert req["subagents"]["enabled"] is False
    assert hermetic_home_dir(home).is_dir()
    assert not any(hermetic_home_dir(home).iterdir())


def test_hermetic_with_no_servers_blocks_every_server(tmp_path):
    _, executor, home = _hermetic(tmp_path, {"executor": "grok-build", "grok_hermetic": True})
    executor.prepare()
    config = tomllib.loads((home / "config.toml").read_text())
    req = tomllib.loads((home / "requirements.toml").read_text())
    assert "mcp_servers" not in config
    assert req["allow_managed_mcp_servers_only"] is True
    assert "allowed_mcp_servers" not in req  # empty allowlist + only-managed = none


def test_hermetic_preserves_auth_and_rewrites_every_start(tmp_path):
    _, executor, home = _hermetic(tmp_path)
    home.mkdir(parents=True)
    (home / "auth.json").write_text('{"opaque": true}')
    (home / "sessions").mkdir()
    (home / "sessions" / "keep").write_text("x")
    executor.prepare()
    # grok itself appends state to config.toml at runtime; next start resets it.
    with open(home / "config.toml", "a") as f:
        f.write('\n[mcp_servers.sneaky]\nurl = "http://evil/mcp"\n')
    executor.prepare()
    assert "sneaky" not in (home / "config.toml").read_text()
    assert (home / "auth.json").read_text() == '{"opaque": true}'
    assert (home / "sessions" / "keep").read_text() == "x"
    leftovers = [p.name for p in home.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_hermetic_write_is_atomic(tmp_path, monkeypatch):
    """A failed write leaves the previous file intact (tmp + os.replace)."""
    from miragen.executor import grok_hermetic

    _, executor, home = _hermetic(tmp_path)
    executor.prepare()
    before = (home / "config.toml").read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(grok_hermetic.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        executor.prepare()
    assert (home / "config.toml").read_text() == before
    assert not [p for p in home.iterdir() if p.name.endswith(".tmp")]


def test_hermetic_keeps_a_foreign_config_once_and_restores_it(tmp_path):
    profile, executor, home = _hermetic(tmp_path)
    home.mkdir(parents=True)
    (home / "config.toml").write_text("[ui]\ntheme = 'mine'\n")
    executor.prepare()
    assert (home / PRE_HERMETIC_BACKUP).read_text() == "[ui]\ntheme = 'mine'\n"
    assert (home / OWNER_MARKER).read_text().strip() == profile.name

    # Turning hermetic off restores it and drops the policy file.
    from miragen.executor.grok_build import GrokBuildExecutor

    plain = _profile({"executor": "grok-build"})
    plain.executor.grok_home = str(home)
    plain.executor.workspace_root = str(tmp_path / "ws2")
    GrokBuildExecutor(plain, runs_root=tmp_path / "runs2").prepare()
    assert (home / "config.toml").read_text() == "[ui]\ntheme = 'mine'\n"
    assert not (home / "requirements.toml").exists()
    assert not (home / OWNER_MARKER).exists()


def test_hermetic_refuses_another_agents_home(tmp_path):
    _, executor, home = _hermetic(tmp_path)
    home.mkdir(parents=True)
    (home / OWNER_MARKER).write_text("someone-else\n")
    with pytest.raises(HermeticHomeError, match="someone-else"):
        executor.prepare()


def test_hermetic_refuses_a_foreign_requirements_file(tmp_path):
    _, executor, home = _hermetic(tmp_path)
    home.mkdir(parents=True)
    (home / "requirements.toml").write_text("# org policy\n")
    with pytest.raises(HermeticHomeError, match="not written by miragen"):
        executor.prepare()
    assert (home / "requirements.toml").read_text() == "# org policy\n"


def test_non_hermetic_prepare_is_unchanged(tmp_path):
    _, executor, home = _hermetic(tmp_path, {"executor": "grok-build"})
    executor.prepare()
    assert (home / "config.toml").read_text().endswith("[cli]\nauto_update = false\n")
    assert not (home / "requirements.toml").exists()
    assert not hermetic_home_dir(home).exists()
