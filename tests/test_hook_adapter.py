"""The stdlib-only harness adapter (miragen_hook): tolerant normalization
against the LIVE-PROBED Claude Code 2.1.270 payloads (2026-09-15) and the
documented Codex ones, envelope construction, harness-side timeouts,
fail-open forwarding, the harness output shape, and hook installation
into Claude Code settings / Codex hooks.json (owned entries only)."""

from __future__ import annotations

import io
import json
import sys
import urllib.error

import pytest

from miragen_hook import ADAPTER_VERSION
from miragen_hook.client import (
    TIMEOUT_CAPTURE_S,
    TIMEOUT_CONTEXT_S,
    TIMEOUT_SESSION_END_S,
    build_envelope,
    harness_pid,
    main,
    post_envelope,
    run,
    timeout_for,
)
from miragen_hook.install import (
    CLAUDE_CODE_EVENTS,
    install_hooks,
    remove_hook_entries,
    uninstall_hooks,
)
from miragen_hook.normalize import (
    event_idempotency_key,
    harness_output,
    normalize_hook_payload,
)

# Exactly what `claude -p` 2.1.270 delivered to a command hook on 2026-09-15.
LIVE = {
    "session_id": "1f6e2259-3e69-4233-a0b1-fff07721d8a3",
    "transcript_path": "/home/u/.claude/projects/x/1f6e2259.jsonl",
    "cwd": "/home/u/repo",
    "permission_mode": "default",
}


class TestLiveClaudeCodeSpellings:
    def test_session_start_uses_source(self):
        event = normalize_hook_payload("claude-code", {
            **LIVE, "hook_event_name": "SessionStart", "source": "startup",
        })
        assert event.name == "context.started"
        assert event.attributes == {"source": "startup"}

    def test_documented_spellings_also_work(self):
        for field in ("how_session_started", "reason"):
            event = normalize_hook_payload("claude-code", {
                **LIVE, "hook_event_name": "SessionStart", field: "resume",
            })
            assert event.name == "context.restored"

    def test_compact_and_fork_restore(self):
        for source in ("compact", "fork"):
            event = normalize_hook_payload("claude-code", {
                **LIVE, "hook_event_name": "SessionStart", "source": source,
            })
            assert event.name == "context.restored"

    def test_prompt_submit_uses_prompt_and_prompt_id(self):
        event = normalize_hook_payload("claude-code", {
            **LIVE, "hook_event_name": "UserPromptSubmit",
            "prompt": "Two questions", "prompt_id": "p-1",
        })
        assert (event.name, event.content, event.ids["prompt_id"]) == (
            "input.received", "Two questions", "p-1",
        )

    def test_tool_failure_carries_error_only(self):
        event = normalize_hook_payload("claude-code", {
            **LIVE, "hook_event_name": "PostToolUseFailure", "prompt_id": "p-1",
            "tool_name": "Bash", "tool_use_id": "toolu_01",
            "tool_input": {"command": "false --token SECRET"},
            "error": "Exit code 1", "duration_ms": 47, "is_interrupt": False,
        })
        assert event.name == "tool.finished"
        assert event.attributes == {"tool_name": "Bash", "ok": False, "error": "Exit code 1"}
        assert "SECRET" not in json.dumps(event.to_dict())

    def test_stop_and_session_end(self):
        stop = normalize_hook_payload("claude-code", {
            **LIVE, "hook_event_name": "Stop", "last_assistant_message": "done",
            "stop_hook_active": False, "prompt_id": "p-1",
        })
        assert (stop.name, stop.content) == ("turn.finished", "done")
        end = normalize_hook_payload("claude-code", {
            **LIVE, "hook_event_name": "SessionEnd", "reason": "other",
        })
        assert (end.name, end.attributes["reason"]) == ("context.closed", "other")

    def test_post_compact_is_its_own_event(self):
        pre = normalize_hook_payload("claude-code", {
            **LIVE, "hook_event_name": "PreCompact", "compaction_trigger": "auto",
        })
        post = normalize_hook_payload("claude-code", {
            **LIVE, "hook_event_name": "PostCompact", "trigger": "manual",
        })
        assert (pre.name, pre.attributes["trigger"]) == ("context.compacting", "auto")
        assert (post.name, post.attributes["trigger"]) == ("context.compacted", "manual")

    def test_subagent_events_keep_agent_id(self):
        start = normalize_hook_payload("claude-code", {
            **LIVE, "hook_event_name": "SubagentStart", "agent_id": "a-1",
            "agent_type": "Explore", "subagent_prompt": "look",
        })
        assert start.name == "context.child_started"
        assert start.ids["agent_id"] == "a-1"
        assert start.content is None

    def test_missing_session_id_is_none_not_string(self):
        event = normalize_hook_payload("claude-code", {"hook_event_name": "Stop"})
        assert event.session_id is None


