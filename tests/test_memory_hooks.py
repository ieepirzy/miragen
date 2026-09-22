"""The native hook bridge (§18.7): normalization against the documented
Claude Code / Codex payload shapes, capture idempotency + recursion guard,
context answers on session start, the CLI bridge's fail-open contract,
Codex hooks.json installation, and the honesty gate's support detection.

Payload fixtures mirror the official hook references checked 2026-09-13
(code.claude.com/docs/en/hooks, developers.openai.com/codex/hooks).
"""

import json
import sys

import pytest
from click.testing import CliRunner
from unittest.mock import AsyncMock, MagicMock

import miragen.app  # noqa: F401
app_module = sys.modules["miragen.app"]
from miragen.memory.harness_hooks import (
    build_sdk_hook_callables,
    event_idempotency_key,
    executor_hook_support,
    handle_hook_event,
    install_codex_hooks,
    normalize_hook_payload,
)
from miragen.memory.lifecycle import MemoryPacket

CLAUDE_COMMON = {
    "session_id": "sess-1",
    "transcript_path": "/tmp/t.jsonl",
    "cwd": "/w",
    "permission_mode": "default",
}


# ── normalization: Claude Code ───────────────────────────────────────────────


class TestNormalizeClaudeCode:
    def test_session_start_startup_opens_context(self):
        event = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "SessionStart", "reason": "startup",
        })
        assert event.name == "context.started"
        assert event.original_event == "SessionStart"
        assert event.session_id == "sess-1"

    def test_session_start_resume_restores_context(self):
        event = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "SessionStart", "reason": "resume",
        })
        assert event.name == "context.restored"

    def test_session_start_after_compaction_is_restored(self):
        event = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "SessionStart", "reason": "compact",
        })
        assert event.name == "context.restored"
        assert event.attributes["source"] == "compact"

    def test_prompt_submit_carries_input(self):
        event = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "UserPromptSubmit",
            "prompt_id": "p-1", "user_input": "fix the bug",
        })
        assert event.name == "input.received"
        assert event.content == "fix the bug"
        assert event.ids["prompt_id"] == "p-1"

    def test_successful_tool_use_is_not_captured(self):
        event = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "PostToolUse",
            "tool_name": "Bash", "tool_use_id": "toolu_01",
            "tool_input": {"command": "npm test"},
            "tool_result": {"content": "ok", "type": "text"},
        })
        assert event is None

    def test_tool_failure_is_captured_without_raw_command(self):
        event = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash", "tool_use_id": "toolu_02",
            "tool_input": {"command": "secret-command --token abc"},
            "error": "exit 1",
        })
        assert event.name == "tool.finished"
        assert event.attributes == {"tool_name": "Bash", "ok": False, "error": "exit 1"}
        # §18.2: raw command text stays out of what we export/capture.
        assert "secret-command" not in json.dumps(event.attributes)
        assert event.content is None

    def test_memory_tools_never_recurse(self):
        for tool in ("memory_remember", "mcp__miragen-memory__memory_checkpoint"):
            event = normalize_hook_payload("claude-code", {
                **CLAUDE_COMMON, "hook_event_name": "PostToolUseFailure",
                "tool_name": tool, "error": "x",
            })
            assert event is None

    def test_stop_and_subagent_and_lifecycle_events(self):
        stop = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "Stop",
            "last_assistant_message": "done",
        })
        assert (stop.name, stop.content) == ("turn.finished", "done")

        compacting = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "PreCompact", "reason": "auto",
        })
        assert compacting.name == "context.compacting"

        child = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "SubagentStop",
            "agent_id": "a-1", "agent_type": "Explore",
            "last_assistant_message": "found it",
        })
        assert child.name == "context.child_finished"
        assert child.ids["agent_id"] == "a-1"

        closed = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "SessionEnd", "reason": "logout",
        })
        assert closed.name == "context.closed"

    def test_unmapped_events_are_ignored(self):
        for name in ("Notification", "MessageDisplay", "PreToolUse", "FileChanged"):
            assert normalize_hook_payload("claude-code", {
                **CLAUDE_COMMON, "hook_event_name": name,
            }) is None


# ── normalization: Codex field spellings ─────────────────────────────────────


