"""Reviewed whole-run publication (capability reviewed-publication/v1).

Uses mocked MCP/httpx transport — no real Loimi. Covers auth, capability
advertisement, success, idempotency, deterministic rejects, and retryable
backend failure without mutating run status.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import miragen.app  # ensure module is registered

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.executor import CodexExecutor
from miragen.models import AgentProfile, ArtifactSinkSpec
from miragen.publication import (
    LoimiPublicationBackend,
    PublicationRecord,
    PublicationStore,
    PublicationUnavailableError,
    preconditions_ok,
)
from miragen.runs import RunStore

from tests.test_executor import StubThread, default_events


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _profile(**sink_kw) -> AgentProfile:
    body = {
        "name": "pub-worker",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "executor": {
            "executor": "codex",
            "instructions": "hi.",
            "artifact_sink": {
                "url": "https://loimi.mesh/mcp/",
                "bearer_token_env": "LOIMI_TOKEN",
                **sink_kw,
            },
        },
    }
    return AgentProfile.model_validate(body)


def _paths(profile: AgentProfile, tmp_path: Path) -> Path:
    profile.executor.workspace_root = str(tmp_path / "workspaces")
    profile.executor.codex_home = str(tmp_path / "codex-home")
    return tmp_path / "runs"


@pytest.fixture
def pub_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOIMI_TOKEN", "tok-pub")
    monkeypatch.delenv("MIRAGEN_INTERNAL_TOKEN", raising=False)
    profile = _profile()
    runs_root = _paths(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = CodexExecutor(
        profile, runs_root=runs_root, session_factory=StubThread(default_events())
    )
    app_module._run_store = RunStore(root=runs_root, retention=50)
    app_module._publication_store = PublicationStore(runs_root / "publications")
    app_module._publication_backend_override = None
    app_module._agent = None
    yield profile, runs_root
    app_module._profile = None
    app_module._executor = None
    app_module._run_store = None
    app_module._publication_store = None
    app_module._publication_backend_override = None


async def _succeed_run(client: AsyncClient) -> str:
    resp = await client.post("/run", json={"prompt": "fix it"})
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    record = app_module._run_store.get(run_id)
    assert record.status == "succeeded"
    assert record.diff_path
    return run_id


def _mcp_transport(calls: list, *, mode: str = "ok"):
    """mode: ok | open_fail_5xx | store_error | missing_run_id

    Mirrors Loimi's real MCP surface (loimi/src/loimi/mcp_server.py): the
    only write tools are store_open_run / store_put_artifact /
    store_close_run — there is no open_run, store_document, or close_run.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({
            "method": body.get("method"),
            "body": body,
            "headers": {k.lower(): v for k, v in request.headers.items()},
        })
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"serverInfo": {"name": "loimi"}}},
                headers={"mcp-session-id": "sess-pub"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            name = body["params"]["name"]
            if mode == "open_fail_5xx" and name == "store_open_run":
                return httpx.Response(503, text="upstream unavailable")
            if mode == "missing_run_id" and name == "store_open_run":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0", "id": body.get("id"),
                        "result": {"content": [{"type": "text", "text": "no id here"}]},
                    },
                )
            if name == "store_open_run":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0", "id": body.get("id"),
                        "result": {
                            "content": [{
                                "type": "text",
                                "text": json.dumps({"id": "loimi-run-99"}),
                            }],
                        },
                    },
                )
            if name == "store_put_artifact":
                if mode == "store_error":
                    return httpx.Response(
                        200,
                        json={
                            "jsonrpc": "2.0", "id": body.get("id"),
                            "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]},
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0", "id": body.get("id"),
                        "result": {
                            "content": [{
                                "type": "text",
                                "text": json.dumps({"id": "loimi-doc-7"}),
                            }],
                        },
                    },
                )
            if name == "store_close_run":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": True}},
                )
            raise AssertionError(f"unexpected tool {name}")
        raise AssertionError(f"unexpected method {method}")

    return httpx.MockTransport(handler)


def _pub_body(key: str = "mirarun:publication:aaaa", **extra_provenance) -> dict:
    return {
        "idempotency_key": key,
        "provenance": {
            "mirarun_run_intent_id": "intent-1",
            "environment_id": "env-1",
            "environment_revision": 3,
            "requested_by": "user-9",
            **extra_provenance,
        },
    }


