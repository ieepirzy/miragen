"""The Grok Build harness end to end against a scripted `grok agent`
(tests/fixtures/fake_grok_agent.py): real subprocesses over ACP stdio, the
real tool gateway over HTTP, real session files on disk."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
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
        self.gateway = ToolGateway(profile(), runtime_tools=[speak])
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


async def test_changed_instructions_fork_the_session_keeping_history(env):
    h = env.harness("You are Mira.")
    await h.run(turn("one"))
    await h.aclose()
    h2 = env.harness("You are Mira, v2.")
    res = await h2.run(turn("HISTORY", run_id="r2"))
    assert res.output == "turns=2 rules='You are Mira, v2.'\n"
    assert any("fork" in e for e in env.log())
    state = json.loads((env.home / "miragen-instances.json").read_text())
    assert state["chat"]["session_id"] == next(e["to"] for e in env.log() if "fork" in e)


async def test_memory_packet_rides_in_the_prompt(env):
    h = env.harness()
    await h.run(turn("question", extra_instructions="MEMORY: Ilari likes rye bread"))
    sid = json.loads((env.home / "miragen-instances.json").read_text())["chat"]["session_id"]
    turns = json.loads((env.home / "fake-sessions" / f"{sid}.json").read_text())["turns"]
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


async def test_per_launch_credentials_are_refused_not_dropped(env):
    h = env.harness()
    with pytest.raises(GrokHarnessError, match="per-launch MCP credentials"):
        await h.run(turn("hi", secret_env={"TOKEN": "x"}))


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
