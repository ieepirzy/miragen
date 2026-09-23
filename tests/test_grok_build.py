"""Grok Build as a third external harness (source-read at grok-build 1.0.41,
2026-09-23): its payload dialect, context delivery on tool results (it
discards SessionStart/UserPromptSubmit stdout), the daemon URL when no
CLAUDE_PLUGIN_OPTION_* is exported (Grok and Codex), its plugin files, and
the bridge MCP session binding over the X-Harness-Session header."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from shlex import quote as shlex_quote

from starlette.testclient import TestClient

from miragen.daemon.api import create_app
from miragen_hook.client import (
    DEFAULT_DAEMON_URL,
    DEFERRED_CONTEXT_CAP,
    foreign_entry_under_grok,
    pending_dir,
    resolve_daemon_url,
    run,
    stash_context,
    take_context,
)
from miragen_hook.install import GROK_BUILD_EVENTS, install_hooks
from miragen_hook.normalize import event_idempotency_key, normalize_hook_payload
from tests.test_hosted_bridge import _config
from tests.test_sessions_plane import Harness

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "miragen-memory"


def grok(event: str, session: str = "g-1", **fields) -> dict:
    """A payload exactly as Grok Build's `to_hook_json` builds it: the
    camelCase envelope, `hookEventName` carrying the snake display name,
    and only the closed list of snake aliases (hook_event_name PascalCase,
    session_id, …) — nothing else in snake case."""
    snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in event).lstrip("_")
    payload = {
        "hookEventName": snake, "hook_event_name": event,
        "sessionId": session, "session_id": session,
        "cwd": "/w/repo", "workspaceRoot": "/w/repo", "timestamp": "2026-09-23T12:00:00Z",
        **fields,
    }
    return payload


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(body: dict, calls: list | None = None):
    def opener(request, timeout):
        if calls is not None:
            calls.append(json.loads(request.data))
        return _Response(json.dumps(body).encode())
    return opener


def _refusing_opener(request, timeout):
    raise AssertionError("the daemon must not be contacted for this event")


# ── payload dialect ──────────────────────────────────────────────────────────


class TestGrokPayloads:
    def test_session_start_new_and_load(self):
        started = normalize_hook_payload("grok-build", grok("SessionStart", source="new"))
        assert (started.name, started.session_id) == ("context.started", "g-1")
        loaded = normalize_hook_payload("grok-build", grok("SessionStart", source="load"))
        assert loaded.name == "context.restored"

    def test_bare_camel_case_payload_still_parses(self):
        """No snake aliases at all (an older/newer Grok, or a Cursor-shaped
        client): hookEventName's snake value is enough."""
        bare = {"hookEventName": "user_prompt_submit", "sessionId": "g-2", "promptId": "p-9",
                "prompt": "hei"}
        event = normalize_hook_payload("grok-build", bare)
        assert (event.name, event.session_id, event.content) == ("input.received", "g-2", "hei")
        assert event.ids == {"prompt_id": "p-9"}

    def test_prompt_id_anchors_the_capture_key(self):
        """promptId exists only in camelCase; without it two identical
        prompts in one session would share a key and the second would be
        dropped as a redelivery."""
        first = normalize_hook_payload("grok-build", grok("UserPromptSubmit", prompt="ok", promptId="p-1"))
        second = normalize_hook_payload("grok-build", grok("UserPromptSubmit", prompt="ok", promptId="p-2"))
        assert first.ids == {"prompt_id": "p-1"}
        assert event_idempotency_key(first) != event_idempotency_key(second)

    def test_stop_reads_camel_case_message(self):
        event = normalize_hook_payload("grok-build", grok(
            "Stop", reason="end_turn", stopHookActive=False, lastAssistantMessage="Valmis."))
        assert (event.name, event.content) == ("turn.finished", "Valmis.")

    def test_session_end_stop_is_not_a_turn(self):
        for reason in ("channel_closed", "shutdown"):
            assert normalize_hook_payload("grok-build", grok("Stop", reason=reason)) is None
        closed = normalize_hook_payload("grok-build", grok("SessionEnd", reason="channel_closed"))
        assert (closed.name, closed.attributes["reason"]) == ("context.closed", "channel_closed")

    def test_subagent_events(self):
        start = normalize_hook_payload("grok-build", grok(
            "SubagentStart", subagentId="sub-1", subagentType="explore", description="look"))
        assert (start.name, start.ids, start.attributes["agent_type"]) == (
            "context.child_started", {"agent_id": "sub-1"}, "explore")
        gate = grok("SubagentStop", phase="gate", subagentId="sub-1", subagentType="explore",
                    stopHookActive=False, lastAssistantMessage="half")
        assert normalize_hook_payload("grok-build", gate) is None
        done = normalize_hook_payload("grok-build", {**gate, "phase": "observe",
                                                     "lastAssistantMessage": "found it"})
        assert (done.name, done.content) == ("context.child_finished", "found it")

    def test_a_subagents_own_session_is_not_registered(self):
        """Grok runs a subagent as a session of its own that fires the full
        lifecycle marked with subagentType — the parent's Subagent* events
        already record it."""
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            payload = grok(event, session="sub-1", subagentType="explore", prompt="x",
                           reason="end_turn", source="new")
            assert normalize_hook_payload("grok-build", payload) is None, event

    def test_compaction_source_is_the_trigger(self):
        event = normalize_hook_payload("grok-build", grok("PreCompact", source="auto"))
        assert (event.name, event.attributes["trigger"]) == ("context.compacting", "auto")

    def test_tool_failure_uses_the_snake_alias(self):
        event = normalize_hook_payload("grok-build", grok(
            "PostToolUseFailure", toolName="run_terminal_command", tool_name="run_terminal_command",
            error="exit 1"))
        assert (event.name, event.attributes["tool_name"]) == ("tool.finished", "run_terminal_command")


