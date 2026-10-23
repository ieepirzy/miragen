"""The Grok Build harness end to end against a scripted `grok agent`
(tests/fixtures/fake_grok_agent.py): real subprocesses over ACP stdio, the
real tool gateway over HTTP, real session files on disk."""

from __future__ import annotations

import contextlib

import asyncio
import json
import os
import socket
import stat
import time
import sys
from pathlib import Path

import pytest
import uvicorn

from grok_build_client import AcpAuthError
from miragen.harness.base import HarnessTurn
from miragen.harness.gateway import ToolGateway
from miragen.harness.grok import GrokHarness, GrokHarnessError, GrokSettings, is_gateway_tool_call
from miragen.models import AgentProfile

FAKE = Path(__file__).parent / "fixtures" / "fake_grok_agent.py"


def profile(instructions="You are Mira.") -> AgentProfile:
    return AgentProfile.model_validate({
        "name": "mira", "mode": "interactive", "triggers": [{"type": "http"}],
        "inject_timestamp": False,
        "spec": {"model": "grok-build:grok-4.6", "instructions": instructions},
    })


def speak(text: str) -> str:
    """Say something."""
    return f"spoke:{text}"


def memory_remember(text: str) -> str:
    """Remember something (the plane's answer shape)."""
    return '{"status": "accepted", "record_id": "r1"}'


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def grok_bin(tmp_path):
    # An executable wrapper so AcpSession spawns it like the real binary.
    path = tmp_path / "grok"
    path.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "grok-home"
    h.mkdir()
    (h / "auth.json").write_text("{}")  # "logged in"
    return h


class Env:
    def __init__(self, tmp_path, home, grok_bin):
        self.tmp_path, self.home, self.grok_bin = tmp_path, home, grok_bin
        self.port = free_port()
        self.gateway = ToolGateway(profile(), runtime_tools=[speak, memory_remember])
        self.harnesses: list[GrokHarness] = []

    def harness(self, instructions="You are Mira.", **kw) -> GrokHarness:
        self.gateway.profile = profile(instructions)
        settings = GrokSettings(grok_home=self.home, workdirs=self.tmp_path / "work",
                                gateway_url=f"http://127.0.0.1:{self.port}/",
                                grok_bin=self.grok_bin, **kw)
        h = GrokHarness(profile(instructions), self.gateway, settings)
        self.harnesses.append(h)
        return h

    def log(self) -> list[dict]:
        path = self.home / "fake-agent.log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
