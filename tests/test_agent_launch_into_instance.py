"""Durable, idempotent launches into a named base-tier instance, and
spec.instructions_file."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import miragen.app as app_module
from miragen.app import app
from miragen.load import load_profile
from miragen.models import AgentProfile
from miragen.runs import RunStore


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "HISTORIES_DIR", tmp_path / "histories")
    app_module._profile = AgentProfile.model_validate({
        "name": "a", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "test", "instructions": "hi"}})
    result = MagicMock()
    result.output = "ok"
    result.usage.requests = 1
    result.usage.input_tokens = None
    result.usage.output_tokens = None
    result.all_messages.return_value = []
    agent = MagicMock()
    agent.run = AsyncMock(return_value=result)
    app_module._agent = agent
    app_module._run_store = RunStore(root=tmp_path / "runs")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, agent
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._busy_instances.clear()
    app_module._active_runs = 0


async def _wait(c, run_id):
    for _ in range(200):
        rec = (await c.get(f"/runs/{run_id}")).json()
        if rec["status"] != "running":
            return rec
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


async def test_launch_into_instance_with_history(client):
    c, agent = client
    body = {"prompt": "p", "idempotency_key": "k1", "instance": "tg-111", "use_history": True}
    r = await c.post("/executor-runs", json=body)
    assert r.status_code == 202, r.text
    rec = await _wait(c, r.json()["run_id"])
    assert rec["status"] == "succeeded" and rec["instance"] == "tg-111" and rec["use_history"] is True
    dup = await c.post("/executor-runs", json=body)
    assert dup.status_code == 200 and dup.json()["duplicate"] is True
    assert agent.run.await_count == 1


async def test_busy_instance_answers_429(client):
    c, _ = client
    app_module._busy_instances.add("tg-111")
    r = await c.post("/executor-runs", json={"prompt": "p", "idempotency_key": "k2",
                                             "instance": "tg-111", "use_history": True})
    assert r.status_code == 429


async def test_bad_instance_name_is_rejected(client):
    c, _ = client
    r = await c.post("/executor-runs", json={"prompt": "p", "idempotency_key": "k3",
                                             "instance": "Not Valid!"})
    assert r.status_code == 422


def test_instructions_file_is_resolved_by_the_loader(tmp_path):
    (tmp_path / "mira.md").write_text("# Mira\nYou are Mira.\n")
    (tmp_path / "agent.yaml").write_text(
        "name: m\nmode: interactive\ntriggers: [{type: http}]\n"
        "spec:\n  model: test\n  instructions_file: mira.md\n")
    p = load_profile(tmp_path / "agent.yaml")
    assert p.spec.instructions == "# Mira\nYou are Mira." and p.spec.instructions_file is None


@pytest.mark.parametrize("spec,match", [
    ({"model": "test"}, "instructions"),
    ({"model": "test", "instructions": "a", "instructions_file": "b.md"}, "not both"),
])
def test_instructions_sources_are_validated(spec, match):
    with pytest.raises(Exception, match=match):
        AgentProfile.model_validate({"name": "m", "mode": "interactive",
                                     "triggers": [{"type": "http"}], "spec": spec})


def test_missing_instructions_file_fails_loudly(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: m\nmode: interactive\ntriggers: [{type: http}]\n"
        "spec:\n  model: test\n  instructions_file: nope.md\n")
    with pytest.raises(ValueError, match="not readable"):
        load_profile(tmp_path / "agent.yaml")


async def test_turns_endpoint_is_the_base_tier_name_for_instance_launches(client):
    c, agent = client
    body = {"prompt": "p", "idempotency_key": "t1"}
    r = await c.post("/instances/tg-111/turns", json=body)
    assert r.status_code == 202, r.text
    turn_id = r.json()["turn_id"]
    assert r.json()["instance"] == "tg-111"
    rec = await _wait(c, turn_id)
    assert rec["status"] == "succeeded" and rec["instance"] == "tg-111" and rec["use_history"] is True
    # the same record under the turn's own path, only in its instance
    assert (await c.get(f"/instances/tg-111/turns/{turn_id}")).json()["run_id"] == turn_id
    assert (await c.get(f"/instances/other/turns/{turn_id}")).status_code == 404
    # a retried key is the same turn, not a second one
    dup = await c.post("/instances/tg-111/turns", json=body)
    assert dup.status_code == 200 and dup.json()["duplicate"] is True
    assert dup.json()["turn_id"] == turn_id and agent.run.await_count == 1
    assert (await c.post("/instances/NOT..VALID/turns", json={"prompt": "p",
                                                               "idempotency_key": "t2"})).status_code == 422