class TestForeignEntriesUnderGrok:
    def test_only_the_grok_entry_speaks_for_a_grok_session(self):
        """Grok also runs Claude Code hook entries (settings files, plugins):
        a claude-code entry there must not forward the event a second time
        next to ~/.grok/hooks/miragen.json. GROK_HOOK_EVENT marks Grok (it
        also sets CLAUDE_PROJECT_DIR, so that proves nothing)."""
        env = {"GROK_HOOK_EVENT": "session_start", "CLAUDE_PROJECT_DIR": "/w/repo"}
        assert foreign_entry_under_grok("claude-code", env)
        assert not foreign_entry_under_grok("grok-build", env)
        assert not foreign_entry_under_grok("claude-code", {"CLAUDE_PROJECT_DIR": "/w/repo"})

    def test_a_claude_code_entry_under_grok_does_nothing(self, tmp_path):
        env = {"GROK_HOOK_EVENT": "session_start", "GROK_PLUGIN_DATA": str(tmp_path)}
        out = run("claude-code", grok("SessionStart", source="new"), daemon_url="http://d",
                  token=None, environ=env, opener=_refusing_opener, pid=1)
        assert out is None

    def test_the_grok_entry_is_filed_under_grok_build(self, tmp_path):
        calls: list = []
        env = {"GROK_HOOK_EVENT": "session_start", "GROK_PLUGIN_DATA": str(tmp_path)}
        run("grok-build", grok("SessionStart", source="new"), daemon_url="http://d", token=None,
            environ=env, opener=_opener({"context": "ctx"}, calls), pid=1)
        assert calls[0]["harness"] == "grok-build"
        assert calls[0]["client"]["context_delivery"] == "deferred"


# ── context delivery on tool results ─────────────────────────────────────────