class TestCodexSpellings:
    def test_codex_fields(self):
        start = normalize_hook_payload("codex", {
            "session_id": "cx", "hook_event_name": "SessionStart", "source": "startup",
            "model": "gpt-5-codex",
        })
        prompt = normalize_hook_payload("codex", {
            "session_id": "cx", "turn_id": "t-1",
            "hook_event_name": "UserPromptSubmit", "prompt": "ship",
        })
        compact = normalize_hook_payload("codex", {
            "session_id": "cx", "hook_event_name": "PreCompact", "trigger": "manual",
        })
        assert start.name == "context.started"
        assert prompt.ids["turn_id"] == "t-1"
        assert compact.attributes["trigger"] == "manual"
        key = event_idempotency_key(prompt)
        assert key == "hook:codex:cx:UserPromptSubmit:t-1"


class TestEnvelopeAndTimeouts:
    def test_envelope_carries_client_facts_never_scope(self):
        event = normalize_hook_payload("claude-code", {
            **LIVE, "hook_event_name": "Stop", "last_assistant_message": "x",
        })
        envelope = build_envelope(
            "claude-code", {**LIVE, "hook_event_name": "Stop"}, event,
            environ={"CLAUDE_PROJECT_DIR": "/home/u/repo", "MIRAGEN_PARENT_SESSION": "codex:p"},
            pid=4242,
        )
        assert envelope["harness"] == "claude-code"
        assert envelope["session_id"] == LIVE["session_id"]
        assert envelope["event"]["name"] == "turn.finished"
        client = envelope["client"]
        assert client["pid"] == 4242
        assert client["cwd"] == "/home/u/repo"
        assert client["project_dir"] == "/home/u/repo"
        assert client["parent_session"] == "codex:p"
        assert client["adapter"] == ADAPTER_VERSION
        assert "scope" not in json.dumps(envelope)

    def test_timeouts_sit_under_harness_budgets(self):
        def ev(name, **extra):
            return normalize_hook_payload("claude-code", {**LIVE, "hook_event_name": name, **extra})

        assert timeout_for(ev("SessionStart", source="startup")) == TIMEOUT_CONTEXT_S
        assert timeout_for(ev("UserPromptSubmit", prompt="x")) == TIMEOUT_CONTEXT_S
        assert timeout_for(ev("Stop")) == TIMEOUT_CAPTURE_S
        assert timeout_for(ev("SessionEnd", reason="other")) == TIMEOUT_SESSION_END_S
        # Claude Code's SessionEnd hooks share 1.5 s; Codex caps at 3 s.
        assert TIMEOUT_SESSION_END_S < 1.5
        assert TIMEOUT_CAPTURE_S < 5

    def test_harness_pid_falls_back_to_start_when_no_marker(self):
        # pid 1 is never our harness; the walk stops and returns the start.
        assert harness_pid("claude-code", start=1) == 1


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener_returning(body: dict):
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return _FakeResponse(json.dumps(body).encode())

    opener.calls = calls
    return opener


