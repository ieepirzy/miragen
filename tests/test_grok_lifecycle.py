"""The Grok harness's session lifecycle: memory save → compaction, rotation
with a handoff note, the ledger, and the policy seam. Real subprocesses
over ACP (tests/fixtures/fake_grok_agent.py models context growth,
/compact and grok's own compaction)."""

from __future__ import annotations

import asyncio
import json

from miragen.harness.grok_lifecycle import (
    Decision,
    Handoff,
    SessionStats,
    ThresholdPolicy,
    accepted_memory_writes,
    load_policy,
)
from miragen.models import ToolCallRecord
from tests.test_grok_harness import (  # noqa: F401
    env,
    fake_session,
    grok_bin,
    home,
    turn,
)

SMALL = ThresholdPolicy(compact_at_tokens=2_000, rotate_at_tokens=50_000, rotate_after_compactions=3)


def stats(ctx, compactions=0):
    return SessionStats(context_tokens=ctx, compactions=compactions, turns=1, age_s=1, seq=1)


def test_threshold_policy():
    p = ThresholdPolicy()   # the first guess: 150k / every 3rd / 400k
    assert p.decide(stats(149_999)).action == "none"
    assert p.decide(stats(150_000)) == Decision("compact", "context_threshold")
    assert p.decide(stats(150_000, compactions=1)).action == "compact"
    assert p.decide(stats(150_000, compactions=2)) == Decision("rotate", "compaction_count")
    assert p.decide(stats(400_000)) == Decision("rotate", "context_cap")


def test_policy_is_pluggable_and_env_tunable(monkeypatch):
    p = load_policy({"MIRAGEN_GROK_COMPACT_AT_TOKENS": "120000",
                     "MIRAGEN_GROK_ROTATE_AT_TOKENS": "300000",
                     "MIRAGEN_GROK_ROTATE_AFTER_COMPACTIONS": "4"})
    assert p == ThresholdPolicy(120_000, 300_000, 4)
    import sys
    import types
    mod = types.ModuleType("my_policy")
    mod.Never = type("Never", (), {"decide": lambda self, s: Decision("none")})
    monkeypatch.setitem(sys.modules, "my_policy", mod)
    assert load_policy({"MIRAGEN_GROK_LIFECYCLE_POLICY": "my_policy:Never"}).decide(
        stats(10**6)).action == "none"


def test_only_accepted_memory_writes_count():
    calls = [
        ToolCallRecord(tool_name="memory_memory_remember", args="{}", ok=True, result_status="accepted"),
        ToolCallRecord(tool_name="memory_memory_remember", args="{}", ok=True, result_status="pending"),
        ToolCallRecord(tool_name="memory_memory_checkpoint", args="{}", ok=False),
        ToolCallRecord(tool_name="memory_memory_recall", args="{}", ok=True, result_status="accepted"),
    ]
    assert accepted_memory_writes(calls) == 1


async def settle(h, instance="chat"):
    task = h._maintenance.get(instance)
    if task is not None:
        await task


def state(env, instance="chat") -> dict:
    return json.loads((env.home / "miragen-instances.json").read_text())[instance]


def ledger(h, instance="chat") -> list[dict]:
    return h.ledger.read(instance)


def script(env, maintenance: str = "", handoff: str | None = None) -> None:
    (env.home / "fake-maintenance.txt").write_text(maintenance)
    if handoff is not None:
        (env.home / "fake-handoff.txt").write_text(handoff)


async def test_compaction_saves_memories_first_then_compacts(env):
    h = env.harness(policy=SMALL)
    events = []

    async def hook(instance, event, info):
        events.append((instance, event, info))
    h.on_lifecycle = hook
    script(env, 'CALL memory_remember {"text": "Ilari likes saunas"}')
    res = await h.run(turn("hello\nGROW 3000", run_id="r1"))
    assert res.output.startswith("echo: hello")          # the user's turn isn't delayed
    await settle(h)
    sid = state(env)["session_id"]
    data = json.loads(fake_session(env, sid).read_text())
    # the memory-save turn ran, then /compact — in that order
    assert "<session-maintenance" in data["turns"][-1] and "compacted" in data["turns"][-1]
    assert data["compacted_after_turns"] == [len(data["turns"])]
    assert data["compact_hints"] and "memory" in data["compact_hints"][0]
    st = state(env)
    assert st["compactions"] == 1 and st["memory_writes"] == 1 and st["context_tokens"] < 3000
    comp = [e for e in ledger(h) if e["event"] == "compaction"]
    assert comp[0]["trigger"] == "miragen" and comp[0]["memory_writes"] == 1
    assert comp[0]["tokens_before"] > comp[0]["tokens_after"]
    assert ("chat", "compacting", {"trigger": "miragen"}) in events
    # the conversation continues in the same session
    assert (await h.run(turn("HISTORY", run_id="r2"))).output.startswith("turns=3 ")