class TestDeferredContext:
    def _env(self, tmp_path) -> dict:
        return {"GROK_HOOK_EVENT": "x", "GROK_PLUGIN_DATA": str(tmp_path)}

    def test_start_context_waits_for_the_first_tool_result(self, tmp_path):
        env = self._env(tmp_path)
        start = run("grok-build", grok("SessionStart", source="new"), daemon_url="http://d",
                    token=None, environ=env, opener=_opener({"context": "[memory] hello"}), pid=1)
        assert start is None  # Grok discards SessionStart stdout
        first_tool = run("grok-build", grok("PostToolUse", toolName="read_file", tool_name="read_file"),
                         daemon_url="http://d", token=None, environ=env, opener=_refusing_opener)
        assert first_tool == {"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": "[memory] hello"}}
        again = run("grok-build", grok("PostToolUse", toolName="read_file", tool_name="read_file"),
                    daemon_url="http://d", token=None, environ=env, opener=_refusing_opener)
        assert again is None  # exactly once
        assert list(pending_dir(env).iterdir()) == []  # nothing left behind

    def test_a_later_prompt_replaces_an_undelivered_one(self, tmp_path):
        """Turn 1 used no tool: its recall must not reach the model in turn 2
        next to turn 2's own — only the start block and the newest prompt's."""
        env = self._env(tmp_path)
        for event, context in (("SessionStart", "START"), ("UserPromptSubmit", "RECALL-1"),
                               ("UserPromptSubmit", "RECALL-2")):
            run("grok-build", grok(event, source="new", prompt="q", promptId=context),
                daemon_url="http://d", token=None, environ=env, opener=_opener({"context": context}), pid=1)
        assert take_context("g-1", env) == "START\n\nRECALL-2"

    def test_a_new_start_replaces_everything(self, tmp_path):
        env = self._env(tmp_path)
        stash_context("g-1", "old recall", env, origin="UserPromptSubmit")
        stash_context("g-1", "fresh start", env, origin="SessionStart")
        assert take_context("g-1", env) == "fresh start"

    def test_stale_context_is_never_delivered(self, tmp_path):
        import os
        import time
        env = self._env(tmp_path)
        stash_context("g-1", "from a crashed session", env)
        [path] = list(pending_dir(env).iterdir())
        old = time.time() - 13 * 3600
        os.utime(path, (old, old))
        assert take_context("g-1", env) is None
        assert list(pending_dir(env).iterdir()) == []

    def test_prompt_context_appends_and_is_delivered_on_a_failure_too(self, tmp_path):
        env = self._env(tmp_path)
        for event, context in (("SessionStart", "start"), ("UserPromptSubmit", "recall")):
            run("grok-build", grok(event, source="new", prompt="q", promptId="p"),
                daemon_url="http://d", token=None, environ=env, opener=_opener({"context": context}), pid=1)
        calls: list = []
        failed = run("grok-build", grok("PostToolUseFailure", toolName="t", tool_name="t", error="boom"),
                     daemon_url="http://d", token=None, environ=env, opener=_opener({}, calls), pid=1)
        assert failed["hookSpecificOutput"]["additionalContext"] == "start\n\nrecall"
        assert calls and calls[0]["event"]["name"] == "tool.finished"  # still captured

    def test_claude_code_is_untouched(self, tmp_path):
        out = run("claude-code", {"hook_event_name": "SessionStart", "session_id": "c", "source": "startup"},
                  daemon_url="http://d", token=None, environ={"CLAUDE_PLUGIN_DATA": str(tmp_path)},
                  opener=_opener({"context": "now"}), pid=1)
        assert out["hookSpecificOutput"]["additionalContext"] == "now"
        assert not pending_dir({"CLAUDE_PLUGIN_DATA": str(tmp_path)}).exists()

    def test_cap_keeps_the_newest_text(self, tmp_path):
        env = self._env(tmp_path)
        stash_context("g-1", "A" * DEFERRED_CONTEXT_CAP, env)
        stash_context("g-1", "newest", env, origin="UserPromptSubmit")
        taken = take_context("g-1", env)
        assert len(taken) == DEFERRED_CONTEXT_CAP and taken.endswith("newest")

    def test_session_end_discards_undelivered_context(self, tmp_path):
        env = self._env(tmp_path)
        stash_context("g-1", "never delivered", env)
        run("grok-build", grok("SessionEnd", reason="shutdown"), daemon_url="http://d", token=None,
            environ=env, opener=_opener({}), pid=1)
        assert take_context("g-1", env) is None

    def test_pending_files_are_private_and_outside_the_repo(self, tmp_path):
        env = self._env(tmp_path)
        stash_context("g-1", "x", env)
        [path] = list(pending_dir(env).iterdir())
        assert path.parent == tmp_path / "pending"
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert "g-1" not in path.name  # hashed, not the raw id

    def test_unreachable_daemon_still_delivers_what_is_queued(self, tmp_path):
        env = self._env(tmp_path)
        stash_context("g-1", "queued", env)

        def refuse(request, timeout):
            raise urllib.error.URLError("refused")
        out = run("grok-build", grok("PostToolUseFailure", toolName="t", tool_name="t", error="e"),
                  daemon_url="http://d", token=None, environ=env, opener=refuse, pid=1)
        assert out["hookSpecificOutput"]["additionalContext"] == "queued"


# ── daemon URL when the harness exports no plugin options ────────────────────