class TestNormalizeCodex:
    def test_session_start_uses_source_field(self):
        event = normalize_hook_payload("codex", {
            "session_id": "cx-1", "hook_event_name": "SessionStart",
            "source": "resume", "model": "gpt-5-codex",
        })
        assert event.name == "context.restored"
        assert event.harness == "codex"

    def test_prompt_submit_uses_prompt_field(self):
        event = normalize_hook_payload("codex", {
            "session_id": "cx-1", "turn_id": "t-9",
            "hook_event_name": "UserPromptSubmit", "prompt": "ship it",
        })
        assert event.content == "ship it"
        assert event.ids["turn_id"] == "t-9"

    def test_precompact_uses_trigger_field(self):
        event = normalize_hook_payload("codex", {
            "session_id": "cx-1", "hook_event_name": "PreCompact", "trigger": "manual",
        })
        assert event.attributes["trigger"] == "manual"

    def test_unknown_harness_is_a_loud_error(self):
        with pytest.raises(ValueError, match="unknown harness"):
            normalize_hook_payload("hermes", {"hook_event_name": "Stop"})


# ── idempotency keys ─────────────────────────────────────────────────────────


class TestIdempotency:
    def test_same_occurrence_same_key(self):
        payload = {**CLAUDE_COMMON, "hook_event_name": "UserPromptSubmit",
                   "prompt_id": "p-1", "user_input": "x"}
        a = event_idempotency_key(normalize_hook_payload("claude-code", payload))
        b = event_idempotency_key(normalize_hook_payload("claude-code", payload))
        assert a == b

    def test_distinct_prompts_get_distinct_keys(self):
        base = {**CLAUDE_COMMON, "hook_event_name": "UserPromptSubmit", "user_input": "x"}
        a = event_idempotency_key(
            normalize_hook_payload("claude-code", {**base, "prompt_id": "p-1"}))
        b = event_idempotency_key(
            normalize_hook_payload("claude-code", {**base, "prompt_id": "p-2"}))
        assert a != b

    def test_content_hash_fallback_when_no_ids(self):
        base = {**CLAUDE_COMMON, "hook_event_name": "Stop"}
        a = event_idempotency_key(normalize_hook_payload(
            "claude-code", {**base, "last_assistant_message": "one"}))
        b = event_idempotency_key(normalize_hook_payload(
            "claude-code", {**base, "last_assistant_message": "two"}))
        assert a != b


# ── event handling ───────────────────────────────────────────────────────────


def _fake_lifecycle(packet_text="GUIDE+STATE"):
    lifecycle = MagicMock()
    lifecycle.prepare_context = AsyncMock(return_value=MemoryPacket(text=packet_text))
    lifecycle.capture_harness_event = AsyncMock(return_value={"status": "captured"})
    return lifecycle


class TestHandleEvent:
    async def test_context_start_answers_the_packet(self):
        lifecycle = _fake_lifecycle()
        event = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "SessionStart", "reason": "startup",
        })
        output = await handle_hook_event(lifecycle, event, instance="ops")
        assert output["hookSpecificOutput"]["additionalContext"] == "GUIDE+STATE"
        assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        lifecycle.prepare_context.assert_awaited_once()
        lifecycle.capture_harness_event.assert_not_awaited()

    async def test_captured_events_write_and_stay_silent(self):
        lifecycle = _fake_lifecycle()
        event = normalize_hook_payload("claude-code", {
            **CLAUDE_COMMON, "hook_event_name": "Stop", "last_assistant_message": "done",
        })
        output = await handle_hook_event(lifecycle, event, instance=None)
        assert output is None
        lifecycle.capture_harness_event.assert_awaited_once()


# ── SDK callables (claude-code in-process path) ──────────────────────────────


class TestSdkCallables:
    async def test_callables_capture_and_fail_open(self):
        lifecycle = _fake_lifecycle()
        callables = build_sdk_hook_callables(lifecycle, instance=None)
        assert "SessionStart" not in callables  # boundary already injects

        result = await callables["Stop"]({**CLAUDE_COMMON,
                                          "last_assistant_message": "done"})
        assert result == {}
        lifecycle.capture_harness_event.assert_awaited_once()

        lifecycle.capture_harness_event.side_effect = RuntimeError("boom")
        result = await callables["UserPromptSubmit"]({**CLAUDE_COMMON,
                                                      "user_input": "x"})
        assert result == {}  # fail-open: the harness never sees the failure


# ── Codex hooks.json installation ────────────────────────────────────────────