async def test_every_third_compaction_rotates_with_a_handoff(env):
    h = env.harness(policy=SMALL)
    events = []

    async def hook(instance, event, info):
        events.append(event)
    h.on_lifecycle = hook
    script(env, 'CALL memory_remember {"text": "x"}', handoff="Sauna booked for 19:00; ask about it.")
    first_sid = None
    for i in range(3):
        await h.run(turn(f"msg {i}\nGROW 3000", run_id=f"r{i}"))
        first_sid = first_sid or state(env)["session_id"]
        if i == 2:
            # Before the rotation has even run, a client can see that the
            # next turn opens a new session (Mira adds her transcript tail).
            assert h.session_info("chat")["fresh"] and h.session_info("chat")["rotation_pending"]
        else:
            assert not h.session_info("chat")["fresh"]
        await settle(h)
    st = state(env)
    assert st["seq"] == 2 and st["session_id"] != first_sid
    assert st["turns"] == 0 and st["compactions"] == 0
    rot = [e for e in ledger(h) if e["event"] == "rotation"][0]
    assert rot["reason"] == "compaction_count" and rot["from_seq"] == 1 and rot["compactions"] == 2
    assert events.count("compacting") == 2 and events[-1] == "closed"
    info = h.session_info("chat")
    assert info["fresh"] and not info["rotation_pending"]
    # the next turn opens the new session with the handoff, once
    await h.run(turn("HISTORY", run_id="r9"))
    new = json.loads(fake_session(env, st["session_id"]).read_text())
    opener = new["turns"][0]
    assert opener.startswith("<handoff source=\"miragen\">")
    assert "Sauna booked for 19:00; ask about it." in opener
    assert "rotated because: compaction_count" in opener
    assert "memories written in it: 3" in opener           # 2 compaction saves + the rotation save
    assert new["rules"] == "You are Mira."                  # fresh session, same identity
    assert not h.session_info("chat")["fresh"]
    await h.run(turn("again", run_id="r10"))
    assert "<handoff" not in json.loads(fake_session(env, st["session_id"]).read_text())["turns"][-1]


async def test_handoff_is_cut_not_retried(env):
    h = env.harness(policy=ThresholdPolicy(2_000, 3_000, 3), handoff_max_chars=20)
    script(env, "", handoff="A" * 50)
    await h.run(turn("big\nGROW 5000"))                      # over the cap: forced rotation
    await settle(h)
    st = state(env)
    assert st["handoff"]["text"] == "A" * 20 and st["handoff"]["truncated"]
    assert st["handoff"]["reason"] == "context_cap"
    old = json.loads(fake_session(env, _retired(env)).read_text())
    assert sum("<session-maintenance" in t for t in old["turns"]) == 1   # asked once
    assert "cut at the size limit" in Handoff(**st["handoff"]).render()


def _retired(env) -> str:
    return state(env)["retired"][0]["session_id"]


async def test_groks_own_compaction_is_counted_and_logged(env):
    h = env.harness(policy=ThresholdPolicy(10**6, 10**7, 3))
    await h.run(turn("x\nGROW 5000\nNATIVECOMPACT"))
    assert state(env)["compactions"] == 1
    comp = [e for e in ledger(h) if e["event"] == "compaction"]
    assert comp and comp[0]["trigger"] == "grok"


async def test_a_turn_waits_for_maintenance(env):
    h = env.harness(policy=SMALL)
    script(env, "")
    await h.run(turn("x\nGROW 3000"))
    assert "chat" in h._maintenance                          # compaction scheduled
    await h.run(turn("HISTORY", run_id="r2"))
    sid = state(env)["session_id"]
    data = json.loads(fake_session(env, sid).read_text())
    # the user's second turn came after the maintenance turn and the compaction
    assert "<session-maintenance" in data["turns"][1] and data["turns"][2] == "HISTORY"
    assert data["compacted_after_turns"] == [2]