class TestDaemonUrlResolution:
    def _plugin(self, tmp_path, default="https://memory.example") -> Path:
        root = tmp_path / "plugin"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
            "name": "miragen-memory", "userConfig": {"daemon_url": {"default": default}}}))
        return root

    def _settings(self, tmp_path, url: str | None) -> Path:
        path = tmp_path / "settings.json"
        configs = {"miragen-memory@miragen": {"options": {"daemon_url": url}}} if url else {}
        path.write_text(json.dumps({"pluginConfigs": configs, "enabledPlugins": {}}))
        return path

    def test_order(self, tmp_path):
        root = self._plugin(tmp_path)
        saved = self._settings(tmp_path, "http://10.8.0.4:8420")
        grok_env = {"GROK_PLUGIN_ROOT": str(root)}
        # The option Claude Code would have exported wins, then the env.
        assert resolve_daemon_url(None, {**grok_env, "CLAUDE_PLUGIN_OPTION_DAEMON_URL": "http://opt"},
                                  settings_path=saved) == "http://opt"
        assert resolve_daemon_url(None, {**grok_env, "MIRAGEND_URL": "http://env"},
                                  settings_path=saved) == "http://env"
        # Grok/Codex: the value the user saved in Claude Code's settings…
        assert resolve_daemon_url(None, grok_env, settings_path=saved) == "http://10.8.0.4:8420"
        # …else the manifest's published default — never the loopback.
        assert resolve_daemon_url(None, grok_env, settings_path=self._settings(tmp_path, None)) \
            == "https://memory.example"
        assert resolve_daemon_url("http://flag", grok_env, settings_path=saved) == "http://flag"

    def test_codex_plugin_root_also_recovers_the_option(self, tmp_path):
        root = self._plugin(tmp_path)
        saved = self._settings(tmp_path, "http://saved")
        assert resolve_daemon_url(None, {"CLAUDE_PLUGIN_ROOT": str(root)}, settings_path=saved) \
            == "http://saved"

    def test_a_bare_install_keeps_the_loopback_default(self, tmp_path):
        saved = self._settings(tmp_path, "http://saved")
        assert resolve_daemon_url(None, {}, settings_path=saved) == DEFAULT_DAEMON_URL

    def test_unreadable_settings_fall_through(self, tmp_path):
        root = self._plugin(tmp_path)
        broken = tmp_path / "broken.json"
        broken.write_text("{nope")
        assert resolve_daemon_url(None, {"GROK_PLUGIN_ROOT": str(root)}, settings_path=broken) \
            == "https://memory.example"


# ── installer + plugin files ─────────────────────────────────────────────────


