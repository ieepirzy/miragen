"""The Claude Code harness against a scripted SDK client: the options it
launches `claude` with, the tool boundary, session continuity, and the
harness-neutral swap marker. (Verified live against the real CLI; see the PR.)"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

sdk = pytest.importorskip("claude_agent_sdk")

from miragen.harness import build_model_harness, parse_harness_model  # noqa: E402
from miragen.harness.base import HarnessTurn  # noqa: E402
from miragen.harness.claude_code import (  # noqa: E402
    WRAPPER_FILE, ClaudeCodeHarness, ClaudeCodeHarnessError, ClaudeCodeSettings,
)
from miragen.harness.gateway import ToolGateway  # noqa: E402
from miragen.harness.served import ServedLedger  # noqa: E402
from miragen.models import AgentProfile  # noqa: E402


def profile(capabilities=None, **extra) -> AgentProfile:
    return AgentProfile.model_validate({
        "name": "mira", "mode": "interactive", "triggers": [{"type": "http"}],
        "inject_timestamp": False,
        "spec": {"model": "claude-code:sonnet", "instructions": "You are Mira.",
                 **({"capabilities": capabilities} if capabilities else {})},
        **extra,
    })


class FakeClient:
    """Stands in for ClaudeSDKClient: remembers its options and prompts,
    writes the session file the CLI would, and answers from a script."""

    instances: list["FakeClient"] = []

    def __init__(self, options, home: Path, script):
        self.options, self.home, self.script = options, home, script
        self.prompts: list[str] = []
        self.connected = self.disconnected = False
        FakeClient.instances.append(self)

    @property
    def session_id(self) -> str:
        return self.options.resume or self.options.session_id

    async def connect(self):
        self.connected = True
        path = self.home / "projects" / "-work-dir" / f"{self.session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        messages = self.script(self, self.prompts[-1])
        if hasattr(messages, "__aiter__"):
            async for message in messages:
                yield message
        else:
            for message in messages:
                yield message

    async def interrupt(self):
        pass

    async def disconnect(self):
        self.disconnected = True


def text(t: str, usage=None):
    return sdk.AssistantMessage(content=[sdk.TextBlock(text=t)], model="sonnet", usage=usage)


def result(session_id: str, *, is_error=False, subtype="success", result_text="ok"):
    return sdk.ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
                             num_turns=1, session_id=session_id, result=result_text,
                             usage={"input_tokens": 7, "output_tokens": 3,
                                    "cache_read_input_tokens": 100})


def echo(client, prompt):
    yield text(f"echo: {prompt}")
    yield result(client.session_id)


class Env:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.home = tmp_path / "claude-home"
        self.served = ServedLedger(tmp_path / "runs" / "harness" / "served.json")
        self.script = echo
        self.harnesses: list[ClaudeCodeHarness] = []
        FakeClient.instances = []

    def harness(self, prof=None, **kw) -> ClaudeCodeHarness:
        prof = prof or profile()
        settings = ClaudeCodeSettings(claude_home=self.home, workdirs=self.tmp_path / "work",
                                      gateway_url="http://127.0.0.1:9/mcp/gateway/",
                                      cli_path="/usr/bin/env", **kw)
        h = ClaudeCodeHarness(prof, ToolGateway(
            prof, native_capabilities=ClaudeCodeHarness.native_capabilities), settings,
            served=self.served,
                              client_factory=lambda o: FakeClient(o, self.home,
                                                                  lambda c, p: self.script(c, p)))
        self.harnesses.append(h)
        return h

    def state(self) -> dict:
        return json.loads((self.home / "miragen-instances.json").read_text())


@pytest.fixture
async def env(tmp_path):
    e = Env(tmp_path)
    yield e
    for h in e.harnesses:
        await h.aclose()


def turn(prompt, instance="chat", use_history=True, run_id="r1", **kw) -> HarnessTurn:
    return HarnessTurn(prompt=prompt, instance=instance, use_history=use_history, run_id=run_id, **kw)


def test_claude_code_prefix_builds_the_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRAGEN_CLAUDE_HOME", str(tmp_path / "claude-home"))
    assert parse_harness_model("claude-code:sonnet") == ("claude-code", "sonnet")
    harness, gateway = build_model_harness(profile(), runs_root=tmp_path)
    assert harness.name == "claude-code" and harness.gateway is gateway
    assert harness.model == "sonnet"
    assert harness.served.path == tmp_path / "harness" / "served.json"


async def test_turns_reuse_one_process_and_one_session(env):
    h = env.harness()
    first = await h.run(turn("hello", extra_instructions="PACKET"))
    second = await h.run(turn("again", run_id="r2"))
    assert len(FakeClient.instances) == 1
    client = FakeClient.instances[0]
    # The memory packet rides the prompt; the system prompt stays stable.
    assert client.prompts[0] == '<context source="miragen">\nPACKET\n</context>\n\nhello'
    assert client.options.system_prompt == "You are Mira."
    assert first.output == "echo: " + client.prompts[0]
    assert second.output == "echo: again"
    assert first.usage.input_tokens == 7 and first.usage.cached_input_tokens == 100
    assert env.state()["chat"]["session_id"] == client.options.session_id
    assert env.state()["chat"]["turns"] == 2


async def test_launch_is_hermetic_and_subscription_only(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "metered")
    h = env.harness()
    await h.run(turn("hi"))
    o = FakeClient.instances[0].options
    assert o.tools == [] and o.setting_sources == [] and o.skills == []
    assert o.strict_mcp_config and list(o.mcp_servers) == ["gw"]
    assert o.mcp_servers["gw"]["headers"] == {"Authorization": "Bearer ${MIRAGEN_GATEWAY_TOKEN}"}
    assert o.permission_mode == "default" and o.can_use_tool is not None
    assert o.verbatim_prompts and o.model == "sonnet"
    assert o.env["CLAUDE_CONFIG_DIR"] == str(env.home)
    assert o.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sub-token"
    assert o.env["ENABLE_TOOL_SEARCH"] == "false"
    keep = o.env["MIRAGEN_ENV_KEEP"].split()
    assert "ANTHROPIC_API_KEY" not in keep and "MIRAGEN_GATEWAY_TOKEN" in keep
    assert o.cli_path == str(env.home / WRAPPER_FILE)
    # The wrapper (here exec'ing /usr/bin/env) passes only the allowlist.
    out = subprocess.run([o.cli_path], env={**o.env, "PATH": "/usr/bin:/bin",
                                            "ANTHROPIC_API_KEY": "metered",
                                            "UPSTREAM_SECRET": "x",
                                            "CLAUDE_AGENT_SDK_VERSION": "1"},
                         capture_output=True, text=True, check=True).stdout
    names = {line.split("=", 1)[0] for line in out.splitlines()}
    assert "ANTHROPIC_API_KEY" not in names and "UPSTREAM_SECRET" not in names
    assert {"CLAUDE_CODE_OAUTH_TOKEN", "MIRAGEN_GATEWAY_TOKEN", "CLAUDE_AGENT_SDK_VERSION"} <= names


async def test_only_gateway_tools_and_enabled_web_tools_are_allowed(env):
    h = env.harness(profile(capabilities=["WebFetch"]))
    assert h.builtins == {"WebFetch"}
    await h.run(turn("hi"))
    assert FakeClient.instances[0].options.tools == ["WebFetch"]
    allow = lambda name: h._permission(name, {}, None)  # noqa: E731
    assert (await allow("mcp__gw__speak")).behavior == "allow"
    assert (await allow("WebFetch")).behavior == "allow"
    for refused in ("Bash", "Read", "WebSearch", "mcp__gw__", "mcp__other__speak", "gw__speak"):
        assert (await allow(refused)).behavior == "deny", refused


async def test_gated_tools_raise_the_mcp_tool_timeout_and_turn_timeout(env):
    h = env.harness(profile(approval_required=["speak"], approval_timeout_s=900),
                    turn_timeout_s=600)
    assert h.tool_timeout_s == 1020 and h.settings.turn_timeout_s == 1080
    await h.run(turn("hi"))
    assert FakeClient.instances[0].options.env["MCP_TOOL_TIMEOUT"] == "1020000"


async def test_restart_resumes_the_session(env):
    h = env.harness()
    await h.run(turn("one"))
    sid = FakeClient.instances[0].options.session_id
    await h.aclose()
    h2 = env.harness()
    await h2.run(turn("two", run_id="r2"))
    o = FakeClient.instances[1].options
    assert o.resume == sid and o.session_id is None
    assert h2.session_info("chat")["fresh"] is False


async def test_a_lost_session_file_starts_a_new_session_and_reports_fresh(env):
    h = env.harness()
    await h.run(turn("one"))
    await h.aclose()
    for f in (env.home / "projects").glob("*/*.jsonl"):
        f.unlink()
    h2 = env.harness()
    await h2.run(turn("two", run_id="r2"))
    o = FakeClient.instances[1].options
    assert o.resume is None and o.session_id != FakeClient.instances[0].options.session_id


async def test_error_results_fail_the_turn(env):
    env.script = lambda c, p: [result(c.session_id, is_error=True, subtype="error_during_execution",
                                      result_text="Claude usage limit reached")]
    h = env.harness()
    with pytest.raises(ClaudeCodeHarnessError, match="usage limit"):
        await h.run(turn("hi"))


async def test_ephemeral_runs_leave_nothing_behind(env):
    h = env.harness()
    await h.run(turn("hi", instance=None, use_history=False, run_id="e1"))
    assert FakeClient.instances[0].disconnected
    assert not list((env.home / "projects").glob("*/*.jsonl"))
    assert not (env.tmp_path / "work" / "run-e1").exists()
    assert h.session_info("run-e1") is None


async def test_a_harness_swap_reports_fresh_and_continues_plane_numbering(env):
    env.served.mark("chat", "grok-build", 3)  # grok served this instance up to s3
    h = env.harness()
    info = h.session_info("chat")
    assert info["fresh"] is True and info["seq"] == 4
    await h.run(turn("hi"))
    assert env.served.get("chat")["harness"] == "claude-code"
    assert h.session_info("chat")["seq"] == 4 and h.session_info("chat")["fresh"] is False
    # Grok serves a while, then back here: fresh again, and a new plane session.
    env.served.mark("chat", "grok-build", 6)
    assert h.session_info("chat")["fresh"] is True
    await h.run(turn("back", run_id="r2"))
    assert h.session_info("chat")["seq"] == 7


def test_grok_reports_fresh_after_another_harness_served(tmp_path):
    pytest.importorskip("grok_build_client")
    from miragen.harness.grok import GrokHarness, GrokSettings

    served = ServedLedger(tmp_path / "served.json")
    prof = AgentProfile.model_validate({
        "name": "mira", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "grok-build:grok-4.7", "instructions": "x"}})
    g = GrokHarness(prof, ToolGateway(prof), GrokSettings(
        grok_home=tmp_path / "grok-home", workdirs=tmp_path / "work", gateway_url="http://x/"),
        served=served)
    (tmp_path / "grok-home" / "miragen-instances.json").write_text(
        json.dumps({"chat": {"session_id": "s", "seq": 2}}))
    assert g.session_info("chat")["fresh"] is False  # no marker: not a switch
    served.mark("chat", "grok-build", 2)
    assert g.session_info("chat")["fresh"] is False
    served.mark("chat", "claude-code", 3)
    assert g.session_info("chat")["fresh"] is True


async def test_rotate_starts_a_new_session_and_forget_removes_everything(env):
    events = []

    async def lifecycle(instance, event, info):
        events.append((instance, event))

    h = env.harness()
    h.on_lifecycle = lifecycle
    await h.run(turn("one"))
    first = FakeClient.instances[0].options.session_id
    info = await h.rotate("chat")
    assert info["fresh"] is True and info["seq"] == 2 and events == [("chat", "closed")]
    await h.run(turn("two", run_id="r2"))
    second = FakeClient.instances[1].options.session_id
    assert second != first and FakeClient.instances[1].options.resume is None
    assert h.session_info("chat")["fresh"] is False
    removed = await h.forget("chat")
    assert removed[:2] == ["process", "session_mapping"] and "workdir" in removed
    assert f"claude_session:{first}" in removed and f"claude_session:{second}" in removed
    assert not list((env.home / "projects").glob("*/*.jsonl"))
    assert h.session_info("chat") is None and env.served.get("chat") is None
    with pytest.raises(KeyError):
        await h.rotate("chat")



def test_first_swap_after_deploy_is_fresh_from_grok_state(tmp_path, monkeypatch):
    """The real deploy: Grok is rate limited, so the ledger never saw a Grok
    turn; the only record is Grok's own instance map."""
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    (grok_home / "miragen-instances.json").write_text(json.dumps({"chat": {"session_id": "g", "seq": 5}}))
    monkeypatch.setenv("MIRAGEN_GROK_HOME", str(grok_home))
    monkeypatch.setenv("MIRAGEN_CLAUDE_HOME", str(tmp_path / "claude-home"))
    harness, _gateway = build_model_harness(profile(), runs_root=tmp_path / "runs")
    assert harness.session_info("chat") == {**harness.session_info("chat"), "fresh": True, "seq": 6}
    assert harness.session_info("heartbeat") is None
    # Seeding happens once: a later ledger is never overwritten.
    harness.served.mark("chat", "claude-code", 6)
    build_model_harness(profile(), runs_root=tmp_path / "runs")
    assert harness.served.get("chat")["harness"] == "claude-code"


