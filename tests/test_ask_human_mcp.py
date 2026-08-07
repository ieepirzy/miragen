"""The ask_human MCP tool (miragen/intervention_mcp.py): the layered-on
front door to structured interventions. The contract under test is the
design doc's: the tool writes exactly the workspace sentinel file, and the
base tier's existing end-of-turn machinery does everything else.
"""

import json
import sys

import pytest
from httpx import ASGITransport, AsyncClient

import miragen.app  # noqa: F401

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.intervention_mcp import AskHumanError, build_ask_human_mcp, record_ask_human
from miragen.runs import RunStore

from tests.test_executor import StubThread, _executor_profile, default_events, make_executor


@pytest.fixture(autouse=True)
def reset_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._executor = None


@pytest.fixture
def wired(tmp_path):
    profile = _executor_profile()
    executor = make_executor(profile, tmp_path)
    store = RunStore(root=tmp_path / "runs", retention=50)
    return store, executor


def _start_running(store, *, workspace=None):
    record = store.start(agent_name="codex-worker", trigger="http", prompt="go")
    if workspace is not None:
        record = store.annotate(record, workspace=str(workspace))
    return record


# ── Sentinel writing + run resolution ────────────────────────────────────────


def test_writes_sentinel_for_single_running_run(wired, tmp_path):
    store, executor = wired
    ws = tmp_path / "ws"
    record = _start_running(store, workspace=ws)

    message = record_ask_human(
        run_store=store,
        executor=executor,
        question="Postgres or Redis?",
        kind="architecture-decision",
        options=[{"id": "postgres", "label": "Postgres outbox"}],
        evidence="docs/queue.md",
    )
    assert record.run_id in message

    sentinel = json.loads((ws / ".miragen" / "intervention.json").read_text())
    assert sentinel == {
        "question": "Postgres or Redis?",
        "kind": "architecture-decision",
        "options": [{"id": "postgres", "label": "Postgres outbox"}],
        "evidence": "docs/queue.md",
    }


def test_defaults_workspace_from_run_id(wired):
    store, executor = wired
    record = _start_running(store)  # record.workspace not yet set (mid-turn)
    record_ask_human(run_store=store, executor=executor, question="Q?")
    from pathlib import Path

    sentinel = Path(executor.spec.workspace_root) / record.run_id / ".miragen" / "intervention.json"
    assert json.loads(sentinel.read_text())["question"] == "Q?"


def test_refuses_when_no_running_run(wired):
    store, executor = wired
    with pytest.raises(AskHumanError, match="intervention.json"):
        record_ask_human(run_store=store, executor=executor, question="Q?")


def test_refuses_empty_question(wired):
    store, executor = wired
    _start_running(store)
    with pytest.raises(AskHumanError, match="question"):
        record_ask_human(run_store=store, executor=executor, question="   ")


def test_multiple_running_requires_run_id(wired, tmp_path):
    store, executor = wired
    a = _start_running(store, workspace=tmp_path / "a")
    _start_running(store, workspace=tmp_path / "b")

    with pytest.raises(AskHumanError, match="run_id"):
        record_ask_human(run_store=store, executor=executor, question="Q?")

    # A unique prefix resolves, same as the HTTP API's run lookup.
    record_ask_human(run_store=store, executor=executor, question="Q?", run_id=a.run_id[:8])
    assert (tmp_path / "a" / ".miragen" / "intervention.json").exists()
    assert not (tmp_path / "b" / ".miragen" / "intervention.json").exists()


def test_unknown_run_id_rejected(wired, tmp_path):
    store, executor = wired
    _start_running(store, workspace=tmp_path / "a")
    with pytest.raises(AskHumanError, match="no running run"):
        record_ask_human(run_store=store, executor=executor, question="Q?", run_id="nope")


def test_one_question_per_turn(wired, tmp_path):
    store, executor = wired
    _start_running(store, workspace=tmp_path / "ws")
    record_ask_human(run_store=store, executor=executor, question="first?")
    with pytest.raises(AskHumanError, match="already pending"):
        record_ask_human(run_store=store, executor=executor, question="second?")


# ── The written file drives the EXISTING suspension machinery ────────────────


async def test_mcp_written_sentinel_suspends_run_end_to_end(tmp_path):
    """An executor turn during which ask_human fires must end suspended with
    pending_intervention — through the base tier's own sentinel detection,
    exactly as if the agent had written the file."""
    profile = _executor_profile()
    executor = make_executor(profile, tmp_path)
    store = RunStore(root=tmp_path / "runs", retention=50)
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = store

    def agent_calls_ask_human():
        record_ask_human(
            run_store=store, executor=executor,
            question="Which retry queue?", kind="architecture-decision",
        )

    executor._session_factory = StubThread(default_events(), touch=agent_calls_ask_human)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "build it"})
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        record = store.get(run_id)
        assert record.status == "suspended"
        assert record.exit_reason == "intervention"
        assert record.pending_intervention.question == "Which retry queue?"

        events = (await client.get(f"/runs/{run_id}/events")).json()["events"]
        assert any(e["type"] == "intervention.requested" for e in events)


# ── FastMCP tool surface ─────────────────────────────────────────────────────


async def test_mcp_tool_calls_through_to_recorder(wired, tmp_path):
    store, executor = wired
    _start_running(store, workspace=tmp_path / "ws")
    mcp = build_ask_human_mcp(lambda: (store, executor))
    blocks, structured = await mcp.call_tool("ask_human", {"question": "Deploy now?"})
    assert "End your turn" in structured["result"]
    assert (tmp_path / "ws" / ".miragen" / "intervention.json").exists()


async def test_mcp_tool_without_executor_state_errors():
    mcp = build_ask_human_mcp(lambda: (None, None))
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises((AskHumanError, ToolError), match="executor"):
        await mcp.call_tool("ask_human", {"question": "Q?"})


# ── Mounted endpoint guard ───────────────────────────────────────────────────


async def test_mount_returns_503_outside_lifespan():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/mcp/ask-human/", json={})
        assert resp.status_code == 503


async def test_mount_enforces_internal_token(monkeypatch):
    monkeypatch.setenv("MIRAGEN_INTERNAL_TOKEN", "sekrit")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/mcp/ask-human/", json={})
        assert resp.status_code == 401
        # Bearer form — how ExecutorMCPServer.bearer_token_env arrives.
        resp = await client.post(
            "/mcp/ask-human/", json={}, headers={"Authorization": "Bearer sekrit"}
        )
        assert resp.status_code == 503  # authorized; MCP just isn't running here
