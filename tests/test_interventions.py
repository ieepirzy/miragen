"""Structured interventions + target event provenance (issue #33 Phase G) —
the sentinel-file question mechanism, the intervention suspension state, the
structured answer/resume flow bound by intervention_id, and the target
provenance envelope fields.
"""

import json
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import miragen.app  # noqa: F401

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.executor.base import _EventWriter
from miragen.models import InterventionAnswer, TargetOperationProvenance
from miragen.runs import RunStore

from tests.test_executor import StubThread, _executor_profile, default_events, make_executor

QUESTION = {
    "question": "Should the retry queue live in Postgres or Redis?",
    "kind": "architecture-decision",
    "options": [
        {"id": "postgres", "label": "Postgres outbox"},
        {"id": "redis", "label": "Redis stream"},
    ],
    "evidence": "See notes in docs/queue.md",
}


def _executor_with_intervention(tmp_path, run_id, payload=QUESTION, raw_text=None):
    """Executor whose stubbed turn writes the sentinel file mid-turn."""
    profile = _executor_profile()
    # set before computing ws — make_executor points workspace_root here too
    profile.executor.workspace_root = str(tmp_path / "workspaces")
    ws = Path(profile.executor.workspace_root) / run_id

    def touch():
        marker = ws / ".miragen"
        marker.mkdir(parents=True, exist_ok=True)
        (marker / "intervention.json").write_text(
            raw_text if raw_text is not None else json.dumps(payload)
        )

    executor = make_executor(profile, tmp_path, thread=StubThread(default_events(), touch=touch))
    return profile, executor


# ── Sentinel detection in run_job ────────────────────────────────────────────


async def test_valid_sentinel_suspends_with_intervention(tmp_path):
    _, executor = _executor_with_intervention(tmp_path, "iv-1")
    result = await executor.run_job("build the queue", "iv-1")
    assert result.status == "suspended"
    assert result.exit_reason == "intervention"
    assert result.diff_path is None  # question beats harvest — partial work is resume state
    assert result.thread_id == "thr_stub123"

    intervention = result.intervention
    assert intervention["question"] == QUESTION["question"]
    assert intervention["kind"] == "architecture-decision"
    assert [o["id"] for o in intervention["options"]] == ["postgres", "redis"]
    assert len(intervention["intervention_id"]) == 32  # miragen-assigned, never agent-chosen

    events = executor.read_events("iv-1", limit=1000)
    (requested,) = [e for e in events if e["type"] == "intervention.requested"]
    assert requested["intervention_id"] == intervention["intervention_id"]

    # file archived: a resumed turn must not re-trigger the same question
    ws = Path(executor.spec.workspace_root) / "iv-1"
    assert not (ws / ".miragen" / "intervention.json").exists()
    assert (ws / ".miragen" / "interventions" / f"{intervention['intervention_id']}.json").exists()

    # resume with a turn that asks nothing further
    executor._thread_factory = lambda **kw: StubThread(default_events())
    resumed = await executor.run_job(
        "continue", "iv-1", thread_id=result.thread_id, first_turn=False,
    )
    assert resumed.status == "succeeded" and resumed.intervention is None


async def test_agent_supplied_id_is_discarded(tmp_path):
    _, executor = _executor_with_intervention(
        tmp_path, "iv-id", payload={**QUESTION, "intervention_id": "spoofed", "requested_at": "1999"},
    )
    result = await executor.run_job("go", "iv-id")
    assert result.intervention["intervention_id"] != "spoofed"
    assert result.intervention["requested_at"] != "1999"


async def test_malformed_sentinel_is_set_aside_not_parsed(tmp_path):
    _, executor = _executor_with_intervention(tmp_path, "iv-bad", raw_text="ask the human something?")
    result = await executor.run_job("go", "iv-bad")
    # schema IS the contract: no prose fallback — the run proceeds normally
    assert result.status == "succeeded"
    assert result.intervention is None
    events = executor.read_events("iv-bad", limit=1000)
    assert any(e["type"] == "intervention.invalid" for e in events)
    ws = Path(executor.spec.workspace_root) / "iv-bad"
    assert (ws / ".miragen" / "intervention.invalid.json").exists()
    assert not (ws / ".miragen" / "intervention.json").exists()


async def test_question_is_required(tmp_path):
    _, executor = _executor_with_intervention(tmp_path, "iv-noq", payload={"kind": "confirmation"})
    result = await executor.run_job("go", "iv-noq")
    assert result.status == "succeeded" and result.intervention is None


# ── Answer/resume flow over the API ──────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._executor = None


@pytest.fixture
async def intervention_client(tmp_path):
    profile, executor = _executor_with_intervention(tmp_path, "will-be-set")
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _suspend_on_question(client, tmp_path):
    """Run once; the stub writes the sentinel for whatever run_id is used."""
    # rebuild the touch closure around the actual run workspace
    profile = app_module._profile
    thread = StubThread(default_events())
    app_module._executor._thread_factory = lambda **kw: thread

    async def run_and_intervene():
        resp = await client.post("/run", json={"prompt": "decide the queue"})
        return resp

    # write the sentinel via a touch that knows the run's workspace lazily:
    def touch():
        # workspace dir == workspace_root / run_id; the only dir there
        root = Path(profile.executor.workspace_root)
        (ws,) = [p for p in root.iterdir() if p.is_dir()]
        marker = ws / ".miragen"
        marker.mkdir(exist_ok=True)
        (marker / "intervention.json").write_text(json.dumps(QUESTION))

    thread._touch = touch
    resp = await run_and_intervene()
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    record = app_module._run_store.get(run_id)
    assert record.status == "suspended" and record.exit_reason == "intervention"
    assert record.pending_intervention is not None
    return record