async def test_context_is_the_last_calls_prompt_and_compaction_knobs_reach_the_child(env):
    def script(client, prompt):
        yield text("step", usage={"input_tokens": 5, "cache_read_input_tokens": 1000})
        yield text("done", usage={"input_tokens": 9, "cache_read_input_tokens": 1200,
                                  "cache_creation_input_tokens": 30})
        yield result(client.session_id)  # usage summed over both calls: not the context

    env.script = script
    h = env.harness(auto_compact_window=100000)
    await h.run(turn("hi"))
    assert h.session_info("chat")["context_tokens"] == 1239
    o = FakeClient.instances[0].options
    assert o.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "100000"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in o.env["MIRAGEN_ENV_KEEP"].split()


async def test_a_cancelled_turn_is_interrupted_and_the_process_replaced(env):
    import asyncio

    gate = asyncio.Event()

    def slow(client, prompt):
        async def gen():
            await gate.wait()
            yield text("late")
            yield result(client.session_id)
        return gen()

    class Stream:
        def __init__(self, agen):
            self.agen = agen

        def __aiter__(self):
            return self.agen

    env.script = lambda c, p: Stream(slow(c, p)) if p == "slow" else echo(c, p)
    h = env.harness()
    task = asyncio.create_task(h.run(turn("slow")))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert FakeClient.instances[0].disconnected  # not left holding the rest of that turn
    res = await h.run(turn("next", run_id="r2"))
    assert res.output == "echo: next" and len(FakeClient.instances) == 2