class TestGrokInstallAndPlugin:
    def test_native_install(self, tmp_path):
        """The hook file is THE Grok path (Grok 1.0.41 loads plugin hooks only
        after a plugin reload): every event, the PostToolUse delivery point,
        and the stdlib adapter run from where it is installed."""
        path = install_hooks("grok-build", daemon_url="http://d", token_file=None,
                             settings_path=tmp_path / "miragen.json")
        hooks = json.loads(path.read_text())["hooks"]
        assert {event: groups[0]["hooks"][0]["timeout"] for event, groups in hooks.items()} \
            == dict(GROK_BUILD_EVENTS)
        assert "PostToolUse" in hooks  # the delivery point
        root = Path(__import__("miragen_hook").__file__).resolve().parent.parent
        assert hooks["Stop"][0]["hooks"][0]["command"] == \
            f"PYTHONPATH={shlex_quote(str(root))} python3 -m miragen_hook grok-build --daemon http://d"
        # Re-installing replaces our entries instead of adding a second set.
        install_hooks("grok-build", daemon_url=None, token_file=None, settings_path=path)
        again = json.loads(path.read_text())["hooks"]
        assert len(again["Stop"]) == 1
        assert again["Stop"][0]["hooks"][0]["command"].endswith("miragen_hook grok-build")

    def test_cli_install_from_a_checkout_refuses_without_a_url(self, tmp_path, monkeypatch, capsys):
        """A checkout has no manifest to fall back on: the runtime answer
        would be the loopback — a possibly different daemon (split memory)."""
        from miragen_hook import client
        monkeypatch.delenv("MIRAGEND_URL", raising=False)
        monkeypatch.setattr(client, "own_plugin_root", lambda: None)
        target = tmp_path / "miragen.json"
        assert client.main(["install", "grok-build", "--settings", str(target)]) == 2
        assert not target.exists() and "--daemon" in capsys.readouterr().err

    def test_cli_install_keeps_an_install_time_url(self, tmp_path, monkeypatch):
        from miragen_hook import client
        monkeypatch.setenv("MIRAGEND_URL", "https://memory.example")
        monkeypatch.setattr(client, "own_plugin_root", lambda: None)
        target = tmp_path / "miragen.json"
        assert client.main(["install", "grok-build", "--settings", str(target)]) == 0
        command = json.loads(target.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert command.endswith("grok-build --daemon https://memory.example")

    def test_commands_survive_awkward_paths(self, tmp_path):
        """Grok runs shell-form commands via `sh -c`: every part is quoted."""
        import subprocess

        from miragen_hook import install
        weird = tmp_path / 'we$ird dir/x"y'
        command = install.hook_command("grok-build", daemon_url="http://d", token_file="/tmp/my tok")
        command = command.replace(shlex_quote(str(install.adapter_root())), shlex_quote(str(weird)))
        probe = ("python3 -c 'import os, sys; "
                 "print(\"|\".join([os.environ[\"PYTHONPATH\"], *sys.argv[1:]]), end=\"|\")'")
        echoed = subprocess.run(["sh", "-c", command.replace("python3 -m miragen_hook", probe)],
                                capture_output=True, text=True, check=False)
        assert echoed.stdout.split("|")[0] == str(weird)
        assert "grok-build|--daemon|http://d|--token-file|/tmp/my tok|" in echoed.stdout

    def test_owned_marker_is_the_module_not_a_prefix(self):
        from miragen_hook.install import _group_is_owned
        mine = {"hooks": [{"command": "PYTHONPATH=/p python3 -m miragen_hook grok-build"}]}
        theirs = {"hooks": [{"command": "python3 -m miragen_hook_wrapper x"}]}
        assert _group_is_owned(mine) and not _group_is_owned(theirs)

    def test_cli_install_does_not_bake_the_loopback(self, tmp_path, monkeypatch):
        """Without --daemon the URL stays resolved per event (env → saved
        option → manifest default) — baking the loopback default would pin
        every Grok session to a daemon nobody configured."""
        from miragen_hook.client import main
        monkeypatch.delenv("MIRAGEND_URL", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_DAEMON_URL", raising=False)
        from miragen_hook import client
        monkeypatch.setattr(client, "own_plugin_root", lambda: PLUGIN)  # run from the plugin
        target = tmp_path / "miragen.json"
        assert main(["install", "grok-build", "--settings", str(target)]) == 0
        command = json.loads(target.read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert "--daemon" not in command

    def test_http_hooks_are_refused(self, tmp_path):
        import pytest
        with pytest.raises(RuntimeError, match="Grok Build"):
            install_hooks("grok-build", daemon_url="http://d", token_file=None,
                          settings_path=tmp_path / "x.json", http=True)

    def test_grok_manifest_points_at_grok_only_files(self):
        claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        manifest = json.loads((PLUGIN / ".grok-plugin" / "plugin.json").read_text())
        assert manifest["name"] == claude["name"] and manifest["version"] == claude["version"]
        assert "userConfig" not in manifest  # Grok has no plugin options
        assert (PLUGIN / manifest["hooks"]).is_file() and (PLUGIN / manifest["mcpServers"]).is_file()
        # A root plugin.json would outrank both harness manifests in Grok.
        assert not (PLUGIN / "plugin.json").exists()

    def test_grok_manifest_declares_no_hooks(self):
        """An explicit EMPTY hooks file: omitting the field would make Grok
        fall back to the Claude hooks/hooks.json once it loads plugin hooks
        (after /reload-plugins), doubling every event the hook file sends."""
        manifest = json.loads((PLUGIN / ".grok-plugin" / "plugin.json").read_text())
        assert manifest["hooks"] != "./hooks/hooks.json"
        assert json.loads((PLUGIN / manifest["hooks"]).read_text())["hooks"] == {}

    def test_vendored_copy_resolves_the_manifest_default_without_env(self, tmp_path):
        """A native Grok hook file runs the plugin's copy with no plugin env
        at all: the copy still finds its own manifest."""
        import subprocess
        import sys
        code = ("from pathlib import Path; from miragen_hook.client import resolve_daemon_url; "
                f"print(resolve_daemon_url(None, {{}}, settings_path=Path({str(tmp_path / 'none.json')!r})))")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=tmp_path, env={"PYTHONPATH": str(PLUGIN), "PATH": "/usr/bin:/bin"},
                                timeout=20, check=False)
        claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        assert result.stdout.strip() == claude["userConfig"]["daemon_url"]["default"], result.stderr

    def test_grok_mcp_config_uses_only_what_grok_expands(self):
        manifest = json.loads((PLUGIN / ".grok-plugin" / "plugin.json").read_text())
        text = (PLUGIN / manifest["mcpServers"]).read_text()
        assert "user_config" not in text  # never expanded by Grok
        server = json.loads(text)["mcpServers"]["miragen-bridge"]
        assert server["url"].startswith("${MIRAGEND_URL:-https://")  # default, never an empty URL
        assert server["headers"]["X-Harness-Session"] == "grok-build:{{session_id}}"
        # Grok adds Authorization only when the variable is set; a literal
        # `Bearer ${MIRAGEND_TOKEN:-}` header would suppress OAuth when unset.
        assert server["bearer_token_env_var"] == "MIRAGEND_TOKEN"
        assert "Authorization" not in server["headers"]


# ── bridge MCP: the session named by the connection ──────────────────────────


MCP_HEADERS = {"Authorization": "Bearer secret", "Accept": "application/json, text/event-stream"}


class TestHarnessSessionHeader:

    def _call(self, client, tool: str, arguments: dict, session_header: str | None) -> dict:
        headers = dict(MCP_HEADERS)
        if session_header is not None:
            headers["X-Harness-Session"] = session_header
        answer = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}})
        assert answer.status_code == 200, answer.text
        return json.loads(answer.json()["result"]["content"][0]["text"])

    async def test_known_session_supplies_project_and_run(self, tmp_path):
        h = Harness(tmp_path, config=_config())
        await h.send("SessionStart", harness="grok-build", session="g-1", source="new")
        session = h.plane.registry.get("grok-build:g-1")
        assert session is not None and session.project.id == "github.com/org/repo"
        app = create_app(None, token="secret", sessions=h.plane)
        with TestClient(app) as client:
            checkpoint = self._call(client, "memory_checkpoint", {"state": {"goal": "g"}},
                                    "grok-build:g-1")
            assert checkpoint["project"] == "github.com/org/repo"
            put = self._call(client, "store_put_artifact", {"kind": "note", "content": "x"},
                             "grok-build:g-1")
            assert put["status"] == "accepted"
            assert put["artifact"]["producer"]["run_id"] == session.run_id
            # An explicit argument still wins over the connection.
            other = self._call(client, "memory_checkpoint",
                               {"state": {"goal": "g"}, "project": "mcp:default"}, "grok-build:g-1")
            assert other["project"] == "mcp:default"

    async def test_unknown_or_malformed_header_never_invents_a_project(self, tmp_path):
        h = Harness(tmp_path, config=_config())
        app = create_app(None, token="secret", sessions=h.plane)
        with TestClient(app) as client:
            for header in ("grok-build:nobody", "../../etc", "grok-build:{{session_id}}", ""):
                result = self._call(client, "memory_checkpoint", {"state": {"k": 1}}, header)
                assert result["project"] == "mcp:default", header
        assert not any("nobody" in scope or "etc" in scope for scope in h.service.scopes)


