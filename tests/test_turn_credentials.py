"""A base-tier turn's run-scoped credentials (MiraRun ADR-021, ADR-045 §5):
they reach the run, never the run record or the logs, and end with the turn.
The tool gateway uses them for the matching upstream during that turn only."""

from __future__ import annotations

import asyncio
import logging

import pytest
from httpx import ASGITransport, AsyncClient

import miragen.app as app_module
from miragen.app import app
from miragen.harness.gateway import ToolGateway
from miragen.models import AgentProfile
from miragen.runs import RunStore
from tests.test_tool_gateway import memory_client_factory, text, upstream_server

SECRET = "run-scoped-bearer-7f3a9c"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "HISTORIES_DIR", tmp_path / "histories")
    app_module._profile = AgentProfile.model_validate({
        "name": "a", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "test", "instructions": "hi"}})
    app_module._run_store = RunStore(root=tmp_path / "runs")
    seen: dict = {}

    async def fake_run_agent(prompt, *, record=None, repositories=None, mcp_secret_env=None,
                             use_history=False, instance=None):
        seen["value"] = dict(mcp_secret_env or {})
        seen["dict"] = mcp_secret_env
        logging.getLogger("miragen.test").info("running %s", prompt)
        app_module._run_store.finish(record, status="succeeded", output="ok")
        return "ok"

    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)
    monkeypatch.setattr(app_module, "_model_ready", lambda: True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, seen, tmp_path
    app_module._profile = None
    app_module._run_store = None
    app_module._busy_instances.clear()
    app_module._active_runs = 0


async def _finished(c, run_id):
    for _ in range(200):
        rec = (await c.get(f"/runs/{run_id}")).json()
        if rec["status"] != "running":
            return rec
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


def _turn(**extra):
    return {"prompt": "p", "idempotency_key": "k-1", **extra}


async def test_credentials_reach_the_run_and_never_the_record_or_the_logs(client, caplog):
    c, seen, tmp_path = client
    caplog.set_level(logging.DEBUG)
    r = await c.post("/instances/main/turns",
                     json=_turn(credentials={"secret_env": {"MIRARUN_MCP_TOKEN": SECRET}}))
    assert r.status_code == 202, r.text
    record = await _finished(c, r.json()["turn_id"])
    assert seen["value"] == {"MIRARUN_MCP_TOKEN": SECRET}
    # Never persisted: not in the record the API serves, nor in any file
    # the run store or the histories wrote.
    assert SECRET not in str(record)
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written, "the run store wrote nothing to check"
    for path in written:
        assert SECRET not in path.read_text(errors="replace"), path
    # Never logged.
    assert SECRET not in caplog.text
    # Dropped when the turn ended.
    assert seen["dict"] == {}


async def test_repository_bindings_are_refused_on_the_base_tier(client):
    c, seen, _ = client
    r = await c.post("/instances/main/turns",
                     json=_turn(credentials={"repositories": {"app": {"clone_url": "x"}}}))
    assert r.status_code == 422
    assert "value" not in seen


async def test_a_bad_variable_name_is_refused(client):
    c, _, _ = client
    r = await c.post("/instances/main/turns",
                     json=_turn(credentials={"secret_env": {"not-a-name": SECRET}}))
    assert r.status_code == 422
    assert SECRET not in r.text


async def test_the_executor_route_does_not_take_turn_credentials(client):
    c, seen, _ = client
    r = await c.post("/executor-runs", json=_turn(
        instance="main", use_history=True,
        credentials={"secret_env": {"MIRARUN_MCP_TOKEN": SECRET}}))
    assert r.status_code == 202, r.text
    await _finished(c, r.json()["run_id"])
    assert seen["value"] == {}


def _profile(**cap) -> AgentProfile:
    return AgentProfile.model_validate({
        "name": "g", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "grok-build:x", "instructions": "i",
                 "capabilities": [{"MCP": {"url": "https://db.example/mcp", "name": "db", **cap}}]}})


async def test_the_gateway_uses_the_turns_credential_for_that_turn_only():
    seen = []
    gw = ToolGateway(_profile(bearer_token_env="DB_TOKEN"), env={"DB_TOKEN": "deployment"},
                     upstream_client=memory_client_factory(upstream_server(), seen))
    with gw.turn("conv_1", "r1", secret_env={"DB_TOKEN": SECRET}) as log:
        res = await gw.call_tool("db_lookup", {"key": "k"}, instance="conv_1")
        assert text(res) == "value-of-k"
        assert SECRET not in repr(log)
    assert seen[-1][1]["Authorization"] == f"Bearer {SECRET}"
    assert log.secret_env is None
    with gw.turn("conv_1", "r2"):
        await gw.call_tool("db_lookup", {"key": "k"}, instance="conv_1")
    assert seen[-1][1]["Authorization"] == "Bearer deployment"