async def test_retired_sessions_are_pruned_after_retention(env):
    h = env.harness(policy=ThresholdPolicy(2_000, 3_000, 3), session_retention_days=0)
    await h.run(turn("x\nGROW 5000"))
    await settle(h)
    old_sid = [e for e in ledger(h) if e["event"] == "session_pruned"][0]["session_id"]
    assert not (h.session_dir("chat") / old_sid).exists()
    assert (h.session_dir("chat") / state(env)["session_id"]).exists()
    assert state(env)["retired"] == []


async def test_lifecycle_off_changes_nothing(env):
    h = env.harness(policy=SMALL, lifecycle=False)
    await h.run(turn("x\nGROW 50000"))
    assert h._maintenance == {} and not h.ledger.read("chat")


async def test_http_rotate_and_session_endpoints(env, tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    import miragen.app as app_module
    from miragen.runs import RunStore
    from tests.test_grok_harness import profile
    h = env.harness(policy=SMALL)
    monkeypatch.setattr(app_module, "HISTORIES_DIR", tmp_path / "histories")
    app_module._profile, app_module._agent, app_module._harness = profile(), None, h
    app_module._run_store = RunStore(root=tmp_path / "runs")
    script(env, "", handoff="fresh start requested")
    try:
        async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://t") as c:
            assert (await c.post("/instances/chat/rotate")).status_code == 404
            await c.post("/run", json={"prompt": "hi", "use_history": True, "instance": "chat"})
            s = (await c.get("/instances/chat/session")).json()
            assert s["seq"] == 1 and not s["fresh"]
            r = await c.post("/instances/chat/rotate")
            assert r.status_code == 200 and r.json()["seq"] == 2 and r.json()["fresh"]
            listed = (await c.get("/instances")).json()["instances"]
            assert [i["session"]["seq"] for i in listed if i["name"] == "chat"] == [2]
            out = await c.post("/run", json={"prompt": "HISTORY", "use_history": True,
                                             "instance": "chat"})
            assert "fresh start requested" in out.json()["output"]   # the handoff opened it
            assert "\nturns=1 " in out.json()["output"]
            assert not (await c.get("/instances/chat/session")).json()["fresh"]
    finally:
        app_module._harness = app_module._profile = app_module._run_store = None
        app_module._busy_instances.clear()


async def test_bridge_sessions_follow_rotation():
    import httpx

    from miragen.memory.bridge import BridgeMemory
    from miragen.models import MemorySpec
    posted = []

    def handler(req):
        posted.append(json.loads(req.content))
        return httpx.Response(200, json={})
    seq = {"chat": 1}
    b = BridgeMemory(spec=MemorySpec(backend="bridge", endpoint_env="U", credential_env="T",
                                     project="github.com/x/mira"),
                     agent_name="mira", base_url="http://plane", token=None,
                     transport=httpx.MockTransport(handler), session_seq=lambda i: seq[i])
    await b.prepare(instance="chat", run_id="r1", prompt="hi")
    await b.prepare(instance="chat", run_id="r1b", prompt="more")   # same session: no re-open
    await b.lifecycle("chat", "compacting", {"trigger": "miragen"})
    await b.lifecycle("chat", "closed", {"reason": "compaction_count"})
    seq["chat"] = 2
    await b.prepare(instance="chat", run_id="r2", prompt="hi again")
    names = [(p["session_id"], p["event"]["name"]) for p in posted]
    assert names == [("mira-chat", "context.started"), ("mira-chat", "input.received"),
                     ("mira-chat", "input.received"), ("mira-chat", "context.compacting"), ("mira-chat", "context.closed"),
                     ("mira-chat-s2", "context.started"), ("mira-chat-s2", "input.received")]
    assert b.session_key("chat") == "miragen:mira-chat-s2"


async def test_gateway_records_result_status_never_content(env):
    h = env.harness()
    res = await h.run(turn('CALL memory_remember {"text": "secret"}'))
    rec = res.tool_calls[0]
    assert rec.result_status == "accepted"
    assert "record_id" not in rec.model_dump_json()
    await asyncio.sleep(0)