class TestCodexInstall:
    def test_installs_all_events_with_bounded_timeouts(self, tmp_path):
        path = tmp_path / "hooks.json"
        install_codex_hooks(path)
        hooks = json.loads(path.read_text())["hooks"]
        assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PreCompact",
                              "PostCompact", "Stop", "SubagentStop", "SessionEnd"}
        session_end = hooks["SessionEnd"][0]["hooks"][0]
        assert session_end["command"] == "miragen memory-hook codex"
        assert session_end["timeout"] == 3  # the documented SessionEnd cap
        assert hooks["Stop"][0]["hooks"][0]["timeout"] == 10

    def test_reinstall_is_idempotent_and_preserves_user_hooks(self, tmp_path):
        path = tmp_path / "hooks.json"
        path.write_text(json.dumps({
            "hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": "/usr/local/bin/my-hook"}]},
            ]},
            "unrelated_key": {"kept": True},
        }))
        install_codex_hooks(path)
        install_codex_hooks(path)
        data = json.loads(path.read_text())
        assert data["unrelated_key"] == {"kept": True}
        stop_groups = data["hooks"]["Stop"]
        user = [g for g in stop_groups if "my-hook" in json.dumps(g)]
        owned = [g for g in stop_groups if "miragen memory-hook" in json.dumps(g)]
        assert len(user) == 1 and len(owned) == 1

    def test_unparsable_hooks_json_is_refused_not_clobbered(self, tmp_path):
        path = tmp_path / "hooks.json"
        path.write_text("{not json")
        with pytest.raises(RuntimeError, match="not valid JSON"):
            install_codex_hooks(path)
        assert path.read_text() == "{not json"


# ── support detection (the honesty gate's input) ─────────────────────────────


class TestSupportDetection:
    def test_codex_is_supported_via_bridge(self):
        assert executor_hook_support("codex")["native_hooks"] is True

    def test_claude_code_reflects_installed_sdk(self):
        # The dev environment has no claude-agent-sdk: the report must say
        # so rather than assume from documentation (§18.7).
        support = executor_hook_support("claude-code")
        assert support["native_hooks"] is False
        assert "not installed" in support["detail"]

    def test_unverified_kinds_say_unverified_not_unsupported(self):
        support = executor_hook_support("kimi-code")
        assert support["native_hooks"] is False
        assert "unverified" in support["detail"]


# ── the CLI bridge ───────────────────────────────────────────────────────────


class TestCliBridge:
    def _write_profile(self, tmp_path, with_memory=True):
        profile = tmp_path / "agent.yaml"
        memory = """
memory:
  scopes:
    read: [profile:a]
    propose: [profile:a]
    default_write: profile:a
""" if with_memory else ""
        profile.write_text(f"""name: a
mode: interactive
triggers:
  - type: http
{memory}spec:
  model: "test:model"
  instructions: "t"
""")
        return profile

    def test_session_start_emits_additional_context(self, tmp_path, monkeypatch):
        from miragen.cli import cli
        from miragen.memory import lifecycle as lifecycle_module

        profile = self._write_profile(tmp_path)
        monkeypatch.setenv("AGENT_PROFILE", str(profile))
        monkeypatch.setattr(
            lifecycle_module.MemoryLifecycle, "prepare_context",
            AsyncMock(return_value=MemoryPacket(text="THE PACKET")),
        )
        result = CliRunner().invoke(cli, ["memory-hook", "codex"], input=json.dumps({
            "session_id": "cx-1", "hook_event_name": "SessionStart", "source": "startup",
        }))
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["hookSpecificOutput"]["additionalContext"] == "THE PACKET"

    def test_capture_event_is_silent_on_stdout(self, tmp_path, monkeypatch):
        from miragen.cli import cli
        from miragen.memory import lifecycle as lifecycle_module

        profile = self._write_profile(tmp_path)
        monkeypatch.setenv("AGENT_PROFILE", str(profile))
        captured = AsyncMock(return_value={"status": "captured"})
        monkeypatch.setattr(
            lifecycle_module.MemoryLifecycle, "capture_harness_event", captured
        )
        result = CliRunner().invoke(cli, ["memory-hook", "codex"], input=json.dumps({
            "session_id": "cx-1", "hook_event_name": "UserPromptSubmit", "prompt": "go",
        }))
        assert result.exit_code == 0
        assert result.output == ""
        captured.assert_awaited_once()

    def test_no_memory_block_is_a_noop(self, tmp_path, monkeypatch):
        from miragen.cli import cli

        profile = self._write_profile(tmp_path, with_memory=False)
        monkeypatch.setenv("AGENT_PROFILE", str(profile))
        result = CliRunner().invoke(cli, ["memory-hook", "claude-code"], input=json.dumps({
            **CLAUDE_COMMON, "hook_event_name": "Stop",
        }))
        assert result.exit_code == 0
        assert result.output == ""

    def test_fail_open_on_any_failure(self, tmp_path, monkeypatch):
        """§18.7: a broken bridge must never block the agent's work — bad
        stdin, missing profile, broken store all exit 0."""
        from miragen.cli import cli

        monkeypatch.setenv("AGENT_PROFILE", str(tmp_path / "missing.yaml"))
        result = CliRunner().invoke(cli, ["memory-hook", "codex"], input="{not json")
        assert result.exit_code == 0

        profile = self._write_profile(tmp_path)
        monkeypatch.setenv("AGENT_PROFILE", str(profile))
        # Memory env vars unset -> MemoryUnavailable inside; still exit 0.
        monkeypatch.delenv("LOIMI_MEMORY_URL", raising=False)
        result = CliRunner().invoke(cli, ["memory-hook", "codex"], input=json.dumps({
            "session_id": "cx", "hook_event_name": "UserPromptSubmit", "prompt": "x",
        }))
        assert result.exit_code == 0
        assert result.output == ""  # degradation goes to stderr/log, never stdout