# ── the daemon counts deferred context honestly ──────────────────────────────


class TestDeferredAccounting:
    async def test_deferred_context_is_counted_apart(self, tmp_path):
        """A Grok start block is answered now but shown later: /health must
        be able to tell those apart from context the model already saw."""
        h = Harness(tmp_path, config=_config())
        await h.send("SessionStart", harness="grok-build", session="g-1", source="new",
                     client_extra={"context_delivery": "deferred"})
        await h.send("SessionStart", session="c-1", source="startup")
        grok_session = h.plane.registry.get("grok-build:g-1")
        claude_session = h.plane.registry.get("claude-code:c-1")
        assert (grok_session.counters.injections, grok_session.counters.deferred_injections) == (1, 1)
        assert (claude_session.counters.injections, claude_session.counters.deferred_injections) == (1, 0)
        assert (h.plane.stats.injections, h.plane.stats.deferred_injections) == (2, 1)

    async def test_an_unknown_delivery_value_is_not_rejected(self, tmp_path):
        h = Harness(tmp_path, config=_config())
        result = await h.send("SessionStart", harness="grok-build", session="g-2", source="new",
                              client_extra={"context_delivery": "someday"})
        assert result.context and h.plane.stats.deferred_injections == 0
        long = await h.send("SessionStart", harness="grok-build", session="g-3", source="new",
                            client_extra={"context_delivery": "on-next-tool-result-" * 5})
        assert long.context  # a longer, unknown value never 422s the event
        odd = await h.send("SessionStart", harness="grok-build", session="g-4", source="new",
                           client_extra={"context_delivery": 5})
        assert odd.context  # nor does a non-string one
