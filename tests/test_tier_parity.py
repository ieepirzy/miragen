"""Tier split-brain closures: the shared run surface must behave the same —
or fail loudly — regardless of which backend a profile declares.

Covers: use_history rejected (not ignored) on executor runs, tool-call
aggregates populated for model-tier runs, grok-build headless rejecting MCP
injection at validation, and the unified /runs/{id}/events log for both tiers.
"""

import sys

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import miragen.app  # noqa: F401

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.models import AgentProfile
from miragen.runs import RunStore

from tests.test_executor import StubThread, _executor_profile, default_events, make_executor
from tests.test_model_tier_stub import StubAgent, model_profile


@pytest.fixture(autouse=True)
def reset_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._executor = None


@pytest.fixture
async def executor_client(tmp_path):
    profile = _executor_profile()
    executor = make_executor(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def model_client(tmp_path):
    app_module._profile = model_profile()
    app_module._agent = StubAgent()
    app_module._executor = None
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── use_history: loud rejection, not a silent no-op ──────────────────────────


async def test_executor_run_rejects_use_history(executor_client):
    resp = await executor_client.post("/run", json={"prompt": "go", "use_history": True})
    assert resp.status_code == 400
    assert "use_history" in str(resp.json()["detail"])


async def test_executor_run_async_rejects_use_history(executor_client):
    resp = await executor_client.post("/run/async", json={"prompt": "go", "use_history": True})
    assert resp.status_code == 400


async def test_executor_run_without_history_still_works(executor_client):
    resp = await executor_client.post("/run", json={"prompt": "go"})
    assert resp.status_code == 200


# ── Tool-call aggregates: populated for BOTH tiers ───────────────────────────


async def test_model_tier_run_populates_tool_call_aggregates(model_client):
    resp = await model_client.post("/run", json={"prompt": "go"})
    assert resp.status_code == 200
    record = app_module._run_store.get(resp.json()["run_id"])
    # StubAgent's message walk yields two tool calls, one failed.
    assert len(record.tool_calls) == 2
    assert record.tool_call_count == 2
    assert record.tool_call_failures == 1


# ── grok-build headless: MCP injection is dead config → loud validation ──────


def test_grok_headless_rejects_mcp_servers():
    with pytest.raises(ValidationError, match="acp"):
        AgentProfile.model_validate({
            "name": "grok-worker",
            "mode": "interactive",
            "triggers": [{"type": "http"}],
            "executor": {
                "executor": "grok-build",
                "instructions": "fix",
                "mcp_servers": [{"name": "loimi", "url": "https://loimi.mesh/mcp/"}],
            },
        })


def test_grok_acp_accepts_mcp_servers():
    profile = AgentProfile.model_validate({
        "name": "grok-worker",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "executor": {
            "executor": "grok-build",
            "instructions": "fix",
            "grok_transport": "acp",
            "mcp_servers": [{"name": "loimi", "url": "https://loimi.mesh/mcp/"}],
        },
    })
    assert profile.executor.grok_transport == "acp"


# ── Unified event log: /runs/{id}/events serves BOTH tiers ───────────────────


async def test_model_tier_run_writes_events_and_serves_them(model_client):
    resp = await model_client.post("/run", json={"prompt": "go"})
    run_id = resp.json()["run_id"]

    events_resp = await model_client.get(f"/runs/{run_id}/events")
    assert events_resp.status_code == 200
    body = events_resp.json()
    types = [e["type"] for e in body["events"]]
    assert "turn.completed" in types
    assert types.count("item.completed") >= 2  # the two tool calls
    # Same envelope contract as the executor stream: monotonic seq + schema.
    seqs = [e["seq"] for e in body["events"]]
    assert seqs == sorted(seqs)
    assert all(e["schema"] for e in body["events"])


async def test_model_tier_events_cursor_read(model_client):
    resp = await model_client.post("/run", json={"prompt": "go"})
    run_id = resp.json()["run_id"]
    page = (await model_client.get(f"/runs/{run_id}/events", params={"after": 0, "limit": 2})).json()
    assert page["count"] == 2
    assert page["has_more"] is True
    rest = (await model_client.get(
        f"/runs/{run_id}/events", params={"after": page["next_after"], "limit": 100}
    )).json()
    assert rest["events"][0]["seq"] == page["next_after"] + 1


async def test_executor_events_endpoint_unchanged(executor_client):
    resp = await executor_client.post("/run", json={"prompt": "go"})
    run_id = resp.json()["run_id"]
    events_resp = await executor_client.get(f"/runs/{run_id}/events")
    assert events_resp.status_code == 200
    assert any(e["type"] == "turn.completed" for e in events_resp.json()["events"])


async def test_model_tier_failed_run_still_writes_events(model_client, tmp_path):
    app_module._agent = StubAgent(raise_exc=RuntimeError("model exploded"))
    resp = await model_client.post("/run", json={"prompt": "go"})
    assert resp.status_code == 500
    run_id = app_module._run_store.list(limit=1)[0].run_id
    body = (await model_client.get(f"/runs/{run_id}/events")).json()
    assert any(e["type"] == "turn.failed" for e in body["events"])


async def test_model_tier_failed_stream_still_writes_events(model_client):
    class _FailingStreamAgent:
        def run_stream(self, prompt, usage_limits=None, message_history=None):
            class _Ctx:
                async def __aenter__(self):
                    raise RuntimeError("stream exploded")

                async def __aexit__(self, *exc):
                    return False

            return _Ctx()

    app_module._agent = _FailingStreamAgent()
    # The exception escapes mid-stream, so it surfaces while the body is read
    # rather than as a status code.
    with pytest.raises(Exception):
        await model_client.post("/run/stream", json={"prompt": "go"})
    record = app_module._run_store.list(limit=1)[0]
    assert record.status == "failed"
    body = (await model_client.get(f"/runs/{record.run_id}/events")).json()
    assert any(e["type"] == "turn.failed" for e in body["events"])