# ── adapter integration ──────────────────────────────────────────────────────


class TestAdapterIntegration:
    def _codex(self, tmp_path, with_memory):
        from miragen.executor.codex import CodexExecutor
        from miragen.models import AgentProfile

        profile = AgentProfile.model_validate({
            "name": "worker", "mode": "interactive", "triggers": [{"type": "http"}],
            "executor": {"executor": "codex", "instructions": "work",
                         "codex_home": str(tmp_path / "codex-home"),
                         "workspace_root": str(tmp_path / "ws")},
        })
        executor = CodexExecutor(profile, runs_root=tmp_path / "runs")
        if with_memory:
            executor.set_memory(_fake_lifecycle())
        return executor

    def test_codex_prepare_installs_hooks_when_memory_set(self, tmp_path):
        executor = self._codex(tmp_path, with_memory=True)
        executor.prepare()
        hooks = json.loads((tmp_path / "codex-home" / "hooks.json").read_text())
        assert "SessionStart" in hooks["hooks"]
        report = executor.memory_hook_capabilities()
        assert report["native_hooks"] is True
        assert report["memory_enabled"] is True

    def test_codex_prepare_without_memory_installs_nothing(self, tmp_path):
        executor = self._codex(tmp_path, with_memory=False)
        executor.prepare()
        assert not (tmp_path / "codex-home" / "hooks.json").exists()
        assert executor.memory_hook_capabilities()["memory_enabled"] is False

    def test_claude_code_options_omit_hooks_without_sdk(self, tmp_path):
        """No claude-agent-sdk in this environment: the adapter must not
        fabricate a hooks option it cannot honor."""
        from miragen.executor.claude_code import ClaudeCodeExecutor
        from miragen.models import AgentProfile

        profile = AgentProfile.model_validate({
            "name": "worker", "mode": "interactive", "triggers": [{"type": "http"}],
            "executor": {"executor": "claude-code", "instructions": "work",
                         "workspace_root": str(tmp_path / "ws")},
        })
        executor = ClaudeCodeExecutor(profile, runs_root=tmp_path / "runs")
        executor.set_memory(_fake_lifecycle())
        options = executor._options(tmp_path / "ws", None)
        assert "hooks" not in options
        assert executor.memory_hook_capabilities()["native_hooks"] is False


class TestRepeatedEventsInOnePrompt:
    """Claude Code stamps one prompt_id on every hook in a prompt. Events
    that repeat within it must not reuse a key with different content —
    Loimi answers that with 409 and the capture is lost (2026-09-22)."""

    def test_second_stop_in_same_prompt_gets_its_own_key(self):
        base = {**CLAUDE_COMMON, "hook_event_name": "Stop", "prompt_id": "p-1"}
        a = event_idempotency_key(normalize_hook_payload(
            "claude-code", {**base, "last_assistant_message": "first answer"}))
        b = event_idempotency_key(normalize_hook_payload(
            "claude-code", {**base, "last_assistant_message": "answer after the Stop hook blocked"}))
        assert a != b

    def test_subagents_in_same_prompt_get_their_own_keys(self):
        base = {**CLAUDE_COMMON, "hook_event_name": "SubagentStop", "prompt_id": "p-1",
                "last_assistant_message": "done"}
        a = event_idempotency_key(normalize_hook_payload("claude-code", {**base, "agent_id": "ag-1"}))
        b = event_idempotency_key(normalize_hook_payload("claude-code", {**base, "agent_id": "ag-2"}))
        assert a != b

    def test_redelivered_stop_still_dedupes(self):
        payload = {**CLAUDE_COMMON, "hook_event_name": "Stop", "prompt_id": "p-1",
                   "last_assistant_message": "same answer"}
        assert event_idempotency_key(normalize_hook_payload("claude-code", payload)) == \
            event_idempotency_key(normalize_hook_payload("claude-code", dict(payload)))