async def test_seq_is_the_same_before_and_after_the_first_turn_back(env):
    h = env.harness()
    await h.run(turn("one"))
    env.served.mark("chat", "grok-build", 6)
    before = h.session_info("chat")["seq"]
    await h.run(turn("two", run_id="r2"))
    assert before == h.session_info("chat")["seq"] == 7


async def test_a_missing_session_file_reads_fresh_before_the_turn(env):
    # The client asks for session info before it submits a turn: a mapped
    # session whose file is gone must already read `fresh` then, not only
    # once the turn has gone to a new, empty session.
    h = env.harness()
    await h.run(turn("one"))
    await h.aclose()
    for f in (env.home / "projects").glob("*/*.jsonl"):
        f.unlink()
    h2 = env.harness()
    assert h2.session_info("chat")["fresh"] is True
    await h2.run(turn("two", run_id="r2"))
    # The loss is still visible after that first turn on the new session...
    assert h2.session_info("chat")["fresh"] is True
    await h2.run(turn("three", run_id="r3"))
    # ...and consumed by the next one.
    assert h2.session_info("chat")["fresh"] is False


async def test_ephemeral_compaction_persists_nothing(env):
    events = []

    async def lifecycle(instance, event, info):
        events.append((instance, event))

    def compacting(client, prompt):
        async def gen():
            for matcher in client.options.hooks["PreCompact"]:
                for hook in matcher.hooks:
                    await hook({"hook_event_name": "PreCompact", "trigger": "auto"}, None, {})
            yield text("done")
            yield result(client.session_id)
        return gen()

    env.script = compacting
    h = env.harness()
    h.on_lifecycle = lifecycle
    await h.run(turn("hi", instance=None, use_history=False, run_id="e1"))
    assert h.session_info("run-e1") is None
    assert not (env.home / "miragen-instances.json").exists() or "run-e1" not in env.state()
    assert events == []
    # A persistent instance still records its compactions.
    await h.run(turn("hi"))
    assert env.state()["chat"]["compactions"] == 1 and events == [("chat", "compacting")]