class TestForwarding:
    def test_context_answer_becomes_harness_output(self):
        opener = _opener_returning({"session": "claude-code:s", "context": "PACKET"})
        output = run(
            "claude-code", {**LIVE, "hook_event_name": "SessionStart", "source": "startup"},
            daemon_url="http://127.0.0.1:1", token="tok", opener=opener, pid=1,
        )
        assert output == harness_output("claude-code", "SessionStart", "PACKET")
        request, timeout = opener.calls[0]
        assert request.full_url == "http://127.0.0.1:1/sessions/v1/events"
        assert request.get_header("Authorization") == "Bearer tok"
        assert timeout == TIMEOUT_CONTEXT_S
        sent = json.loads(request.data)
        assert sent["event"]["original_event"] == "SessionStart"

    def test_capture_answer_prints_nothing(self):
        opener = _opener_returning({"session": "claude-code:s", "context": None})
        assert run(
            "claude-code", {**LIVE, "hook_event_name": "Stop", "last_assistant_message": "d"},
            daemon_url="http://127.0.0.1:1", token=None, opener=opener, pid=1,
        ) is None

    def test_context_on_a_capture_event_is_ignored(self):
        """A daemon must not be able to inject via an event the harness
        does not honor context on — the adapter only shapes context for
        context-bearing events."""
        opener = _opener_returning({"context": "sneaky"})
        assert run(
            "claude-code", {**LIVE, "hook_event_name": "Stop"},
            daemon_url="http://127.0.0.1:1", token=None, opener=opener, pid=1,
        ) is None

    def test_unreachable_daemon_fails_open(self, capsys):
        def opener(request, timeout):
            raise urllib.error.URLError("connection refused")

        assert run(
            "claude-code", {**LIVE, "hook_event_name": "SessionStart", "source": "startup"},
            daemon_url="http://127.0.0.1:1", token=None, opener=opener, pid=1,
        ) is None
        assert "unreachable" in capsys.readouterr().err

    def test_http_error_fails_open(self, capsys):
        def opener(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 422, "Unprocessable", {}, io.BytesIO(b'{"code":"malformed_event"}')
            )

        assert post_envelope({}, daemon_url="http://x", token=None, timeout=1, opener=opener) is None
        assert "422" in capsys.readouterr().err

    def test_unmapped_event_never_contacts_daemon(self):
        opener = _opener_returning({"context": "x"})
        assert run(
            "claude-code", {**LIVE, "hook_event_name": "Notification"},
            daemon_url="http://127.0.0.1:1", token=None, opener=opener, pid=1,
        ) is None
        assert opener.calls == []

    def test_real_socket_refused_within_budget(self, monkeypatch, capsys):
        """No opener stub: a closed loopback port must fail open, fast, exit 0."""
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {**LIVE, "hook_event_name": "Stop", "last_assistant_message": "d"}
        )))
        assert main(["claude-code", "--daemon", "http://127.0.0.1:9"]) == 0
        out, err = capsys.readouterr()
        assert out == ""
        assert "unreachable" in err

    def test_main_garbage_stdin_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
        assert main(["codex", "--daemon", "http://127.0.0.1:9"]) == 0
        assert capsys.readouterr().out == ""