# ── Capability + auth ────────────────────────────────────────────────────────


async def test_health_advertises_reviewed_publication(pub_env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        body = (await c.get("/health")).json()
    assert "reviewed-publication/v1" in body["capabilities"]
    # Capability = endpoint supported; readiness is separate.
    assert body["publication"]["endpoint_supported"] is True
    assert body["publication"]["backend_configured"] is True
    assert body["publication"]["backend_kind"] == "loimi"


async def test_health_publication_not_configured_without_sink(tmp_path, monkeypatch):
    monkeypatch.delenv("MIRAGEN_INTERNAL_TOKEN", raising=False)
    profile = AgentProfile.model_validate({
        "name": "no-pub",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "executor": {"executor": "codex", "instructions": "hi."},
    })
    profile.executor.workspace_root = str(tmp_path / "ws")
    profile.executor.codex_home = str(tmp_path / "ch")
    app_module._profile = profile
    app_module._executor = CodexExecutor(
        profile, runs_root=tmp_path / "runs", session_factory=StubThread(default_events())
    )
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        body = (await c.get("/health")).json()
    assert "reviewed-publication/v1" in body["capabilities"]
    assert body["publication"]["endpoint_supported"] is True
    assert body["publication"]["backend_configured"] is False
    assert body["publication"]["backend_kind"] is None


async def test_publication_requires_token_when_configured(pub_env, monkeypatch):
    class _Stub:
        async def publish_run(self, **kw):
            from miragen.publication import PublicationResult
            return PublicationResult(
                backend="loimi", external_run_id="r", external_artifact_ids=["a"]
            )

    app_module._publication_backend_override = _Stub()
    headers = {"X-Miragen-Token": "s3cret"}
    # Create run without token enforcement, then enable token for publication.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        run_id = await _succeed_run(c)
    monkeypatch.setenv("MIRAGEN_INTERNAL_TOKEN", "s3cret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        denied = await c.post(f"/runs/{run_id}/publications", json=_pub_body("k-auth"))
        assert denied.status_code == 401
        resp = await c.post(
            f"/runs/{run_id}/publications",
            json=_pub_body("k-auth-ok"),
            headers=headers,
        )
        assert resp.status_code == 200, resp.text


# ── Success + idempotency ────────────────────────────────────────────────────


async def test_successful_whole_run_publication(pub_env):
    calls = []
    app_module._publication_backend_override = LoimiPublicationBackend(
        ArtifactSinkSpec.model_validate({"url": "https://loimi.mesh/mcp/"}),
        bearer_token="tok-pub",
        transport=_mcp_transport(calls),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        run_id = await _succeed_run(c)
        # Auto-sink retired: succeeded run must not have published yet.
        assert app_module._run_store.get(run_id).artifact_stored is None

        resp = await c.post(f"/runs/{run_id}/publications", json=_pub_body())
        assert resp.status_code == 200, resp.text
        body = resp.json()

    assert body["run_id"] == run_id
    assert body["status"] == "published"
    assert body["duplicate"] is False
    assert body["backend"] == "loimi"
    assert body["external_run_id"] == "loimi-run-99"
    assert body["external_artifact_ids"] == ["loimi-doc-7"]
    # Loimi convenience aliases (same opaque values)
    assert body["loimi_run_id"] == "loimi-run-99"
    assert body["loimi_artifact_ids"] == ["loimi-doc-7"]
    assert body["publication_id"]
    assert isinstance(body["publication_id"], str)

    tool_names = [
        c["body"]["params"]["name"]
        for c in calls
        if c["method"] == "tools/call"
    ]
    assert tool_names == ["store_open_run", "store_put_artifact", "store_close_run"]
    # Auth + session on backend calls
    store_call = next(c for c in calls if c["method"] == "tools/call" and c["body"]["params"]["name"] == "store_put_artifact")
    assert store_call["headers"]["authorization"] == "Bearer tok-pub"
    assert store_call["headers"]["mcp-session-id"] == "sess-pub"

    open_call = next(c for c in calls if c["method"] == "tools/call" and c["body"]["params"]["name"] == "store_open_run")
    open_args = open_call["body"]["params"]["arguments"]
    assert open_args["agent_id"] == "pub-worker"
    # The agent's own home namespace always exists (Loimi auto-mints it
    # alongside the agent) — anything else risks UnknownNamespace.
    assert open_args["namespace"] == "pub-worker"

    artifact_args = store_call["body"]["params"]["arguments"]
    assert artifact_args["properties"]["agent"] == "pub-worker"

    # Run status unchanged; advisory flag set
    record = app_module._run_store.get(run_id)
    assert record.status == "succeeded"
    assert record.artifact_stored is True


async def test_routine_slug_travels_alongside_agent_name_in_artifact_properties(pub_env):
    # This is the coupling the admin dashboard needs: InsightCite's hardcoded
    # agent-id strings only resolve to real output if the published artifact
    # carries the routine's slug — and it must sit alongside `agent`
    # (the miragen profile name), not replace it: an agent can be invoked by
    # more than one routine, and not every run is routine-driven.
    calls = []
    app_module._publication_backend_override = LoimiPublicationBackend(
        ArtifactSinkSpec.model_validate({"url": "https://loimi.mesh/mcp/"}),
        transport=_mcp_transport(calls),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        run_id = await _succeed_run(c)
        resp = await c.post(
            f"/runs/{run_id}/publications",
            json=_pub_body("k-routine", routine_slug="pricing-guardian"),
        )
        assert resp.status_code == 200, resp.text

    store_call = next(
        x for x in calls
        if x["method"] == "tools/call" and x["body"]["params"]["name"] == "store_put_artifact"
    )
    properties = store_call["body"]["params"]["arguments"]["properties"]
    assert properties["routine_slug"] == "pricing-guardian"
    assert properties["agent"] == "pub-worker"


async def test_no_routine_slug_when_provenance_omits_it(pub_env):
    calls = []
    app_module._publication_backend_override = LoimiPublicationBackend(
        ArtifactSinkSpec.model_validate({"url": "https://loimi.mesh/mcp/"}),
        transport=_mcp_transport(calls),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        run_id = await _succeed_run(c)
        resp = await c.post(f"/runs/{run_id}/publications", json=_pub_body("k-noroutine"))
        assert resp.status_code == 200, resp.text

    store_call = next(
        x for x in calls
        if x["method"] == "tools/call" and x["body"]["params"]["name"] == "store_put_artifact"
    )
    assert "routine_slug" not in store_call["body"]["params"]["arguments"]["properties"]


async def test_idempotent_repeat_publication(pub_env):
    calls = []
    app_module._publication_backend_override = LoimiPublicationBackend(
        ArtifactSinkSpec.model_validate({"url": "https://loimi.mesh/mcp/"}),
        transport=_mcp_transport(calls),
    )
    key = "mirarun:publication:same-key"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        run_id = await _succeed_run(c)
        first = (await c.post(f"/runs/{run_id}/publications", json=_pub_body(key))).json()
        n_tools = sum(1 for x in calls if x["method"] == "tools/call")
        second = await c.post(f"/runs/{run_id}/publications", json=_pub_body(key))
        assert second.status_code == 200
        body = second.json()

    assert body["duplicate"] is True
    assert body["publication_id"] == first["publication_id"]
    assert body["external_run_id"] == first["external_run_id"]
    assert body["external_artifact_ids"] == first["external_artifact_ids"]
    # No additional backend writes
    assert sum(1 for x in calls if x["method"] == "tools/call") == n_tools


# ── Deterministic rejects ────────────────────────────────────────────────────


async def test_reject_running_run(pub_env):
    store = app_module._run_store
    record = store.start(agent_name="pub-worker", trigger="http", prompt="x")
    # still running, no diff
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(f"/runs/{record.run_id}/publications", json=_pub_body("k-run"))
    assert resp.status_code == 400
    assert "succeeded" in resp.json()["detail"].lower()


async def test_reject_failed_run(pub_env):
    store = app_module._run_store
    record = store.start(agent_name="pub-worker", trigger="http", prompt="x")
    store.finish(record, status="failed", error="boom", exit_reason="crash")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(f"/runs/{record.run_id}/publications", json=_pub_body("k-fail"))
    assert resp.status_code == 400
    assert "failed" in resp.json()["detail"].lower()


async def test_reject_succeeded_without_diff(pub_env):
    store = app_module._run_store
    record = store.start(agent_name="pub-worker", trigger="http", prompt="x")
    store.finish(record, status="succeeded", output="ok")  # no diff_path
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(f"/runs/{record.run_id}/publications", json=_pub_body("k-nodiff"))
    assert resp.status_code == 400
    assert "diff" in resp.json()["detail"].lower()


async def test_reject_missing_backend_config(tmp_path, monkeypatch):
    monkeypatch.delenv("MIRAGEN_INTERNAL_TOKEN", raising=False)
    profile = AgentProfile.model_validate({
        "name": "no-sink",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "executor": {"executor": "codex", "instructions": "hi."},
    })
    profile.executor.workspace_root = str(tmp_path / "ws")
    profile.executor.codex_home = str(tmp_path / "ch")
    runs_root = tmp_path / "runs"
    app_module._profile = profile
    app_module._executor = CodexExecutor(
        profile, runs_root=runs_root, session_factory=StubThread(default_events())
    )
    app_module._run_store = RunStore(root=runs_root, retention=50)
    app_module._publication_store = PublicationStore(runs_root / "publications")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        run_id = (await c.post("/run", json={"prompt": "x"})).json()["run_id"]
        resp = await c.post(f"/runs/{run_id}/publications", json=_pub_body("k-nosink"))
    assert resp.status_code == 400
    assert "not configured" in resp.json()["detail"].lower()


# ── Retryable backend failure ────────────────────────────────────────────────


async def test_retryable_loimi_failure_does_not_change_run_status(pub_env):
    calls = []
    app_module._publication_backend_override = LoimiPublicationBackend(
        ArtifactSinkSpec.model_validate({"url": "https://loimi.mesh/mcp/"}),
        transport=_mcp_transport(calls, mode="open_fail_5xx"),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        run_id = await _succeed_run(c)
        before = app_module._run_store.get(run_id)
        resp = await c.post(f"/runs/{run_id}/publications", json=_pub_body("k-503"))
        after = app_module._run_store.get(run_id)

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["retryable"] is True
    assert after.status == "succeeded"
    assert after.artifact_stored is None  # never marked stored
    assert after.status == before.status
    assert after.diff_path == before.diff_path


async def test_preconditions_helper():
    from datetime import datetime, timezone
    from miragen.models import RunRecord

    r = RunRecord(
        run_id="abc",
        agent_name="a",
        trigger="http",
        status="failed",
        prompt="p",
        started_at=datetime.now(timezone.utc),
    )
    assert preconditions_ok(r) is not None


async def test_concurrent_same_key_serializes(pub_env):
    """Two concurrent publications with the same key must not double-write."""
    import asyncio

    calls = []
    barrier = asyncio.Event()
    entered = 0

    class _SlowBackend:
        async def publish_run(self, **kw):
            nonlocal entered
            entered += 1
            # Let the sibling request hit the claim path while we hold the key.
            if entered == 1:
                barrier.set()
                await asyncio.sleep(0.05)
            from miragen.publication import PublicationResult
            calls.append("publish")
            return PublicationResult(
                backend="loimi",
                external_run_id="only-one",
                external_artifact_ids=["a1"],
            )

    app_module._publication_backend_override = _SlowBackend()
    key = "mirarun:publication:concurrent"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        run_id = await _succeed_run(c)

        async def _pub():
            return await c.post(f"/runs/{run_id}/publications", json=_pub_body(key))

        t1 = asyncio.create_task(_pub())
        await barrier.wait()
        t2 = asyncio.create_task(_pub())
        r1, r2 = await asyncio.gather(t1, t2)

    statuses = sorted([r1.status_code, r2.status_code])
    # One 200 published, the other either 200 duplicate or 503 in-progress then
    # would need retry — under in-process lock both should complete as 200 with
    # one duplicate.
    assert statuses == [200, 200]
    bodies = [r1.json(), r2.json()]
    assert {b["external_run_id"] for b in bodies} == {"only-one"}
    assert {b["publication_id"] for b in bodies}  # both have ids
    assert sum(1 for b in bodies if b["duplicate"]) == 1
    assert sum(1 for b in bodies if not b["duplicate"]) == 1
    assert calls.count("publish") == 1