async def test_delete_purges_the_inactive_harnesses_state_too(env, monkeypatch):
    # Grok served "chat", then the profile swapped to Claude Code, which also
    # served it. DELETE (Claude active) must discard Grok's conversation too:
    # swapping back must not resume it.
    pytest.importorskip("grok_build_client")
    from miragen.harness import forget_inactive_harnesses
    from miragen.harness.grok import GrokHarness, GrokSettings, session_dir

    grok_home, grok_work = env.tmp_path / "grok-home", env.tmp_path / "grok-work"
    monkeypatch.setenv("MIRAGEN_GROK_HOME", str(grok_home))
    monkeypatch.setenv("MIRAGEN_GROK_WORKDIRS", str(grok_work))
    gsettings = GrokSettings.from_env(gateway_url="http://x/")
    grok_home.mkdir()
    (grok_home / "miragen-instances.json").write_text(
        json.dumps({"chat": {"session_id": "g1", "seq": 2}, "other": {"session_id": "g2"}}))
    session_dir(gsettings, "chat").mkdir(parents=True)
    (grok_work / "chat").mkdir(parents=True)
    env.served.mark("chat", "grok-build", 2)

    h = env.harness()
    await h.run(turn("hi"))
    removed = await h.forget("chat")
    removed += forget_inactive_harnesses("chat", active="claude-code",
                                         runs_root=env.tmp_path / "runs")
    assert {"grok-build:session_mapping", "grok-build:grok_sessions",
            "grok-build:workdir"} <= set(removed)
    assert json.loads((grok_home / "miragen-instances.json").read_text()) == {
        "other": {"session_id": "g2"}}
    assert not session_dir(gsettings, "chat").exists() and not (grok_work / "chat").exists()
    prof = AgentProfile.model_validate({
        "name": "mira", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "grok-build:grok-4.7", "instructions": "x"}})
    g = GrokHarness(prof, ToolGateway(prof), gsettings, served=env.served)
    assert g.session_info("chat") is None