async def test_answer_resume_binds_id_and_records_event(intervention_client, tmp_path):
    record = await _suspend_on_question(intervention_client, tmp_path)
    pending_id = record.pending_intervention.intervention_id

    # wrong id → 409, nothing consumed
    resp = await intervention_client.post(f"/runs/{record.run_id}/resume", json={
        "answer": {"intervention_id": "deadbeef", "decision": "postgres"},
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["pending_intervention_id"] == pending_id

    # matching answer resumes on the same thread; miragen renders the prompt
    thread = StubThread(default_events())
    app_module._executor._thread_factory = lambda **kw: thread
    resp = await intervention_client.post(f"/runs/{record.run_id}/resume", json={
        "answer": {
            "intervention_id": pending_id,
            "decision": "postgres",
            "text": "Use the outbox pattern discussed earlier.",
            "approval_ref": "appr_777",
            "answered_by": "ilari",
        },
    })
    assert resp.status_code == 200, resp.text
    resumed = resp.json()
    assert resumed["status"] == "succeeded"
    assert resumed["pending_intervention"] is None

    # deterministic rendering, since no prompt was supplied
    (sent_prompt,) = thread.prompts
    assert pending_id in sent_prompt
    assert "Decision: postgres (Postgres outbox)" in sent_prompt
    assert "outbox pattern" in sent_prompt

    # the answer — including the approval reference — is durable API state
    events = app_module._executor.read_events(record.run_id, limit=1000)
    (answered,) = [e for e in events if e["type"] == "intervention.answered"]
    assert answered["intervention_id"] == pending_id
    assert answered["answer"]["approval_ref"] == "appr_777"
    assert answered["answer"]["answered_by"] == "ilari"
    # ordered strictly after the request in the same sequenced stream
    (requested,) = [e for e in events if e["type"] == "intervention.requested"]
    assert answered["seq"] > requested["seq"]


async def test_plain_prompt_resume_supersedes_pending_question(intervention_client, tmp_path):
    record = await _suspend_on_question(intervention_client, tmp_path)
    pending_id = record.pending_intervention.intervention_id

    app_module._executor._thread_factory = lambda **kw: StubThread(default_events())
    resp = await intervention_client.post(f"/runs/{record.run_id}/resume", json={
        "prompt": "Forget the question, just ship it with Postgres.",
    })
    assert resp.status_code == 200
    assert resp.json()["pending_intervention"] is None
    events = app_module._executor.read_events(record.run_id, limit=1000)
    (superseded,) = [e for e in events if e["type"] == "intervention.superseded"]
    assert superseded["intervention_id"] == pending_id


async def test_answer_without_pending_intervention_is_409(intervention_client, tmp_path):
    # suspend via budget, not intervention
    app_module._profile.limits = type(app_module._profile).model_validate({
        **json.loads(app_module._profile.model_dump_json()),
        "limits": {"tokens_per_run": 1},
    }).limits
    app_module._executor._thread_factory = lambda **kw: StubThread(default_events())
    resp = await intervention_client.post("/run", json={"prompt": "big"})
    run_id = resp.json()["run_id"]
    assert app_module._run_store.get(run_id).status == "suspended"

    resp = await intervention_client.post(f"/runs/{run_id}/resume", json={
        "answer": {"intervention_id": "abc", "text": "hello"},
    })
    assert resp.status_code == 409
    assert "no pending intervention" in resp.json()["detail"]


async def test_resume_requires_prompt_or_answer(intervention_client, tmp_path):
    record = await _suspend_on_question(intervention_client, tmp_path)
    resp = await intervention_client.post(f"/runs/{record.run_id}/resume", json={})
    assert resp.status_code == 422


def test_answer_requires_decision_or_text():
    with pytest.raises(ValidationError, match="decision.*text|at least one"):
        InterventionAnswer(intervention_id="x")
    assert InterventionAnswer(intervention_id="x", decision="a").decision == "a"


# ── Target provenance envelope fields ────────────────────────────────────────


def test_target_provenance_model_contract():
    target = TargetOperationProvenance.model_validate({
        "target_id": "tgt_1",
        "target_name": "staging-db",
        "operation_class": "write",
        "credential_grant_ref": "grant_9",
        "approval_ref": "appr_3",
        "adapter_specific": "kept",
    })
    assert target.operation_class == "write"
    assert target.model_dump()["adapter_specific"] == "kept"
    with pytest.raises(ValidationError):
        TargetOperationProvenance.model_validate({"target_id": "t", "operation_class": "yolo"})


def test_events_carry_target_provenance_through_the_envelope(tmp_path):
    """Phase C leftover: target provenance fields ride the flat envelope
    without requiring targets to exist — any event MAY carry `target`."""
    path = tmp_path / "t.events.jsonl"
    with _EventWriter(path) as sink:
        sink.write({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "psql ..."},
            "target": {
                "target_id": "tgt_1",
                "operation_class": "read",
                "credential_grant_ref": "grant_9",
            },
        })
    (line,) = path.read_text().splitlines()
    event = json.loads(line)
    assert event["seq"] == 1 and event["schema"] == "miragen/executor-event/v1"
    parsed = TargetOperationProvenance.model_validate(event["target"])
    assert parsed.target_id == "tgt_1" and parsed.operation_class == "read"
