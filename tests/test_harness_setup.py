"""Daemon-managed harness setup (miragen_hook/harness_setup.py): Grok Build
hook files and Codex native hooks + trust + MCP server, written into the
harness homes with no human step — and never anything we do not own."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from miragen_hook import harness_setup as hs
from miragen_hook.install import CODEX_EVENTS, GROK_BUILD_EVENTS

PLUGIN_COMMAND = 'PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m miragen_hook claude-code'


# ── Codex's trust hash ───────────────────────────────────────────────────────


class TestCodexHookHash:
    def test_matches_hashes_codex_itself_wrote(self):
        """Oracle: trusted_hash values codex 0.156.1 accepted for the plugin's
        entries (scratch config.toml of the 2026-09-23 Codex research run)."""
        entry = {"type": "command", "command": PLUGIN_COMMAND, "timeout": 15,
                 "statusMessage": "miragen memory"}
        assert hs.codex_hook_hash("SessionStart", entry) == \
            "sha256:4622aae5f2ebd428c9ba6376e64da9413ca1685440fe8a7107a4f37d7680cea4"
        assert hs.codex_hook_hash("SessionEnd", dict(entry, timeout=3)) == \
            "sha256:cc0bdcfa6b5d04a1b651a7677ca153d03f1aebf028dd50afead870a41dda42c7"
        assert hs.codex_hook_hash("Stop", dict(entry, timeout=5)) == \
            "sha256:8d35dcfded58bf24e5e62446c5fff3bda2d440571bff11a2fd9272c36b9e716f"

    def test_session_end_timeout_is_clamped_like_codex(self):
        entry = {"type": "command", "command": "x", "timeout": 3}
        assert hs.codex_hook_hash("SessionEnd", dict(entry, timeout=30)) == hs.codex_hook_hash("SessionEnd", entry)
        assert hs.codex_hook_hash("SessionEnd", {"type": "command", "command": "x"}) == \
            hs.codex_hook_hash("SessionEnd", dict(entry, timeout=1))
        assert hs.codex_hook_hash("Stop", {"type": "command", "command": "x"}) == \
            hs.codex_hook_hash("Stop", dict(entry, timeout=600))

    def test_context_limit_counts_only_where_codex_keeps_it(self):
        base = {"type": "command", "command": "x", "timeout": 15}
        assert hs.codex_hook_hash("SessionStart", dict(base, additionalContextLimit=6000)) != \
            hs.codex_hook_hash("SessionStart", base)
        # the default is normalized away, and events that cannot emit
        # context drop the field before hashing
        assert hs.codex_hook_hash("SessionStart", dict(base, additionalContextLimit=2500)) == \
            hs.codex_hook_hash("SessionStart", base)
        assert hs.codex_hook_hash("Stop", dict(base, additionalContextLimit=6000)) == \
            hs.codex_hook_hash("Stop", base)

    def test_key_names_the_file_event_and_position(self):
        assert hs.codex_hook_key("/h/hooks.json", "UserPromptSubmit", 2) == "/h/hooks.json:user_prompt_submit:2:0"


# ── Codex ────────────────────────────────────────────────────────────────────


USER_CONFIG = '''# my codex config
model = "gpt-9"

[projects."/w/repo"]
trust_level = "trusted"

[mcp_servers.other]
command = "other-mcp"
args = ["--x"]
'''


def _codex_home(tmp_path: Path, *, config: str | None = USER_CONFIG, hooks: dict | None = None) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    if config is not None:
        (home / "config.toml").write_text(config)
    if hooks is not None:
        (home / "hooks.json").write_text(json.dumps(hooks))
    return home


def _user_hooks() -> dict:
    return {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]}}


def _strip(parsed: dict) -> dict:
    parsed = json.loads(json.dumps(parsed))
    parsed.get("mcp_servers", {}).pop("miragen-bridge", None)
    if parsed.get("mcp_servers") == {}:
        parsed.pop("mcp_servers")
    parsed.pop("hooks", None)
    return parsed


class TestEnsureCodex:
    def test_not_installed_is_a_no_op_that_creates_nothing(self, tmp_path):
        status = hs.ensure_codex(tmp_path / "nope", url="https://m.example")
        assert status["installed"] is False and not (tmp_path / "nope").exists()

    def test_hooks_trust_and_mcp(self, tmp_path):
        home = _codex_home(tmp_path, hooks=_user_hooks())
        status = hs.ensure_codex(home, url="https://m.example", token_file="/secrets/t")
        assert status["current"] and status["installed"]

        hooks = json.loads((home / "hooks.json").read_text())["hooks"]
        # user groups kept, in place (their trust keys do not move)
        assert hooks["SessionStart"][0]["hooks"][0]["command"] == "echo mine"
        assert hooks["PreToolUse"] == _user_hooks()["hooks"]["PreToolUse"]
        ours = {event: groups[-1]["hooks"][0] for event, groups in hooks.items() if event != "PreToolUse"}
        assert {event: e["timeout"] for event, e in ours.items()} == dict(CODEX_EVENTS)
        assert "PostToolUseFailure" not in hooks  # Codex has no such event
        copy = next((home / "miragen-adapter").glob("[0-9a-f]*"))
        for entry in ours.values():
            assert entry["command"] == (f"python3 {copy}/miragen_hook/__main__.py codex "
                                        "--daemon https://m.example --token-file /secrets/t")
        assert ours["SessionStart"]["additionalContextLimit"] == hs.CODEX_CONTEXT_TOKEN_LIMIT
        assert "additionalContextLimit" not in ours["Stop"]

        config = tomllib.loads((home / "config.toml").read_text())
        assert _strip(config) == _strip(tomllib.loads(USER_CONFIG))  # nothing else changed
        server = config["mcp_servers"]["miragen-bridge"]
        assert server["command"] == "python3"
        assert server["default_tools_approval_mode"] == "approve"  # codex exec: approval policy never
        assert server["args"] == [f"{copy}/miragen_hook/__main__.py", "mcp-proxy", "--harness", "codex",
                                  "--daemon", "https://m.example", "--token-file", "/secrets/t"]
        state = config["hooks"]["state"]
        key = f"{home}/hooks.json"
        # exactly our entries are trusted, at their merged positions
        assert state[f"{key}:session_start:1:0"]["trusted_hash"] == \
            hs.codex_hook_hash("SessionStart", ours["SessionStart"])
        assert state[f"{key}:stop:0:0"]["trusted_hash"] == hs.codex_hook_hash("Stop", ours["Stop"])
        assert f"{key}:session_start:0:0" not in state  # the user's own hook: never trusted by us
        assert not any(k.startswith(f"{key}:pre_tool_use") for k in state)
        assert len(state) == len(CODEX_EVENTS)

    def test_second_run_is_a_pure_read(self, tmp_path):
        home = _codex_home(tmp_path, hooks=_user_hooks())
        hs.ensure_codex(home, url="https://m.example")
        files = [home / "hooks.json", home / "config.toml", home / "miragen-adapter" / "setup.json"]
        before = {p: p.stat().st_mtime_ns for p in files}
        time.sleep(0.01)
        status = hs.ensure_codex(home, url="https://m.example")
        assert status["changed"] == [] and {p: p.stat().st_mtime_ns for p in files} == before

    def test_codex_appending_to_the_file_does_not_cause_a_rewrite(self, tmp_path):
        """Codex writes config.toml itself (project trust…): our tables are
        then no longer last — still current, still no write."""
        home = _codex_home(tmp_path)
        hs.ensure_codex(home, url="https://m.example")
        with (home / "config.toml").open("a") as f:
            f.write('\n[projects."/w/other"]\ntrust_level = "trusted"\n')
        text = (home / "config.toml").read_text()
        assert hs.ensure_codex(home, url="https://m.example")["changed"] == []
        assert (home / "config.toml").read_text() == text

    def test_url_change_rewrites_and_moves_trust(self, tmp_path):
        home = _codex_home(tmp_path)
        hs.ensure_codex(home, url="https://a.example")
        hs.ensure_codex(home, url="https://b.example")
        config = tomllib.loads((home / "config.toml").read_text())
        assert "https://b.example" in config["mcp_servers"]["miragen-bridge"]["args"]
        hooks = json.loads((home / "hooks.json").read_text())["hooks"]
        assert all(len(groups) == 1 for groups in hooks.values())
        state = config["hooks"]["state"]
        assert state[f"{home}/hooks.json:stop:0:0"]["trusted_hash"] == \
            hs.codex_hook_hash("Stop", hooks["Stop"][0]["hooks"][0])
        assert len(state) == len(CODEX_EVENTS)

    def test_stale_trust_is_removed_when_our_position_moves(self, tmp_path):
        home = _codex_home(tmp_path)
        hs.ensure_codex(home, url="https://m.example")
        hooks = json.loads((home / "hooks.json").read_text())
        hooks["hooks"]["Stop"].insert(0, {"hooks": [{"type": "command", "command": "user-stop"}]})
        (home / "hooks.json").write_text(json.dumps(hooks))
        hs.ensure_codex(home, url="https://m.example")
        state = tomllib.loads((home / "config.toml").read_text())["hooks"]["state"]
        assert f"{home}/hooks.json:stop:1:0" in state
        assert f"{home}/hooks.json:stop:0:0" not in state  # would now name the USER's hook

    def test_a_group_after_ours_keeps_its_index_and_trust(self, tmp_path):
        """Codex keys trust by group index: our group is replaced IN PLACE,
        never moved behind a later one (a user's, or another daemon's that
        edits the same file) — that would untrust it, and two setups that
        each re-append would ping-pong forever."""
        home = _codex_home(tmp_path)
        hs.ensure_codex(home, url="https://m.example")
        hooks = json.loads((home / "hooks.json").read_text())
        foreign = {"hooks": [{"type": "command", "command": "python3 /x/miradesign_hook/__main__.py codex"}]}
        hooks["hooks"]["Stop"].append(foreign)
        (home / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n")
        before = (home / "hooks.json").read_text()
        assert hs.ensure_codex(home, url="https://m.example")["changed"] == []
        assert (home / "hooks.json").read_text() == before
        hs.ensure_codex(home, url="https://other.example")  # an update rewrites our entry only
        stop = json.loads((home / "hooks.json").read_text())["hooks"]["Stop"]
        assert stop[1] == foreign and "https://other.example" in stop[0]["hooks"][0]["command"]
        state = tomllib.loads((home / "config.toml").read_text())["hooks"]["state"]
        assert f"{home}/hooks.json:stop:0:0" in state and f"{home}/hooks.json:stop:1:0" not in state

    def test_owned_duplicates_collapse_into_the_first(self, tmp_path):
        legacy = {"hooks": [{"type": "command", "command": "miragen-hook codex --daemon http://old"}]}
        user = {"hooks": [{"type": "command", "command": "user-stop"}]}
        home = _codex_home(tmp_path, hooks={"hooks": {"Stop": [legacy, user, legacy]}})
        hs.ensure_codex(home, url="https://m.example")
        stop = json.loads((home / "hooks.json").read_text())["hooks"]["Stop"]
        assert len(stop) == 2 and stop[1] == user and "miragen-adapter" in stop[0]["hooks"][0]["command"]

    def test_a_users_disable_is_kept(self, tmp_path):
        home = _codex_home(tmp_path)
        hs.ensure_codex(home, url="https://m.example")
        key = f"{home}/hooks.json:stop:0:0"
        text = (home / "config.toml").read_text().replace(
            f'[hooks.state."{key}"]  {hs.MANAGED_COMMENT}\ntrusted_hash',
            f'[hooks.state."{key}"]  {hs.MANAGED_COMMENT}\nenabled = false\ntrusted_hash')
        (home / "config.toml").write_text(text)
        hs.ensure_codex(home, url="https://other.example")
        state = tomllib.loads((home / "config.toml").read_text())["hooks"]["state"]
        assert state[key]["enabled"] is False

    def test_an_unmarked_table_for_our_key_is_replaced(self, tmp_path):
        """Trusting through Codex's own /hooks writes an unmarked table for
        our key: it is ours by key and gets the current hash."""
        home = _codex_home(tmp_path)
        hs.ensure_codex(home, url="https://m.example")
        key = f"{home}/hooks.json:stop:0:0"
        text = (home / "config.toml").read_text().replace(f'[hooks.state."{key}"]  {hs.MANAGED_COMMENT}',
                                                         f'[hooks.state."{key}"]')
        (home / "config.toml").write_text(text)
        hs.ensure_codex(home, url="https://z.example")
        parsed = tomllib.loads((home / "config.toml").read_text())
        hooks = json.loads((home / "hooks.json").read_text())["hooks"]
        assert parsed["hooks"]["state"][key]["trusted_hash"] == hs.codex_hook_hash("Stop", hooks["Stop"][0]["hooks"][0])

    @pytest.mark.parametrize("config", [
        'mcp_servers = { miragen-bridge = { url = "http://x" } }\n',
        '[mcp_servers]\nmiragen-bridge = { url = "http://x" }\n',
        '[hooks]\nstate = { "anything" = { trusted_hash = "sha256:0" } }\n',
    ])
    def test_our_keys_defined_inline_refuse_and_touch_nothing(self, tmp_path, config):
        home = _codex_home(tmp_path, config=config, hooks=_user_hooks())
        hooks_before = (home / "hooks.json").read_text()
        with pytest.raises(hs.SetupError):
            hs.ensure_codex(home, url="https://m.example")
        assert (home / "config.toml").read_text() == config
        assert (home / "hooks.json").read_text() == hooks_before  # no untrusted hooks left behind

    def test_a_misread_table_is_refused_not_dropped(self, tmp_path, monkeypatch):
        """The text cut is checked against the parsed file: if the block
        reader ever took a user table for ours, nothing is written."""
        home = _codex_home(tmp_path)
        original_is_ours = hs._is_ours
        monkeypatch.setattr(hs, "_is_ours", lambda key, marked, keys: key == ["projects", "/w/repo"]
                            or original_is_ours(key, marked, keys))
        with pytest.raises(hs.SetupError, match="change other settings"):
            hs.ensure_codex(home, url="https://m.example")
        assert (home / "config.toml").read_text() == USER_CONFIG

    def test_invalid_files_are_refused(self, tmp_path):
        home = _codex_home(tmp_path, config="this is = = not toml")
        with pytest.raises(hs.SetupError, match="not valid TOML"):
            hs.ensure_codex(home, url="https://m.example")
        home2 = tmp_path / "h2"
        home2.mkdir()
        (home2 / "hooks.json").write_text("{nope")
        with pytest.raises(hs.SetupError, match="not valid JSON"):
            hs.ensure_codex(home2, url="https://m.example")

    def test_symlinked_config_keeps_the_link_and_the_mode(self, tmp_path):
        home = _codex_home(tmp_path, config=None)
        dotfiles = tmp_path / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "config.toml"
        real.write_text(USER_CONFIG)
        real.chmod(0o640)
        (home / "config.toml").symlink_to(real)
        hs.ensure_codex(home, url="https://m.example")
        assert (home / "config.toml").is_symlink()
        assert "miragen-bridge" in tomllib.loads(real.read_text())["mcp_servers"]
        assert stat.S_IMODE(real.stat().st_mode) == 0o640

    def test_both_spellings_of_a_symlinked_home_are_trusted(self, tmp_path):
        real = _codex_home(tmp_path)
        link = tmp_path / "link-home"
        link.symlink_to(real)
        hs.ensure_codex(link, url="https://m.example")
        state = tomllib.loads((real / "config.toml").read_text())["hooks"]["state"]
        assert f"{link}/hooks.json:stop:0:0" in state and f"{real}/hooks.json:stop:0:0" in state

    def test_remove(self, tmp_path):
        home = _codex_home(tmp_path, hooks=_user_hooks())
        hs.ensure_codex(home, url="https://m.example")
        hs.remove_codex(home)
        assert json.loads((home / "hooks.json").read_text()) == _user_hooks()
        config = tomllib.loads((home / "config.toml").read_text())
        assert config == tomllib.loads(USER_CONFIG)
        assert not (home / "miragen-adapter").exists()

    def test_the_toml_edit_keeps_everything_else_byte_for_byte(self, tmp_path):
        home = _codex_home(tmp_path)
        hs.ensure_codex(home, url="https://m.example")
        assert (home / "config.toml").read_text().startswith(USER_CONFIG)


# ── Grok Build ───────────────────────────────────────────────────────────────


class TestEnsureGrok:
    def test_not_installed_is_a_no_op(self, tmp_path):
        assert hs.ensure_grok(tmp_path / ".grok", url="https://m.example")["installed"] is False
        assert not (tmp_path / ".grok").exists()

    def test_hook_file_and_setup_record(self, tmp_path):
        home = tmp_path / ".grok"
        (home / "hooks").mkdir(parents=True)
        (home / "hooks" / "other.json").write_text('{"hooks": {}}')
        status = hs.ensure_grok(home, url="https://m.example", token_file="/s/tok")
        hooks = json.loads((home / "hooks" / "miragen.json").read_text())["hooks"]
        assert {event: groups[0]["hooks"][0]["timeout"] for event, groups in hooks.items()} == dict(GROK_BUILD_EVENTS)
        copy = next((home / "miragen-adapter").glob("[0-9a-f]*"))
        assert hooks["PostToolUse"][0]["hooks"][0]["command"] == (
            f"python3 {copy}/miragen_hook/__main__.py grok-build --daemon https://m.example --token-file /s/tok")
        assert (home / "hooks" / "other.json").read_text() == '{"hooks": {}}'
        assert hs.read_setup_record(home) == {"url": "https://m.example", "token_file": "/s/tok",
                                              "managed_by": "miragend"}
        assert status["current"]
        again = hs.ensure_grok(home, url="https://m.example", token_file="/s/tok")
        assert again["changed"] == []

    def test_legacy_plugin_dir_entries_are_replaced(self, tmp_path):
        home = tmp_path / ".grok"
        (home / "hooks").mkdir(parents=True)
        legacy = {"hooks": {"Stop": [{"hooks": [{"type": "command",
                  "command": "PYTHONPATH=/p python3 /p/miragen_hook/__main__.py grok-build"}]}]}}
        (home / "hooks" / "miragen.json").write_text(json.dumps(legacy))
        hs.ensure_grok(home, url="https://m.example")
        hooks = json.loads((home / "hooks" / "miragen.json").read_text())["hooks"]
        assert len(hooks["Stop"]) == 1 and "miragen-adapter" in hooks["Stop"][0]["hooks"][0]["command"]

    def test_a_dollar_is_refused(self, tmp_path):
        (tmp_path / ".grok").mkdir()
        with pytest.raises(hs.SetupError, match=r"'\$'"):
            hs.ensure_grok(tmp_path / ".grok", url="https://m.example", token_file="/s/$HOME/t")
        assert not (tmp_path / ".grok" / "hooks").exists()

    def test_the_hook_runs_the_copy_even_inside_a_checkout(self, tmp_path):
        """Executed for real through `sh -c` in a directory holding another
        `miragen_hook` (a miragen checkout as the session's repository)."""
        home = tmp_path / ".grok"
        home.mkdir()
        hs.ensure_grok(home, url="http://127.0.0.1:1")
        command = json.loads((home / "hooks" / "miragen.json").read_text())["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        checkout = tmp_path / "checkout" / "miragen_hook"
        checkout.mkdir(parents=True)
        (checkout / "__init__.py").write_text("raise SystemExit('SHADOWED')\n")
        (checkout / "client.py").write_text("raise SystemExit('SHADOWED')\n")
        payload = json.dumps({"hook_event_name": "PostToolUse", "session_id": "s", "tool_name": "Bash"})
        ran = subprocess.run(["sh", "-c", command], input=payload, capture_output=True, text=True,
                             cwd=checkout.parent, env={"PATH": os.environ["PATH"], "PYTHONPATH": str(checkout.parent),
                                                               "HOME": str(tmp_path)},
                             timeout=20, check=False)
        assert (ran.returncode, ran.stdout) == (0, ""), ran.stderr
        assert "SHADOWED" not in ran.stderr

    def test_remove(self, tmp_path):
        home = tmp_path / ".grok"
        home.mkdir()
        hs.ensure_grok(home, url="https://m.example")
        hs.remove_grok(home)
        assert not (home / "hooks" / "miragen.json").exists() and not (home / "miragen-adapter").exists()


# ── the adapter copy ─────────────────────────────────────────────────────────


class TestAdapterCopy:
    def test_copy_is_the_running_package(self, tmp_path):
        root = hs.ensure_adapter_copy(tmp_path, [])
        assert root.name == hs.adapter_digest()
        assert hs.adapter_digest(root / "miragen_hook") == hs.adapter_digest()

    def test_superseded_copies_live_a_day(self, tmp_path):
        current = hs.ensure_adapter_copy(tmp_path, [])
        old = tmp_path / "miragen-adapter" / "0000000000000000"
        (old / "miragen_hook").mkdir(parents=True)
        assert hs.prune_adapter_copies(tmp_path, current) == []
        assert (old / ".superseded").exists()  # marked, not deleted: a session may still run it
        assert hs.prune_adapter_copies(tmp_path, current, now=time.time() + 3600) == []
        assert hs.prune_adapter_copies(tmp_path, current, now=time.time() + hs.PRUNE_AFTER_S + 5) == [str(old)]
        assert current.exists()

    def test_a_broken_copy_is_replaced(self, tmp_path):
        root = hs.ensure_adapter_copy(tmp_path, [])
        (root / "miragen_hook" / "client.py").write_text("broken")
        changed: list[str] = []
        again = hs.ensure_adapter_copy(tmp_path, changed)
        assert again == root and changed == [str(root)]
        assert hs.adapter_digest(root / "miragen_hook") == hs.adapter_digest()


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_setup_cli(tmp_path):
    home = _codex_home(tmp_path)
    ran = subprocess.run([sys.executable, "-m", "miragen_hook", "setup", "codex", "--home", str(home),
                          "--daemon", "https://m.example"], capture_output=True, text=True, timeout=30,
                         check=False, cwd=Path(__file__).resolve().parents[1])
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout)["current"] is True
    ran = subprocess.run([sys.executable, "-m", "miragen_hook", "setup", "codex", "--home", str(home),
                          "--remove"], capture_output=True, text=True, timeout=30, check=False,
                         cwd=Path(__file__).resolve().parents[1])
    assert ran.returncode == 0 and "miragen-bridge" not in (home / "config.toml").read_text()



# ── review fixes: ordering, user additions, locking, TOML edges ──────────────


def test_trust_is_written_before_the_hooks(tmp_path):
    """config.toml whose target cannot be written (a dotfile symlink into a
    read-only directory): hooks.json must stay untouched — new hooks without
    their trust would be skipped by codex exec and prompt in the TUI."""
    home = _codex_home(tmp_path, config=None, hooks=_user_hooks())
    locked = tmp_path / "ro"
    locked.mkdir()
    (locked / "config.toml").write_text(USER_CONFIG)
    (home / "config.toml").symlink_to(locked / "config.toml")
    locked.chmod(0o555)
    try:
        before = (home / "hooks.json").read_text()
        with pytest.raises(OSError):
            hs.ensure_codex(home, url="https://m.example")
        assert (home / "hooks.json").read_text() == before
    finally:
        locked.chmod(0o755)


def test_user_additions_to_our_server_table_are_kept(tmp_path):
    config = (USER_CONFIG + '\n[mcp_servers.miragen-bridge]\ncommand = "old"\nenabled = false\n'
              'startup_timeout_sec = 30\n\n[mcp_servers.miragen-bridge.env]\nFOO = "1"\n\n'
              '[mcp_servers.miragen-bridge.tools.store_put_artifact]\napproval_mode = "prompt"\n')
    home = _codex_home(tmp_path, config=config)
    hs.ensure_codex(home, url="https://m.example")
    server = tomllib.loads((home / "config.toml").read_text())["mcp_servers"]["miragen-bridge"]
    assert server["enabled"] is False and server["startup_timeout_sec"] == 30
    assert server["env"] == {"FOO": "1"}
    assert server["tools"]["store_put_artifact"]["approval_mode"] == "prompt"  # narrows our approve
    assert server["command"] == "python3" and server["default_tools_approval_mode"] == "approve"
    again = hs.ensure_codex(home, url="https://m.example")
    assert again["changed"] == []  # not reverted every interval


def test_setup_waits_for_the_shared_lock(tmp_path):
    import fcntl
    import threading
    home = _codex_home(tmp_path)
    fd = os.open(home / ".mira-harness-setup.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    done = threading.Event()
    worker = threading.Thread(target=lambda: (hs.ensure_codex(home, url="https://m.example"), done.set()))
    worker.start()
    try:
        assert not done.wait(0.4)  # blocked by the other writer (the MiraDesign setup)
        assert not (home / "hooks.json").exists()
    finally:
        os.close(fd)
    worker.join(10)
    assert done.is_set() and (home / "hooks.json").exists()
    assert hs.LOCK_FILE == ".mira-harness-setup.lock"  # the name both adapters share


def test_command_windows_is_not_hashed():
    entry = {"type": "command", "command": "x", "timeout": 5}
    assert hs.codex_hook_hash("Stop", dict(entry, commandWindows="y.cmd")) == hs.codex_hook_hash("Stop", entry)


@pytest.mark.parametrize("config", [
    "﻿" + USER_CONFIG,
    USER_CONFIG + '\n[projects."/home/a]b"]\ntrust_level = "trusted"\n',
    USER_CONFIG.replace("\n", "\r\n"),
    'model = "x"',
])
def test_toml_edges_are_edited_not_refused(tmp_path, config):
    home = _codex_home(tmp_path, config=config)
    hs.ensure_codex(home, url="https://m.example")
    text = (home / "config.toml").read_text()
    assert text.startswith("﻿") == config.startswith("﻿")
    parsed = tomllib.loads(text.lstrip("﻿"))
    assert _strip(parsed) == _strip(tomllib.loads(config.lstrip("﻿")))
    assert hs.ensure_codex(home, url="https://z.example")["current"]  # and again, with a change


def test_a_bracket_key_after_our_table_is_not_swallowed(tmp_path):
    home = _codex_home(tmp_path)
    hs.ensure_codex(home, url="https://m.example")
    with (home / "config.toml").open("a") as f:
        f.write('\n[projects."/home/a]b"]\ntrust_level = "trusted"\n')
    hs.ensure_codex(home, url="https://other.example")  # must not be refused
    assert tomllib.loads((home / "config.toml").read_text())["projects"]["/home/a]b"]["trust_level"] == "trusted"


def test_a_comment_after_our_table_survives(tmp_path):
    home = _codex_home(tmp_path)
    hs.ensure_codex(home, url="https://m.example")
    with (home / "config.toml").open("a") as f:
        f.write('\n# ---- my projects ----\n[projects."/w/new"]\ntrust_level = "trusted"\n')
    hs.ensure_codex(home, url="https://other.example")
    text = (home / "config.toml").read_text()
    assert '# ---- my projects ----\n[projects."/w/new"]' in text


def test_toml_strings_are_toml_not_json():
    for value in ("emoji \U0001F600 path", 'q"uote\\back', "tab\tnl\n", "del\x7f"):
        assert tomllib.loads(f"k = {hs._toml_string(value)}")["k"] == value


def test_codex_install_carries_the_context_limit(tmp_path):
    from miragen_hook.install import install_hooks
    path = install_hooks("codex", daemon_url="http://d", token_file=None, settings_path=tmp_path / "h.json")
    hooks = json.loads(path.read_text())["hooks"]
    assert hooks["SessionStart"][0]["hooks"][0]["additionalContextLimit"] == hs.CODEX_CONTEXT_TOKEN_LIMIT
    assert "additionalContextLimit" not in hooks["Stop"][0]["hooks"][0]



def test_a_mixed_user_group_keeps_its_shape(tmp_path):
    """Our handler inside a user's group (next to theirs, under their
    matcher): only our handler is replaced; only it is trusted."""
    mixed = {"matcher": "startup", "hooks": [
        {"type": "command", "command": "user-start"},
        {"type": "command", "command": "miragen-hook codex --daemon http://old"}]}
    home = _codex_home(tmp_path, hooks={"hooks": {"SessionStart": [mixed]}})
    hs.ensure_codex(home, url="https://m.example")
    group = json.loads((home / "hooks.json").read_text())["hooks"]["SessionStart"]
    assert len(group) == 1 and group[0]["matcher"] == "startup"
    assert group[0]["hooks"][0] == {"type": "command", "command": "user-start"}
    assert "miragen-adapter" in group[0]["hooks"][1]["command"]
    state = tomllib.loads((home / "config.toml").read_text())["hooks"]["state"]
    ours = f"{home}/hooks.json:session_start:0:1"
    assert state[ours]["trusted_hash"] == hs.codex_hook_hash("SessionStart", group[0]["hooks"][1], "startup")
    assert f"{home}/hooks.json:session_start:0:0" not in state
    hs.remove_codex(home)
    group = json.loads((home / "hooks.json").read_text())["hooks"]["SessionStart"]
    assert group == [{"matcher": "startup", "hooks": [{"type": "command", "command": "user-start"}]}]


def test_our_table_is_rewritten_where_it_stands(tmp_path):
    config = ('model = "x"\n\n[mcp_servers.miragen-bridge]\ncommand = "old"\n\n'
              '# my projects\n[projects."/a"]\ntrust_level = "trusted"\n')
    home = _codex_home(tmp_path, config=config)
    hs.ensure_codex(home, url="https://m.example")
    text = (home / "config.toml").read_text()
    assert text.index("[mcp_servers.miragen-bridge]") < text.index("# my projects\n[projects.\"/a\"]")


def test_the_daemon_installs_from_its_start_time_snapshot(tmp_path, monkeypatch):
    """A branch switch in the live checkout after start never reaches the homes."""
    live = tmp_path / "live" / "miragen_hook"
    shutil.copytree(hs.adapter_source(), live, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(hs, "adapter_source", lambda: live)
    snap = hs.snapshot_adapter(tmp_path / "snap")
    (live / "client.py").write_text("# someone checked out another branch\n")
    home = tmp_path / ".grok"
    home.mkdir()
    hs.ensure_grok(home, url="https://m.example", source=snap)
    copy = next((home / "miragen-adapter").glob("[0-9a-f]*"))
    assert (copy / "miragen_hook" / "client.py").read_bytes() == (snap / "client.py").read_bytes()
    assert copy.name == hs.adapter_digest(copy / "miragen_hook")  # digest names the content


def test_a_source_changing_mid_copy_is_not_published(tmp_path, monkeypatch):
    src = tmp_path / "src" / "miragen_hook"
    shutil.copytree(hs.adapter_source(), src, ignore=shutil.ignore_patterns("__pycache__"))
    real_copy = shutil.copy2

    def racing_copy(a, b, *args, **kw):
        result = real_copy(a, b, *args, **kw)
        if Path(a).name == "client.py":
            Path(b).write_text("# changed underneath\n")
        return result
    monkeypatch.setattr(hs.shutil, "copy2", racing_copy)
    with pytest.raises(hs.SetupError, match="changed while it was copied"):
        hs.ensure_adapter_copy(tmp_path / "home", [], src)
    assert not [p for p in (tmp_path / "home" / "miragen-adapter").iterdir() if not p.name.startswith(".")]