class TestInstall:
    def test_claude_code_install_merges_and_is_idempotent(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({
            "permissions": {"allow": ["Bash(git log *)"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/my-hook"}]}]},
        }))
        install_hooks("claude-code", daemon_url="http://127.0.0.1:8420",
                      token_file="/etc/miragend/token", settings_path=settings)
        install_hooks("claude-code", daemon_url="http://127.0.0.1:8420",
                      token_file="/etc/miragend/token", settings_path=settings)
        data = json.loads(settings.read_text())
        assert data["permissions"] == {"allow": ["Bash(git log *)"]}
        hooks = data["hooks"]
        assert set(hooks) == {name for name, _ in CLAUDE_CODE_EVENTS}
        stop_groups = hooks["Stop"]
        assert [g for g in stop_groups if "my-hook" in json.dumps(g)]
        owned = [g for g in stop_groups if "miragen-hook" in json.dumps(g)]
        assert len(owned) == 1
        command = owned[0]["hooks"][0]["command"]
        assert command == "miragen-hook claude-code --daemon http://127.0.0.1:8420 --token-file /etc/miragend/token"
        assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] == 3
        assert hooks["SessionStart"][0]["hooks"][0]["timeout"] >= TIMEOUT_CONTEXT_S

    def test_uninstall_leaves_user_entries(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/my-hook"}]}]},
        }))
        install_hooks("claude-code", daemon_url=None, token_file=None, settings_path=settings)
        uninstall_hooks("claude-code", settings_path=settings)
        data = json.loads(settings.read_text())
        assert data == {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/my-hook"}]}]}}

    def test_uninstall_drops_empty_hooks_key(self, tmp_path):
        settings = tmp_path / "settings.json"
        install_hooks("codex", daemon_url="http://d", token_file=None, settings_path=settings)
        remove_hook_entries(settings)
        assert json.loads(settings.read_text()) == {}

    def test_unparsable_settings_are_refused(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text("{nope")
        with pytest.raises(RuntimeError, match="not valid JSON"):
            install_hooks("claude-code", daemon_url=None, token_file=None, settings_path=settings)
        assert settings.read_text() == "{nope"

    def test_install_via_cli(self, tmp_path, capsys):
        settings = tmp_path / "hooks.json"
        assert main(["install", "codex", "--daemon", "http://127.0.0.1:8420",
                     "--settings", str(settings)]) == 0
        assert "installed into" in capsys.readouterr().out
        data = json.loads(settings.read_text())
        assert "SessionStart" in data["hooks"]
        assert "PostToolUseFailure" not in data["hooks"]  # Codex has no such event


def test_adapter_package_imports_only_stdlib():
    """The whole point of the split: a hook invocation must not pay for
    the agent runtime. Assert by module list in a fresh interpreter."""
    import subprocess

    code = (
        "import sys; import miragen_hook.client, miragen_hook.install; "
        "print(sorted(m for m in sys.modules if m.startswith(('miragen', 'pydantic'))))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    loaded = json.loads(out.stdout.replace("'", '"'))
    assert not any(m.startswith("pydantic") for m in loaded)
    assert not any(m == "miragen" or m.startswith("miragen.") for m in loaded)


# ── hosted bridge additions ─────────────────────────────────────────────────


class TestHostedBridgeAdapter:
    def test_envelope_carries_host_remote_and_project_remote(self, tmp_path, monkeypatch):
        import subprocess

        from miragen_hook.client import build_envelope, project_remote
        from miragen_hook.normalize import normalize_hook_payload

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "git@github.com:org/repo.git"], check=True)
        assert project_remote(str(repo)) == "git@github.com:org/repo.git"
        assert project_remote(str(tmp_path)) is None  # no git here
        assert project_remote(None) is None

        payload = {"session_id": "s", "hook_event_name": "SessionStart", "cwd": str(repo)}
        event = normalize_hook_payload("claude-code", payload)
        env = {"CLAUDE_CODE_REMOTE": "true"}
        envelope = build_envelope("claude-code", payload, event, environ=env, pid=7)
        client = envelope["client"]
        assert client["project_remote"] == "git@github.com:org/repo.git"
        assert client["remote"] is True and client["host"]  # this machine's name
        # The daemon's own normalization claims none of that.
        raw = build_envelope("claude-code", payload, event, environ={}, pid=None, host=None,
                             user=None, remote=True, project_remote_url=None)
        assert (raw["client"]["host"], raw["client"]["user"], raw["client"]["project_remote"],
                raw["client"]["pid"]) == (None, None, None, None)
        local = build_envelope("claude-code", payload, event, environ={}, pid=7)
        assert local["client"]["remote"] is False

    def test_plugin_options_supply_url_and_token(self, tmp_path):
        from miragen_hook.client import read_token, resolve_daemon_url

        env = {"CLAUDE_PLUGIN_OPTION_DAEMON_URL": "https://memory.example",
               "CLAUDE_PLUGIN_OPTION_TOKEN": "opt", "MIRAGEND_TOKEN": "env",
               "MIRAGEND_URL": "http://env"}
        assert resolve_daemon_url(None, env) == "https://memory.example"
        assert read_token(None, env) == "opt"
        assert resolve_daemon_url(None, {"MIRAGEND_URL": "http://env"}) == "http://env"
        assert read_token(None, {"MIRAGEND_TOKEN": "env"}) == "env"
        assert resolve_daemon_url(None, {}) == "http://127.0.0.1:8420"
        assert resolve_daemon_url("http://x", env) == "http://x"
        f = tmp_path / "t"
        f.write_text("filetoken\n")
        assert read_token(str(f), env) == "filetoken"

    def test_http_install_for_a_repository(self, tmp_path):
        from miragen_hook.install import CLAUDE_CODE_EVENTS, install_hooks, uninstall_hooks

        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "echo user-owned"}]}]}}))
        install_hooks("claude-code", daemon_url="https://memory.example/", token_file=None,
                      settings_path=settings, http=True)
        data = json.loads(settings.read_text())
        for event, timeout in CLAUDE_CODE_EVENTS:
            owned = [g for g in data["hooks"][event] if g["hooks"][0].get("type") == "http"]
            assert len(owned) == 1
            entry = owned[0]["hooks"][0]
            assert entry == {
                "type": "http",
                "url": f"https://memory.example/sessions/v1/hooks/claude-code",
                "headers": {"Authorization": "Bearer $MIRAGEND_TOKEN"},
                "allowedEnvVars": ["MIRAGEND_TOKEN"],
                "timeout": timeout,
            }
        assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo user-owned"
        # Idempotent, then removable, user entry intact.
        install_hooks("claude-code", daemon_url="https://memory.example", token_file=None,
                      settings_path=settings, http=True)
        assert len(json.loads(settings.read_text())["hooks"]["Stop"]) == 2
        uninstall_hooks("claude-code", settings_path=settings)
        assert json.loads(settings.read_text())["hooks"] == {"Stop": [{"hooks": [
            {"type": "command", "command": "echo user-owned"}]}]}

    def test_http_install_refuses_codex_and_missing_url(self, tmp_path):
        from miragen_hook.install import install_hooks

        with pytest.raises(RuntimeError, match="Claude Code"):
            install_hooks("codex", daemon_url="http://x", token_file=None,
                          settings_path=tmp_path / "h.json", http=True)
        with pytest.raises(RuntimeError, match="daemon URL"):
            install_hooks("claude-code", daemon_url=None, token_file=None,
                          settings_path=tmp_path / "s.json", http=True)


class TestCaptureKeys:
    def test_content_free_events_get_distinct_stable_keys(self):
        """Two Stops without a last message but with different attributes
        must not share a key (Loimi 409s a reused key with new content);
        the same event twice must (idempotent redelivery)."""
        from miragen_hook.normalize import captured_content, event_idempotency_key

        base = {"session_id": "s", "hook_event_name": "Stop"}
        a = normalize_hook_payload("claude-code", {**base, "stop_reason": "end_turn"})
        b = normalize_hook_payload("claude-code", {**base, "stop_reason": "max_tokens"})
        again = normalize_hook_payload("claude-code", {**base, "stop_reason": "end_turn"})
        assert a.content is None
        assert event_idempotency_key(a) != event_idempotency_key(b)
        assert event_idempotency_key(a) == event_idempotency_key(again)
        assert captured_content(a) == captured_content(again)
        assert "end_turn" in captured_content(a)
