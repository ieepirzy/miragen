"""The Codex harness against a scripted app-server
(tests/fixtures/fake_codex_app_server.py): a real subprocess over JSON-RPC
stdio, the real tool gateway in-process, real state files. (The tool
boundary and dynamic tools are verified live against codex 0.159; see the
PR.)"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from miragen.harness import build_model_harness, parse_harness_model
from miragen.harness.base import HarnessTurn
from miragen.harness.codex import FEATURES_OFF, CodexHarness, CodexHarnessError, CodexSettings
from miragen.harness.gateway import ToolGateway
from miragen.harness.served import ServedLedger
from miragen.models import AgentProfile

FAKE = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


def speak(text: str) -> str:
    """Say something."""
    return f"spoke:{text}"


def profile(**extra) -> AgentProfile:
    return AgentProfile.model_validate({
        "name": "mira", "mode": "interactive", "triggers": [{"type": "http"}],
        "inject_timestamp": False,
        "spec": {"model": "codex:gpt-5.5", "instructions": "You are Mira.",
                 "capabilities": ["WebSearch", "WebFetch"]},
        **extra,
    })


@pytest.fixture
def codex_bin(tmp_path):
    path = tmp_path / "codex"
    path.write_text(f"#!/bin/sh\nshift\nexec {sys.executable} {FAKE}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class Env:
    def __init__(self, tmp_path, codex_bin):
        self.tmp_path = tmp_path
        self.home = tmp_path / "codex-home"
        self.home.mkdir()
        (self.home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
        self.codex_bin = codex_bin
        self.served = ServedLedger(tmp_path / "runs" / "harness" / "served.json")
        self.harnesses: list[CodexHarness] = []

    def harness(self, prof=None, **kw) -> CodexHarness:
        prof = prof or profile()
        gateway = ToolGateway(prof, runtime_tools=[speak],
                              native_capabilities=CodexHarness.native_capabilities)
        h = CodexHarness(prof, gateway, CodexSettings(
            codex_home=self.home, workdirs=self.tmp_path / "work", gateway_url="unused",
            codex_bin=self.codex_bin, **kw), served=self.served)
        self.harnesses.append(h)
        return h

    def log(self) -> list[dict]:
        path = self.home / "fake.log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def state(self) -> dict:
        return json.loads((self.home / "miragen-instances.json").read_text())


@pytest.fixture
async def env(tmp_path, codex_bin):
    e = Env(tmp_path, codex_bin)
    yield e
    for h in e.harnesses:
        await h.aclose()


def turn(prompt, instance="chat", use_history=True, run_id="r1", **kw) -> HarnessTurn:
    return HarnessTurn(prompt=prompt, instance=instance, use_history=use_history, run_id=run_id, **kw)


def test_codex_prefix_builds_the_harness_with_a_hermetic_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRAGEN_CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("MIRAGEN_CODEX_AUTO_COMPACT_TOKENS", "60000")
    assert parse_harness_model("codex:gpt-5.5") == ("codex", "gpt-5.5")
    (tmp_path / "codex-home").mkdir()
    (tmp_path / "codex-home" / "hooks.json").write_text("{}")  # stale: must go
    harness, _gateway = build_model_harness(profile(), runs_root=tmp_path)
    assert harness.name == "codex" and harness.model == "gpt-5.5"
    config = (tmp_path / "codex-home" / "config.toml").read_text()
    assert "shell_tool = false" in config and "unified_exec = false" in config
    assert "multi_agent = false" in config
    assert "code_mode_host" not in config  # gpt-6 models call tools through code mode
    assert 'web_search = "live"' in config and "model_auto_compact_token_limit = 60000" in config
    assert "mcp_servers" not in config
    assert not (tmp_path / "codex-home" / "hooks.json").exists()


async def test_turn_shape_and_hermetic_process(env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "metered")
    monkeypatch.setenv("CODEX_API_KEY", "metered")
    monkeypatch.setenv("UPSTREAM_SECRET", "x")
    h = env.harness()
    res = await h.run(turn("hello", extra_instructions="PACKET"))
    # Only final answers are the reply: commentary and sub-agent text are not.
    assert res.output == 'echo: <context source="miragen">\nPACKET\n</context>\n\nhello'
    assert res.usage.input_tokens == 900 and res.usage.cached_input_tokens == 400
    assert h.session_info("chat")["context_tokens"] == 450
    start = next(e for e in env.log() if "argv" in e)
    assert start["argv"] == [] and start["cwd"].endswith("/work/chat")
    assert {"OPENAI_API_KEY", "CODEX_API_KEY", "UPSTREAM_SECRET"}.isdisjoint(start["env"])
    assert "CODEX_HOME" in start["env"]
    init = next(e for e in env.log() if "initialize" in e)["initialize"]
    assert init["capabilities"] == {"experimentalApi": True}
    params = next(e for e in env.log() if "thread_start" in e)["thread_start"]
    assert params["approvalPolicy"] == "untrusted" and params["approvalsReviewer"] == "user"
    assert params["sandbox"] == "read-only" and params["model"] == "gpt-5.5"
    assert params["baseInstructions"].startswith("You are Mira.")
    assert all(params["config"][f"features.{f}"] is False for f in FEATURES_OFF)
    assert [t["name"] for t in params["dynamicTools"]] == ["speak"]
    assert params["dynamicTools"][0]["deferLoading"] is False
    assert not any(k.startswith("mcp_servers") for k in params["config"])


async def test_gateway_tools_run_as_dynamic_tools_and_are_recorded(env):
    h = env.harness()
    res = await h.run(turn('CALL speak {"text": "moi"}'))
    assert res.output == "tool[speak]=spoke:moi ok=True"
    assert [(c.tool_name, c.ok) for c in res.tool_calls] == [("speak", True)]
    res = await h.run(turn('CALL nope {}', run_id="r2"))
    assert res.output.endswith("ok=False")


async def test_everything_else_is_declined(env):
    h = env.harness()
    assert (await h.run(turn("PATCH"))).output == "patch=decline"
    weird = (await h.run(turn("WEIRD", run_id="r2"))).output
    assert "unsupported request" in weird  # an unknown request is refused, not hung


async def test_restart_resumes_the_thread_and_a_lost_rollout_starts_fresh(env):
    h = env.harness()
    await h.run(turn("one"))
    tid = env.state()["chat"]["thread_id"]
    await h.aclose()
    h2 = env.harness()
    res = await h2.run(turn("HISTORY", run_id="r2"))
    assert res.output == "turns=2 carried=0"
    resume = next(e for e in env.log() if "thread_resume" in e)["thread_resume"]
    assert resume["threadId"] == tid and [t["name"] for t in resume["dynamicTools"]] == ["speak"]
    assert h2.session_info("chat")["fresh"] is False
    await h2.aclose()
    for f in (env.home / "sessions").rglob("*.jsonl"):
        f.unlink()
    h3 = env.harness()
    await h3.run(turn("again", run_id="r3"))
    assert env.state()["chat"]["thread_id"] != tid


async def test_auth_is_subscription_only(env):
    (env.home / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
    with pytest.raises(CodexHarnessError, match="ChatGPT subscription login"):
        await env.harness().run(turn("hi"))
    (env.home / "auth.json").unlink()
    with pytest.raises(CodexHarnessError, match="device-auth"):
        await env.harness().run(turn("hi"))


async def test_a_failed_turn_raises_with_codex_error(env):
    h = env.harness()
    with pytest.raises(CodexHarnessError, match="usage limit"):
        await h.run(turn("FAIL"))


async def test_commentary_is_the_reply_only_when_there_is_no_final_answer(env):
    h = env.harness()
    assert (await h.run(turn("ONLYCOMMENTARY"))).output == "working on it"


async def test_compaction_reaches_the_memory_plane_and_swaps_read_as_fresh(env):
    events = []

    async def lifecycle(instance, event, info):
        events.append((instance, event, info))

    env.served.mark("chat", "grok-build", 4)
    h = env.harness()
    h.on_lifecycle = lifecycle
    assert h.session_info("chat")["fresh"] is True and h.session_info("chat")["seq"] == 5
    await h.run(turn("COMPACT"))
    assert events == [("chat", "compacting", {"trigger": "codex"})]
    info = h.session_info("chat")
    assert info["compactions"] == 1 and info["seq"] == 5 and info["fresh"] is False
    assert env.served.get("chat")["harness"] == "codex"


async def test_ephemeral_runs_leave_nothing_behind(env):
    h = env.harness()
    await h.run(turn("hi", instance=None, use_history=False, run_id="e1"))
    assert not list((env.home / "sessions").rglob("*.jsonl")) if (env.home / "sessions").exists() else True
    assert not (env.tmp_path / "work" / "run-e1").exists()
    assert h.status()["processes"] == []


async def test_rotate_and_forget(env):
    h = env.harness()
    await h.run(turn("one"))
    first = env.state()["chat"]["thread_id"]
    info = await h.rotate("chat")
    assert info["fresh"] is True and info["seq"] == 2
    await h.run(turn("two", run_id="r2"))
    second = env.state()["chat"]["thread_id"]
    assert second != first
    removed = await h.forget("chat")
    assert {"process", "session_mapping", "workdir",
            f"codex_thread:{first}", f"codex_thread:{second}"} <= set(removed)
    assert h.session_info("chat") is None and env.served.get("chat") is None


async def test_tool_names_the_model_api_rejects_get_aliases(env):
    h = env.harness()

    def odd_tool(text: str) -> str:
        """An upstream-style name with a dot."""
        return f"odd:{text}"

    h.gateway._add_local("home.odd", odd_tool, runtime=True)
    tools = await h._dynamic_tools()
    assert [t["name"] for t in tools] == ["speak", "home_odd"]
    res = await h.run(turn('CALL home_odd {"text": "x"}'))
    assert res.output == "tool[home_odd]=odd:x ok=True"
    assert [c.tool_name for c in res.tool_calls] == ["home.odd"]


def _odd(text: str) -> str:
    """Another tool."""
    return text


async def test_new_tools_move_to_a_new_thread_carrying_the_history(env):
    h = env.harness()
    await h.run(turn("remember PELICAN"))
    await h.run(turn("COMPACT", run_id="r2"))
    await h.run(turn("after compaction", run_id="r3"))
    old = env.state()["chat"]["thread_id"]
    await h.aclose()
    h2 = env.harness()
    h2.gateway._add_local("lamp", _odd, runtime=True)  # an upstream tool appeared
    res = await h2.run(turn("HISTORY", run_id="r4"))
    state = env.state()["chat"]
    assert state["thread_id"] != old and set(state["tools"]) == {"speak", "lamp"}
    inject = next(e for e in env.log() if "inject_items" in e)["inject_items"]
    texts = [i.get("encrypted_content") or i["content"][0]["text"] for i in inject["items"]]
    # From the last compaction on: its summary + messages, then later turns.
    assert texts == ["SUMMARISED", "ENC", "reply", "after compaction", "reply"]
    assert res.output == "turns=1 carried=5"
    assert h2.session_info("chat")["fresh"] is False  # carried: no transcript needed


async def test_a_vanished_tool_stays_declared_and_the_thread_resumes(env):
    h = env.harness()
    h.gateway._add_local("lamp", _odd, runtime=True)
    await h.run(turn("one"))
    tid = env.state()["chat"]["thread_id"]
    await h.aclose()
    h2 = env.harness()  # 'lamp' is gone (its upstream is down)
    await h2.run(turn("two", run_id="r2"))
    assert env.state()["chat"]["thread_id"] == tid
    resume = next(e for e in env.log() if "thread_resume" in e)["thread_resume"]
    assert {t["name"] for t in resume["dynamicTools"]} == {"speak", "lamp"}