async def env(tmp_path, home, grok_bin):
    e = Env(tmp_path, home, grok_bin)
    server = uvicorn.Server(uvicorn.Config(e.gateway.asgi, host="127.0.0.1", port=e.port,
                                           log_level="warning", lifespan="off", interface="asgi3"))
    stop = asyncio.Event()

    async def serve():
        # One task owns the gateway's task group from enter to exit.
        async with e.gateway.running():
            task = asyncio.create_task(server.serve())
            await stop.wait()
            server.should_exit = True
            await task

    bg = asyncio.create_task(serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield e
    finally:
        for h in e.harnesses:
            await h.aclose()
        stop.set()
        await bg


def fake_session(env, sid: str) -> Path:
    return next((env.home / "sessions").glob(f"*/{sid}/session.json"))


def turn(prompt, instance="chat", use_history=True, run_id="r1", **kw) -> HarnessTurn:
    return HarnessTurn(prompt=prompt, instance=instance, use_history=use_history, run_id=run_id, **kw)


async def test_turns_reuse_one_process_and_continue_the_session(env):
    h = env.harness()
    first = await h.run(turn("hello there", run_id="r1"))
    second = await h.run(turn("HISTORY", run_id="r2"))
    assert first.output == "echo: hello there\n"
    assert first.usage.input_tokens == 10 and first.usage.output_tokens == 5
    assert second.output == "turns=2 rules='You are Mira.'\n"
    starts = [e for e in env.log() if "argv" in e]
    assert len(starts) == 1  # one long-lived process for the instance


async def test_process_is_hermetic_and_subscription_only(env, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-should-never-leak")
    monkeypatch.setenv("GROK_CLAUDE_MCPS_ENABLED", "true")
    h = env.harness()
    await h.run(turn("hi"))
    start = next(e for e in env.log() if "argv" in e)
    assert "XAI_API_KEY" not in start["env"] and "GROK_CLAUDE_MCPS_ENABLED" not in start["env"]
    assert start["home"].endswith("hermetic-home") and start["has_gateway_token"]
    assert start["argv"][:1] == ["agent"] and "--agent-profile" in start["argv"]
    assert start["argv"][-2:] == ["--no-leader", "stdio"] and "--always-approve" not in start["argv"]
    assert start["cwd"].endswith("/work/chat")
    init = next(e for e in env.log() if "initialize" in e)["initialize"]
    assert init["clientCapabilities"] == {"fs": {"readTextFile": False, "writeTextFile": False},
                                          "terminal": False}
    profile_md = (env.home / "miragen-agent-profile.md").read_text()
    assert "tools: search_tool, use_tool" in profile_md
    config = (env.home / "config.toml").read_text()
    assert "${MIRAGEN_GATEWAY_TOKEN}" in config  # the credential is never on disk
    assert env.gateway.credential("chat") not in config


async def test_tool_calls_go_through_the_gateway_and_are_recorded(env):
    h = env.harness()
    res = await h.run(turn('CALL speak {"text": "moi"}'))
    assert res.output == "tool[speak]=spoke:moi\n"
    assert [(c.tool_name, c.ok) for c in res.tool_calls] == [("speak", True)]


async def test_non_gateway_permission_requests_are_refused(env):
    h = env.harness()
    res = await h.run(turn("BUILTIN"))
    assert res.output == "builtin=refused\n"
    assert is_gateway_tool_call({"toolCall": {"title": "gateway__speak"}})
    assert not is_gateway_tool_call({"toolCall": {"title": "run_terminal_cmd"}})
    assert not is_gateway_tool_call({"toolCall": {"title": "other__speak"}})


async def test_restart_resumes_the_same_session_via_load(env):
    h = env.harness()
    await h.run(turn("one"))
    await h.aclose()
    h2 = env.harness()
    res = await h2.run(turn("HISTORY", run_id="r2"))
    assert res.output.startswith("turns=2 ")
    loads = [e for e in env.log() if "session_load" in e]
    assert len(loads) == 1 and loads[0]["mcpServers"] == [] and loads[0]["cwd"].endswith("/work/chat")


async def test_changed_instructions_fork_and_deliver_the_update_once(env):
    h = env.harness("You are Mira.")
    await h.run(turn("one"))
    await h.aclose()
    h2 = env.harness("You are Mira, v2.")
    res = await h2.run(turn("HISTORY", run_id="r2"))
    # history kept; grok keeps the fork's original rules (verified live) …
    assert res.output.endswith("turns=2 rules='You are Mira.'\n")
    fork = next(e for e in env.log() if "fork" in e)
    state = json.loads((env.home / "miragen-instances.json").read_text())["chat"]
    assert state["session_id"] == fork["to"] and state["instructions_update_pending"] is False
    # … so the new instructions ride the first turn after the fork, once
    turns = json.loads(fake_session(env, fork['to']).read_text())["turns"]
    assert "<instructions-update" in turns[-1] and "You are Mira, v2." in turns[-1]
    await h2.run(turn("again", run_id="r3"))
    turns = json.loads(fake_session(env, fork['to']).read_text())["turns"]
    assert "<instructions-update" not in turns[-1]


async def test_instructions_update_stays_pending_until_a_turn_delivers_it(env):
    h = env.harness("You are Mira.")
    await h.run(turn("one"))
    await h.aclose()
    h2 = env.harness("You are Mira, v2.", turn_timeout_s=0.5)
    with pytest.raises(GrokHarnessError):
        await h2.run(turn("SLEEP", run_id="r2"))
    state = json.loads((env.home / "miragen-instances.json").read_text())["chat"]
    assert state["instructions_update_pending"] is True
    h2.settings.turn_timeout_s = 30
    await h2.run(turn("retry", run_id="r3"))
    sid = json.loads((env.home / "miragen-instances.json").read_text())["chat"]["session_id"]
    turns = json.loads(fake_session(env, sid).read_text())["turns"]
    assert "<instructions-update" in turns[-1]


async def test_memory_packet_rides_in_the_prompt(env):
    h = env.harness()
    await h.run(turn("question", extra_instructions="MEMORY: Ilari likes rye bread"))
    sid = json.loads((env.home / "miragen-instances.json").read_text())["chat"]["session_id"]
    turns = json.loads(fake_session(env, sid).read_text())["turns"]
    assert "MEMORY: Ilari likes rye bread" in turns[0] and turns[0].rstrip().endswith("question")


async def test_ephemeral_runs_get_a_fresh_session_and_no_lingering_process(env):
    h = env.harness()
    a = await h.run(turn("HISTORY", use_history=False, run_id="e1"))
    b = await h.run(turn("HISTORY", use_history=False, run_id="e2"))
    assert a.output.startswith("turns=1") and b.output.startswith("turns=1")
    assert h.status()["processes"] == []
    assert not (env.home / "miragen-instances.json").exists()


async def test_capacity_evicts_idle_process_and_resumes_it_later(env):
    h = env.harness(max_processes=1)
    await h.run(turn("one", instance="a"))
    await h.run(turn("one", instance="b", run_id="r2"))
    assert h.status()["processes"] == ["b"]
    res = await h.run(turn("HISTORY", instance="a", run_id="r3"))
    assert res.output.startswith("turns=2")
    assert any(e.get("session_load") for e in env.log())


async def test_turn_timeout_cancels_and_drops_the_process(env):
    h = env.harness(turn_timeout_s=0.5)
    with pytest.raises(GrokHarnessError, match="cancelled|exceeded"):
        await h.run(turn("SLEEP"))
    assert h.status()["processes"] == []
    res = await h.run(turn("HISTORY", run_id="r2"))  # comes back via session/load
    assert res.output.startswith("turns=2")


async def test_not_logged_in_fails_loudly(env):
    (env.home / "auth.json").unlink()
    h = env.harness()
    with pytest.raises(AcpAuthError, match="grok login"):
        await h.run(turn("hi"))


async def test_stream_yields_deltas_then_result(env):
    h = env.harness()
    async with h.stream(turn("stream me")) as stream:
        deltas = [d async for d in stream]
    assert deltas == ["echo: ", "stream me\n"] and stream.result.output == "echo: stream me\n"


async def test_per_launch_credentials_reach_the_gateway_for_that_turn_only(env):
    h = env.harness()
    seen = []
    original = h.gateway.turn

    @contextlib.contextmanager
    def spy(instance, run_id, *, secret_env=None):
        with original(instance, run_id, secret_env=secret_env) as log:
            seen.append(dict(log.secret_env or {}))
            yield log
        seen.append(log.secret_env)

    h.gateway.turn = spy
    await h.run(turn("hi", secret_env={"TOKEN": "x"}))
    assert seen == [{"TOKEN": "x"}, None]


# ── through the app tier ─────────────────────────────────────────────────────

@pytest.fixture
async def app_with_grok(env, tmp_path, monkeypatch):
    import miragen.app as app_module
    from miragen.runs import RunStore

    h = env.harness()
    monkeypatch.setattr(app_module, "HISTORIES_DIR", tmp_path / "histories")
    app_module._profile = profile()
    app_module._agent = None
    app_module._harness = h
    app_module._run_store = RunStore(root=tmp_path / "runs")
    try:
        yield app_module
    finally:
        app_module._harness = None
        app_module._profile = None
        app_module._run_store = None
        app_module._active_runs = 0
        app_module._busy_instances.clear()


async def test_http_run_records_output_and_gateway_tool_calls(app_with_grok):
    from httpx import ASGITransport, AsyncClient
    app_module = app_with_grok
    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://t") as c:
        r = await c.post("/run", json={"prompt": 'CALL speak {"text": "hei"}',
                                       "use_history": True, "instance": "chat"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["output"] == "tool[speak]=spoke:hei\n"
        rec = (await c.get(f"/runs/{body['run_id']}")).json()
        assert rec["status"] == "succeeded" and rec["instance"] == "chat"
        assert [t["tool_name"] for t in rec["tool_calls"]] == ["speak"]
        r2 = await c.post("/run", json={"prompt": "HISTORY", "use_history": True, "instance": "chat"})
        assert r2.json()["output"].startswith("turns=2 ")


async def test_http_stream_through_the_grok_harness(app_with_grok):
    from httpx import ASGITransport, AsyncClient
    app_module = app_with_grok
    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://t") as c:
        async with c.stream("POST", "/run/stream", json={"prompt": "streamed", "use_history": True,
                                                          "instance": "chat"}) as resp:
            body = "".join([chunk async for chunk in resp.aiter_text()])
    assert "data: echo: " in body and "data: [DONE]" in body


@pytest.mark.parametrize("call,allowed", [
    ({"title": "gateway__speak"}, True),
    ({"toolName": "gateway__mira_subscribe"}, True),
    ({"title": "use_tool", "rawInput": {"server": "gateway", "tool": "speak"}}, True),
    ({"title": "use_tool", "rawInput": {"tool_name": "gateway__speak"}}, True),
    # model-written input mentioning a gateway tool never approves a built-in
    ({"title": "run_terminal_cmd", "rawInput": {"command": "echo gateway__x; cat ~/.ssh/id_rsa"}}, False),
    ({"title": "read_file", "rawInput": {"path": "/gateway__speak"}}, False),
    ({"title": "use_tool", "rawInput": {"server": "evil", "tool": "gateway__speak"}}, False),
    ({"title": "use_tool", "rawInput": {"server": "gatewayx", "tool": "speak"}}, False),
    ({"title": "gateway__speak; rm -rf /"}, False),
    ({"title": "other__speak"}, False),
    ({}, False),
])
def test_gateway_check_reads_identity_never_arguments(call, allowed):
    assert is_gateway_tool_call({"toolCall": call}) is allowed


async def test_a_reserved_agent_is_never_evicted(env):
    """An agent handed to a turn (reserved) but not yet locked must survive a
    capacity eviction triggered by another instance's spawn."""
    h = env.harness(max_processes=1)
    agent_a = await h._agent_for("a", ephemeral=False)      # reserved, not locked
    b = await h.run(turn("two", instance="b", run_id="rb"))  # needs a slot
    assert b.output == "echo: two\n"
    assert "a" in h.status()["processes"] and agent_a.acp.alive
    agent_a.reserved -= 1


async def test_concurrent_turns_on_different_instances_both_succeed(env):
    h = env.harness(max_processes=1)
    a, b = await asyncio.gather(h.run(turn("one", instance="a", run_id="ra")),
                                h.run(turn("two", instance="b", run_id="rb")))
    assert a.output == "echo: one\n" and b.output == "echo: two\n"


async def test_voice_guidance_is_part_of_the_session_rules(env):
    base = env.harness()
    h = GrokHarness(profile(), env.gateway, base.settings, system_guidance="Tags: [laugh]")
    env.harnesses.append(h)
    res = await h.run(turn("HISTORY", instance="voice"))
    assert "You are Mira." in res.output
    assert "## Speaking aloud" in res.output and "Tags: [laugh]" in res.output


async def test_web_capabilities_enable_grok_builtins_and_nothing_else(env):
    p = AgentProfile.model_validate({
        "name": "mira", "mode": "interactive", "triggers": [{"type": "http"}],
        "inject_timestamp": False,
        "spec": {"model": "grok-build:grok-4.6", "instructions": "You are Mira.",
                 "capabilities": ["WebSearch", "WebFetch"]}})
    base = env.harness()
    h = GrokHarness(p, env.gateway, base.settings)
    env.harnesses.append(h)
    assert "tools: search_tool, use_tool, web_fetch, web_search" in \
        (env.home / "miragen-agent-profile.md").read_text()
    config = (env.home / "config.toml").read_text()
    assert "web_fetch = true" in config and "backend_tools = true" in config
    assert "disable_web_search = false" in config
    fetch = {"toolCall": {"title": "Fetch https://x", "_meta": {"x.ai/tool": {"name": "web_fetch"}}}}
    shell = {"toolCall": {"title": "run", "_meta": {"x.ai/tool": {"name": "run_terminal_cmd"}}}}
    assert await h._permission(fetch) == "allow"
    assert await h._permission(shell) == "deny"
    # without the capabilities, web tools are neither granted nor allowed
    assert await base._permission(fetch) == "deny"


async def test_text_after_a_tool_call_starts_a_new_paragraph(env):
    h = env.harness()
    res = await h.run(turn('SAY Checking.\nCALL speak {"text": "x"}'))
    assert res.output == "Checking.\n\ntool[speak]=spoke:x\n"


async def test_clocks_are_ordered_for_gated_calls(env):
    p = AgentProfile.model_validate({
        "name": "mira", "mode": "interactive", "triggers": [{"type": "http"}],
        "approval_required": ["crm_*"], "approval_mode": "queue", "approval_timeout_s": 900,
        "spec": {"model": "grok-build:", "instructions": "i"}})
    base = env.harness(turn_timeout_s=600)
    h = GrokHarness(p, env.gateway, base.settings)
    env.harnesses.append(h)
    assert h.tool_timeout_s == 1020
    assert h.settings.turn_timeout_s >= h.tool_timeout_s + 60
    assert "tool_timeout_sec = 1020" in (env.home / "config.toml").read_text()


def test_session_dir_matches_grok_on_disk_layout(tmp_path):
    """Name observed live (grok 1.0.41, miragen image): the directory is the
    percent-encoded working directory."""
    h = GrokHarness.__new__(GrokHarness)
    h.settings = GrokSettings(grok_home=Path("/agent/grok-home"),
                              workdirs=Path("/agent/workspaces"), gateway_url="http://x/")
    assert h.session_dir("conv_1b945f6f2f397d9e") == Path(
        "/agent/grok-home/sessions/%2Fagent%2Fworkspaces%2Fconv_1b945f6f2f397d9e")


async def test_forget_discards_process_session_files_and_workdir(env):
    h = env.harness()
    await h.run(turn("one", instance="old"))
    await h.run(turn("keep me", instance="other", run_id="r2"))
    sessions = h.session_dir("old")
    assert sessions.is_dir() and any(sessions.iterdir())
    removed = await h.forget("old")
    assert set(removed) == {"process", "session_mapping", "grok_sessions", "workdir"}
    assert not sessions.exists() and not (env.tmp_path / "work" / "old").exists()
    assert "old" not in h.status()["processes"]
    assert "old" not in json.loads((env.home / "miragen-instances.json").read_text())
    # the other instance is untouched
    assert h.session_dir("other").is_dir()
    assert (await h.run(turn("HISTORY", instance="other", run_id="r3"))).output.startswith("turns=2 ")
    # the forgotten name starts over with a new session, not a load
    res = await h.run(turn("HISTORY", instance="old", run_id="r4"))
    assert res.output.startswith("turns=1 ")
    assert await h.forget("never-existed") == []


async def test_forget_removes_forked_sessions_too(env):
    h = env.harness("You are Mira.")
    await h.run(turn("one"))
    await h.aclose()
    h2 = env.harness("You are Mira, v2.")
    await h2.run(turn("two", run_id="r2"))           # forks within the same cwd
    assert len(list(h2.session_dir("chat").iterdir())) == 2
    await h2.forget("chat")
    assert not h2.session_dir("chat").exists()


async def test_forget_refuses_a_busy_instance(env):
    from miragen.harness import InstanceBusyError
    h = env.harness()
    agent = await h._agent_for("busy", ephemeral=False)  # reserved = a turn holds it
    with pytest.raises(InstanceBusyError):
        await h.forget("busy")
    assert agent.acp.alive
    agent.reserved -= 1


async def test_http_delete_instance_discards_the_grok_conversation(app_with_grok, env):
    from httpx import ASGITransport, AsyncClient
    app_module = app_with_grok
    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://t") as c:
        await c.post("/run", json={"prompt": "one", "use_history": True, "instance": "chat"})
        r = await c.delete("/instances/chat")
        assert r.status_code == 200, r.text
        assert {"process", "grok_sessions", "workdir"} <= set(r.json()["deleted"])
        assert not app_module._harness.session_dir("chat").exists()
        r2 = await c.post("/run", json={"prompt": "HISTORY", "use_history": True, "instance": "chat"})
        assert r2.json()["output"].startswith("turns=1 ")  # a fresh conversation
        assert (await c.delete("/instances/nope")).status_code == 404


# ── MiraRun execution refs (execution-refs/v1) ─────────────────────────────

MIRARUN_REFS = {"agent_session_id": "4f0c2a7e-1d2b-4c5e-9f00-112233445566",
                "lane_id": "lane-1", "lane_role": "engineer", "lane_slot": "engineer-1"}
MIRARUN_ENV = {"MIRARUN_AGENT_SESSION_ID": MIRARUN_REFS["agent_session_id"],
               "MIRARUN_LANE_ID": "lane-1", "MIRARUN_LANE_ROLE": "engineer",
               "MIRARUN_LANE_SLOT": "engineer-1"}


def _spy_env(harness) -> list[dict]:
    seen: list[dict] = []
    original = harness._env

    def spy(instance):
        env = original(instance)
        seen.append(env)
        return env

    harness._env = spy
    return seen


async def test_grok_spawns_with_refs_and_one_off_runs_keep_none(env):
    h = env.harness()
    seen = _spy_env(h)
    await h.run(turn("hello", mirarun=MIRARUN_REFS))
    assert seen and {k: seen[0][k] for k in MIRARUN_ENV} == MIRARUN_ENV
    await h.run(turn("once", use_history=False, run_id="eph", mirarun={"run_id": "run-9"}))
    assert seen[-1]["MIRARUN_RUN_ID"] == "run-9"
    assert not h.__dict__.get("_mirarun_ephemeral")


async def test_usage_counts_model_requests_and_records_context(env):
    """requests = model requests the turn really made (response_completed per
    call, at least one per tool round + 1), not a hard-coded 1; context_tokens
    = the last request's prompt size."""
    h = env.harness()
    plain = await h.run(turn("hello", run_id="r1"))
    assert plain.usage.requests == 1
    assert plain.usage.context_tokens is not None and plain.usage.context_tokens > 0
    # A tool round with no per-call usage of its own still costs a second request.
    tool = await h.run(turn('SAY Checking.\nCALL speak {"text": "x"}\nSAY done', run_id="r2"))
    assert tool.usage.requests == 2
    # Three reported model calls in one turn are three requests.
    rounds = await h.run(turn("ROUND\nROUND\nSAY ok", run_id="r3"))
    assert rounds.usage.requests == 3
    assert rounds.usage.context_tokens > plain.usage.context_tokens  # the context grew


def _names(timings):
    return [s["name"] for s in timings["spans"]]


async def test_a_turn_records_where_its_time_went_cold_then_warm(env):
    """Mira#86: spawn / initialize / session open on the turn that started
    the agent, then prompt, first update, model responses, tool calls."""
    h = env.harness()
    cold = await h.run(turn('SAY Checking.\nCALL speak {"text": "x"}\nSAY done', run_id="r1"))
    t = cold.timings
    assert t["cold"] is True and t["total_ms"] > 0 and t["started_at"].endswith("Z")
    names = _names(t)
    for name in ("agent", "spawn", "initialize", "session_open", "lock_wait", "compose",
                 "prompt_sent", "first_update", "model_response", "tool_call", "end"):
        assert name in names, name
    session = next(s for s in t["spans"] if s["name"] == "session_open")
    assert session["detail"]["mode"] == "new"
    call = next(s for s in t["spans"] if s["name"] == "tool_call")
    assert call["detail"]["tool"] == "speak" and call["duration_ms"] is not None
    assert [s["start_ms"] for s in t["spans"]] == sorted(s["start_ms"] for s in t["spans"])
    s = t["summary"]
    assert s["spawn_ms"] >= 0 and s["first_update_ms"] > 0 and s["total_ms"] == t["total_ms"]
    assert s["tools_ms"] == call["duration_ms"]

    warm = await h.run(turn("hello again", run_id="r2"))
    assert warm.timings["cold"] is False
    assert not {"spawn", "initialize", "session_open"} & set(_names(warm.timings))


async def test_a_failed_turn_carries_its_timings_and_tool_calls_on_the_error(env):
    """Review on #209: the timed-out turn with a slow tool is the one most
    worth a waterfall, so its tool_call spans must survive the timeout."""
    h = env.harness(turn_timeout_s=0.5)
    with pytest.raises(GrokHarnessError) as err:
        await h.run(turn('CALL speak {"text": "x"}\nSLEEP', run_id="r1"))
    t = err.value.timings
    assert t["cold"] is True and "prompt_sent" in _names(t) and "end" not in _names(t)
    assert "tool_call" in _names(t) and t["summary"]["tools_ms"] >= 0


async def test_another_instances_listing_is_not_claimed_by_this_turn(env):
    """Review on #209 (probe): b's agent listing tools while a's turn runs
    must not land in a's waterfall; a listing by a's own session does."""
    import time as _time
    h = env.harness()
    await asyncio.gather(h.run(turn("hi", instance="a", run_id="a0")),
                         h.run(turn("hi", instance="b", run_id="b0")))

    async def listings_during_a():
        while "a" not in h.gateway._active:
            await asyncio.sleep(0)
        for who in ("b", "a"):
            now = _time.monotonic()
            h.gateway.listings.append({"upstream": "home", "instance": who, "start": now,
                                       "end": now + 0.01, "cached": False,
                                       "backing_off": False, "ok": True})
    ra, _ = await asyncio.gather(h.run(turn("hello", instance="a", run_id="a1")),
                                 listings_during_a())
    claimed = [sp for sp in ra.timings["spans"] if sp["name"] == "list_tools"]
    assert len(claimed) == 1 and claimed[0]["detail"]["upstream"] == "home"
    assert ra.timings["summary"]["list_tools_ms"] == claimed[0]["duration_ms"]


async def test_timing_trouble_never_fails_a_turn_or_masks_its_error(env, monkeypatch):
    h = env.harness()

    def broken(*a, **kw):
        raise RuntimeError("timing bug")
    monkeypatch.setattr(h, "_finish_timings", broken)
    ok = await h.run(turn("hello", run_id="r1"))
    assert ok.output == "echo: hello\n" and ok.timings is None
    h.settings.turn_timeout_s = 0.3
    with pytest.raises(GrokHarnessError, match="exceeded"):   # the real error, not the timing one
        await h.run(turn("SLEEP", run_id="r2"))


async def test_spawn_spans_start_when_the_spawn_did_not_when_the_wait_began(env):
    """Review on #209: _agent_for first waits on the spawn lock (another
    instance spawning); the spawn spans sit after that wait."""
    h = env.harness()

    async def hold_spawn_lock():
        async with h._spawn_lock:
            await asyncio.sleep(0.4)
    holder = asyncio.create_task(hold_spawn_lock())
    await asyncio.sleep(0)
    result = await h.run(turn("hello", run_id="r1"))
    await holder
    spans = {sp["name"]: sp for sp in result.timings["spans"]}
    assert spans["spawn"]["start_ms"] - spans["agent"]["start_ms"] >= 300
    assert spans["agent"]["duration_ms"] >= 400


async def test_the_run_record_carries_the_turns_timings_over_http(app_with_grok):
    """app.py wiring: /run's record has the harness timings (+ dispatch_ms),
    a failed run's record has the error's, the stream path too."""
    from httpx import ASGITransport, AsyncClient
    app_module = app_with_grok
    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://t") as c:
        body = (await c.post("/run", json={"prompt": "hi", "use_history": True,
                                           "instance": "chat"})).json()
        rec = (await c.get(f"/runs/{body['run_id']}")).json()
        assert rec["timings"]["cold"] is True and "dispatch_ms" in rec["timings"]["summary"]
        async with c.stream("POST", "/run/stream", json={"prompt": "streamed", "use_history": True,
                                                         "instance": "chat"}) as resp:
            [line async for line in resp.aiter_lines()]

        async def by_prompt(text):
            runs = (await c.get("/runs")).json()
            runs = runs["runs"] if isinstance(runs, dict) else runs
            rid = next(r["run_id"] for r in runs if text in (r.get("prompt_preview") or ""))
            return (await c.get(f"/runs/{rid}")).json()
        rec = await by_prompt("streamed")
        assert rec["timings"]["cold"] is False
        app_module._harness.settings.turn_timeout_s = 0.3
        failed = await c.post("/run", json={"prompt": "SLEEP", "use_history": True,
                                            "instance": "chat"})
        assert failed.status_code >= 400
        rec = await by_prompt("SLEEP")
        assert rec["status"] == "failed" and "prompt_sent" in _names(rec["timings"])


async def test_grok_end_usage_fills_model_calls_cache_reasoning_api_time_and_cost(env):
    # grok 1.0.62's end usage keys, as logged live (2026-10-18).
    (env.home / "fake-end-usage.json").write_text(json.dumps({
        "inputTokens": 10442, "outputTokens": 34, "cachedReadTokens": 9856,
        "cacheCreationTokens": 12, "reasoningTokens": 20, "apiDurationMs": 15123,
        "costUsdTicks": 777, "modelCalls": 3, "numTurns": 1, "totalTokens": 10476}))
    h = env.harness()
    result = await h.run(turn("hello", run_id="r1"))
    u = result.usage
    assert (u.cached_input_tokens, u.cache_write_tokens, u.reasoning_tokens) == (9856, 12, 20)
    assert (u.api_duration_ms, u.cost_usd_ticks, u.requests) == (15123, 777, 3)
    assert result.timings["summary"]["model_api_ms"] == 15123


async def test_reasoning_effort_is_set_per_instance(env):
    # Mira (2026-10-18): chat on medium, heartbeats on low; grok's default
    # for grok-4.6 is high.
    h = env.harness(effort="medium", instance_effort={"mira-heartbeat": "low"})
    chat = await h.run(turn("one", instance="chat"))
    await h.run(turn("one", instance="mira-heartbeat", run_id="r2"))
    argvs = {e["cwd"].rsplit("/", 1)[-1]: e["argv"] for e in env.log() if "argv" in e}
    assert argvs["chat"][argvs["chat"].index("--reasoning-effort") + 1] == "medium"
    assert argvs["mira-heartbeat"][argvs["mira-heartbeat"].index("--reasoning-effort") + 1] == "low"
    assert argvs["chat"][-2:] == ["--no-leader", "stdio"]
    spawn = next(sp for sp in chat.timings["spans"] if sp["name"] == "spawn")
    assert spawn["detail"]["effort"] == "medium"


async def test_no_effort_leaves_grok_its_default(env):
    h = env.harness()
    await h.run(turn("one"))
    start = next(e for e in env.log() if "argv" in e)
    assert "--reasoning-effort" not in start["argv"]


def test_effort_settings_from_env(monkeypatch):
    monkeypatch.setenv("MIRAGEN_GROK_EFFORT", "Medium")
    monkeypatch.setenv("MIRAGEN_GROK_INSTANCE_EFFORT", "mira-heartbeat=low, other=xhigh")
    s = GrokSettings.from_env(gateway_url="http://x/")
    assert s.effort == "medium" and s.effort_for("chat") == "medium"
    assert s.effort_for("mira-heartbeat") == "low" and s.effort_for("other") == "xhigh"
    monkeypatch.setenv("MIRAGEN_GROK_INSTANCE_EFFORT", "mira-heartbeat=lowest")
    with pytest.raises(ValueError, match="unknown reasoning effort 'lowest'"):
        GrokSettings.from_env(gateway_url="http://x/")


async def test_instance_idle_stops_a_background_lane_and_keeps_the_chat(env):
    # Mira (2026-10-18): the chat stays warm for a week, the heartbeat's
    # process stops as soon as its beat is done.
    h = env.harness(idle_s=3600.0, instance_idle_s={"mira-heartbeat": 1.0})
    await h.run(turn("one", instance="chat"))
    await h.run(turn("one", instance="mira-heartbeat", run_id="r2"))
    deadline = time.monotonic() + 10
    while "mira-heartbeat" in h.status()["processes"] and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert h.status()["processes"] == ["chat"]


def test_instance_idle_settings_from_env(monkeypatch):
    monkeypatch.setenv("MIRAGEN_GROK_IDLE_S", "604800")
    monkeypatch.setenv("MIRAGEN_GROK_INSTANCE_IDLE_S", "mira-heartbeat=120, mira-events=1800")
    s = GrokSettings.from_env(gateway_url="http://x/")
    assert s.idle_for("conv_x") == 604800 and s.idle_for("mira-heartbeat") == 120
    assert s.idle_for("mira-events") == 1800
    for bad in ("mira-heartbeat", "mira-heartbeat=soon", "=60", "x=-1", "x=nan"):
        monkeypatch.setenv("MIRAGEN_GROK_INSTANCE_IDLE_S", bad)
        with pytest.raises(ValueError, match="expected instance=seconds"):
            GrokSettings.from_env(gateway_url="http://x/")


async def test_prewarm_starts_the_most_recent_matching_instance_and_its_turn_is_warm(env):
    # Mira#86: most of Ilari's messages came hours apart, after a deploy or an
    # idle reap, and paid ~15 s of grok start + session/load.
    h = env.harness()
    await h.run(turn("one", instance="conv_old", run_id="r1"))
    await h.run(turn("one", instance="mira-heartbeat", run_id="r2"))
    await h.run(turn("one", instance="conv_new", run_id="r3"))
    await h.aclose()
    h2 = env.harness(prewarm=("conv_*",), prewarm_max=1)
    assert await h2.prewarm() == ["conv_new"]
    assert h2.status()["processes"] == ["conv_new"]
    res = await h2.run(turn("HISTORY", instance="conv_new", run_id="r4"))
    assert res.output.startswith("turns=2")          # the same conversation, resumed
    assert res.timings["cold"] is False              # its process was already running


async def test_prewarm_is_off_by_default_and_never_exceeds_the_process_cap(env):
    h = env.harness()
    await h.run(turn("one", instance="conv_a", run_id="r1"))
    await h.run(turn("one", instance="conv_b", run_id="r2"))
    await h.aclose()
    assert await env.harness().prewarm() == []
    capped = env.harness(prewarm=("conv_*",), prewarm_max=5, max_processes=1)
    assert len(await capped.prewarm()) == 1


def test_prewarm_settings_from_env(monkeypatch):
    monkeypatch.setenv("MIRAGEN_GROK_PREWARM", "conv_*, chat")
    monkeypatch.setenv("MIRAGEN_GROK_PREWARM_MAX", "2")
    s = GrokSettings.from_env(gateway_url="http://x/")
    assert s.prewarm == ("conv_*", "chat") and s.prewarm_max == 2


async def test_a_warm_turn_never_waits_behind_another_spawn(env):
    # Review of #215: a prewarm holds the spawn lock for a whole spawn
    # (~15 s); a turn whose process is already running must not wait on it.
    import asyncio

    h = env.harness()
    await h.run(turn("one", instance="warm", run_id="r1"))
    async with h._spawn_lock:                  # someone else is spawning
        res = await asyncio.wait_for(h.run(turn("two", instance="warm", run_id="r2")), 5)
    assert res.timings["cold"] is False


async def test_prewarm_skips_instances_with_mirarun_refs(env):
    h = env.harness()
    await h.run(turn("one", instance="conv_a", run_id="r1"))
    await h._update_instance("conv_a", mirarun={"MIRARUN_RUN_ID": "x"})
    await h.aclose()
    assert await env.harness(prewarm=("conv_*",)).prewarm() == []
