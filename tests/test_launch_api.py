"""POST /profiles/resolve + POST /executor-runs (issue #33 Phases A/B) —
resolve-without-run, durable idempotent launch, provenance persistence,
snapshot retrieval/reproduction, and the crash-recovery contract.
"""

import asyncio
import sys

import pytest
from httpx import ASGITransport, AsyncClient

import miragen.app  # noqa: F401 — ensure module is registered in sys.modules

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.models import RunProvenance
from miragen.runs import RunStore

from tests.test_edf import default_dev_edf, minimal_edf
from tests.test_executor import _executor_profile, make_executor


@pytest.fixture(autouse=True)
def reset_app_state():
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


async def wait_terminal(run_id: str):
    record = app_module._run_store.get(run_id)
    for _ in range(200):
        if record.status != "running":
            return record
        await asyncio.sleep(0)
        record = app_module._run_store.get(run_id)
    return record


# ── /profiles/resolve ────────────────────────────────────────────────────────


async def test_resolve_returns_canonical_output_without_starting_a_run(executor_client):
    resp = await executor_client.post("/profiles/resolve", json={"edf": default_dev_edf()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["sha256"]) == 64
    assert body["resolved_profile"]["executor"]["executor"] == "codex"
    assert body["repository_plan"][0]["commit"] is None
    assert body["agent_compatibility"]["compatible"] is True
    assert app_module._run_store.list() == []  # no run started


async def test_resolve_reports_structured_validation_errors(executor_client):
    edf = default_dev_edf()
    edf["spec"]["executor"]["budget"]["tokensPerRun"] = 0
    resp = await executor_client.post("/profiles/resolve", json={"edf": edf})
    assert resp.status_code == 422
    (error,) = resp.json()["detail"]["errors"]
    assert error["loc"] == "spec.executor.budget.tokensPerRun"


async def test_resolve_hash_is_reproducible_from_snapshot_canonical(executor_client):
    """Acceptance criterion: obtain and later REPRODUCE the canonical
    snapshot/hash — resolving the canonical document again yields the same
    hash."""
    first = (await executor_client.post("/profiles/resolve", json={"edf": default_dev_edf()})).json()
    second = (await executor_client.post("/profiles/resolve", json={"edf": first["canonical"]})).json()
    assert second["sha256"] == first["sha256"]
    assert second["canonical"] == first["canonical"]


async def test_resolve_reports_agent_incompatibility(executor_client):
    resp = await executor_client.post("/profiles/resolve", json={"edf": minimal_edf(kind="claude-code")})
    assert resp.status_code == 200
    compatibility = resp.json()["agent_compatibility"]
    assert compatibility["compatible"] is False
    assert any("claude-code" in issue for issue in compatibility["issues"])


# ── /executor-runs: launch + provenance ──────────────────────────────────────


async def test_launch_persists_snapshot_and_provenance(executor_client):
    # Repository-less EDF: the provenance/snapshot flow needs no bindings.
    # Repository launches (bindings, commits, per-repo diffs) are covered in
    # tests/test_multi_repo.py.
    edf = minimal_edf()
    resolved = (await executor_client.post("/profiles/resolve", json={"edf": edf})).json()
    resp = await executor_client.post("/executor-runs", json={
        "prompt": "fix the flaky test",
        "idempotency_key": "mirarun:run-intent-001",
        "edf": edf,
        "expected_sha256": resolved["sha256"],
        "provenance": {
            "environment_id": "env_42",
            "environment_revision": "7",
            "invocation_id": "inv_9",
            "requested_by": "user_1",
            "custom_product_field": "kept-verbatim",
        },
    })
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    assert body["snapshot_sha256"] == resolved["sha256"]

    record = await wait_terminal(body["run_id"])
    assert record.status == "succeeded"
    assert record.trigger == "launch"
    assert record.executor == "codex"
    assert record.snapshot_sha256 == resolved["sha256"]
    assert record.provenance.idempotency_key == "mirarun:run-intent-001"
    assert record.provenance.environment_id == "env_42"
    assert record.provenance.edf_api_version == "mirarun.io/v1alpha1"
    # extra="allow": product-side fields survive the round trip verbatim
    assert record.provenance.model_dump()["custom_product_field"] == "kept-verbatim"

    # the immutable snapshot is retrievable and self-consistent
    resp = await executor_client.get(f"/runs/{body['run_id']}/snapshot")
    assert resp.status_code == 200
    snapshot = resp.json()
    assert snapshot["sha256"] == resolved["sha256"]
    assert snapshot["canonical"] == resolved["canonical"]
    assert snapshot["run_id"] == body["run_id"]


async def test_launch_prompt_is_dispatched_verbatim(executor_client):
    """A launch carries a COMPLETED prompt: no timestamp stamping, no
    header_prompt injection — prompt rendering is a control-plane concern."""
    resp = await executor_client.post("/executor-runs", json={
        "prompt": "exact prompt", "idempotency_key": "verbatim-1",
    })
    assert resp.status_code == 202
    record = await wait_terminal(resp.json()["run_id"])
    assert record.prompt == "exact prompt"
    assert record.snapshot_sha256 is None  # EDF-less launch: no snapshot
    resp = await executor_client.get(f"/runs/{record.run_id}/snapshot")
    assert resp.status_code == 404


# ── /executor-runs: idempotency + recovery ───────────────────────────────────


async def test_duplicate_idempotency_key_returns_original_run(executor_client):
    first = await executor_client.post("/executor-runs", json={
        "prompt": "do it", "idempotency_key": "once-only",
    })
    assert first.status_code == 202
    await wait_terminal(first.json()["run_id"])

    second = await executor_client.post("/executor-runs", json={
        "prompt": "do it AGAIN", "idempotency_key": "once-only",
    })
    assert second.status_code == 200  # not 202: nothing was launched
    body = second.json()
    assert body["duplicate"] is True
    assert body["run_id"] == first.json()["run_id"]
    assert body["status"] == "succeeded"
    assert len(app_module._run_store.list()) == 1  # no second record


async def test_recovery_after_crash_between_acceptance_and_dispatch(executor_client):
    """The ambiguity-window contract: acceptance is durable before dispatch.
    If the process dies before the background task completes, the startup
    sweep marks the record interrupted, and a retried idempotency key
    surfaces that original run (with its true status) instead of silently
    launching twice."""
    # Simulate durable acceptance whose dispatch never ran (process died):
    app_module._run_store.start(
        agent_name="codex-worker", trigger="launch", prompt="lost dispatch",
        provenance=RunProvenance(idempotency_key="ambiguous-1"),
    )
    # ...process restarts:
    assert app_module._run_store.sweep_interrupted() == 1

    resp = await executor_client.post("/executor-runs", json={
        "prompt": "lost dispatch", "idempotency_key": "ambiguous-1",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["duplicate"] is True
    assert body["status"] == "interrupted"  # caller decides how to proceed


# ── /executor-runs: refusals ─────────────────────────────────────────────────


async def test_launch_refuses_hash_mismatch(executor_client):
    resp = await executor_client.post("/executor-runs", json={
        "prompt": "x", "idempotency_key": "hash-check",
        "edf": default_dev_edf(), "expected_sha256": "0" * 64,
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "canonical snapshot hash mismatch"
    assert app_module._run_store.list() == []


async def test_launch_refuses_incompatible_edf(executor_client):
    resp = await executor_client.post("/executor-runs", json={
        "prompt": "x", "idempotency_key": "incompat-1",
        "edf": minimal_edf(kind="claude-code"),
    })
    assert resp.status_code == 409
    assert "issues" in resp.json()["detail"]
    assert app_module._run_store.list() == []


async def test_launch_refuses_invalid_edf(executor_client):
    edf = default_dev_edf()
    edf["unknownField"] = 1
    resp = await executor_client.post("/executor-runs", json={
        "prompt": "x", "idempotency_key": "invalid-1", "edf": edf,
    })
    assert resp.status_code == 422


async def test_launch_refuses_expected_sha_without_edf(executor_client):
    resp = await executor_client.post("/executor-runs", json={
        "prompt": "x", "idempotency_key": "sha-no-edf", "expected_sha256": "0" * 64,
    })
    assert resp.status_code == 422


async def test_launch_requires_executor_tier(tmp_path):
    from unittest.mock import MagicMock

    app_module._profile = MagicMock()
    app_module._executor = None
    app_module._agent = MagicMock()
    app_module._run_store = RunStore(root=tmp_path / "runs")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/executor-runs", json={"prompt": "x", "idempotency_key": "k"})
    assert resp.status_code == 400


# ── /health: version + capability discovery ──────────────────────────────────


async def test_health_advertises_version_and_contract_capabilities(executor_client):
    body = (await executor_client.get("/health")).json()
    assert "edf-resolve/mirarun.io-v1alpha1" in body["capabilities"]
    assert "executor-launch/v1" in body["capabilities"]
    assert "events-cursor/v1" in body["capabilities"]
    assert "run-snapshot/v1" in body["capabilities"]
