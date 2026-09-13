"""The memory lifecycle (PR 2 of the memory architecture pass): client
error vocabulary, boundary packets, explicit degradation, durable finish
capture, tool semantics, and the run-boundary wiring in app.py.

The Loimi /memory/v1 side is simulated with an in-memory fake behind
httpx.MockTransport — its response shapes mirror Loimi's PR-1 contract
(tests there are the authority on the real behavior).
"""

import json
import sys
import uuid

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import miragen.app  # noqa: F401
app_module = sys.modules["miragen.app"]
from miragen.memory import MemoryAPIError, MemoryClient, MemoryLifecycle, MemoryUnavailable
from miragen.memory.guidance import GUIDANCE_VERSION
from miragen.models import AgentProfile

MEMORY_BLOCK = {
    "scopes": {
        "read": ["profile:test-agent", "group:project-a"],
        "propose": ["profile:test-agent"],
        "default_write": "profile:test-agent",
    },
}


def _make_profile(**kw):
    return AgentProfile.model_validate({
        "name": "test-agent",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
        **kw,
    })


class FakeMemoryService:
    """In-memory /memory/v1: contexts, events, records, manifests. Shapes
    mirror Loimi PR 1. `fail_with` forces transport-level failure."""

    def __init__(self):
        self.contexts: dict[str, dict] = {}
        self.events: dict[str, dict] = {}          # idempotency_key -> event
        self.records: dict[str, dict] = {}
        self.manifests: list[dict] = []
        self.jobs: list[dict] = []
        self.proposals: list[dict] = []            # raw propose bodies, in order
        self.requests: list[tuple[str, str]] = []  # (method, path)
        self.fail_with: Exception | None = None
        self.auth_seen: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with is not None:
            raise self.fail_with
        self.auth_seen.append(request.headers.get("authorization", ""))
        path = request.url.path.removeprefix("/memory/v1")
        self.requests.append((request.method, path))
        body = json.loads(request.content) if request.content else {}

        if request.method == "POST" and path == "/contexts":
            context_id = str(uuid.uuid4())
            context = {
                "id": context_id, "scope_id": body["scope_id"],
                "kind": body.get("kind", "task"), "title": body.get("title", ""),
                "state": body.get("state", {}), "state_revision": 1,
                "created_by": "test", "updated_at": "2026-09-13T00:00:00Z",
            }
            self.contexts[context_id] = context
            return httpx.Response(201, json=context)
        if request.method == "GET" and path.startswith("/contexts/"):
            context = self.contexts.get(path.split("/")[-1])
            if context is None:
                return httpx.Response(404, json=_err("not_found", "unknown work context"))
            return httpx.Response(200, json=context)
        if request.method == "PATCH" and path.startswith("/contexts/"):
            context = self.contexts.get(path.split("/")[-1])
            if context is None:
                return httpx.Response(404, json=_err("not_found", "unknown work context"))
            if context["state_revision"] != body["expected_revision"]:
                return httpx.Response(409, json=_err(
                    "illegal_transition", "expected_revision does not match",
                    {"current_revision": context["state_revision"]},
                ))
            if body.get("state") is not None:
                context["state"] = body["state"]
            elif body.get("patch"):
                merged = {**context["state"], **body["patch"]}
                context["state"] = {k: v for k, v in merged.items() if v is not None}
            context["state_revision"] += 1
            return httpx.Response(200, json=context)
        if request.method == "POST" and path == "/events":
            key = body["idempotency_key"]
            if key in self.events:
                return httpx.Response(201, json=self.events[key] | {"created": False})
            event = {
                "id": str(uuid.uuid4()), "scope_id": body["scope_id"],
                "producer_id": "test-agent", "source": body["source"],
                "content": body["content"], "content_digest": "x" * 64,
                "attributes": body.get("attributes", {}),
                "occurred_at": body.get("occurred_at"),
                "received_at": "2026-09-13T00:00:00Z",
                "generation": 1, "retracted_at": None, "erased_at": None,
                "created": True,
            }
            self.events[key] = event
            return httpx.Response(201, json=event)
        if request.method == "POST" and path == "/records":
            self.proposals.append(body)
            if body.get("quarantine"):
                admission = "quarantined"
            else:
                admission = "accepted" if body.get("source_event_ids") else "candidate"
            record = {
                "record_id": str(uuid.uuid4()), "type": body["type"],
                "scope_id": body["scope_id"],
                "admission": admission,
                "slot_id": None,
                "revision": {"id": str(uuid.uuid4()), "seq": 1,
                             "payload": body["payload"], "lifecycle": "active"},
                "resolution": None, "roots": [], "roots_valid": True,
            }
            self.records[record["record_id"]] = record
            return httpx.Response(201, json=record)
        if request.method == "POST" and path.endswith("/correct"):
            record_id = path.split("/")[-2]
            record = self.records.get(record_id)
            if record is None:
                return httpx.Response(404, json=_err("not_found", "unknown record"))
            if record["revision"]["seq"] != body["expected_seq"]:
                return httpx.Response(409, json=_err(
                    "illegal_transition", "expected_seq does not match",
                    {"current_seq": record["revision"]["seq"]},
                ))
            record["revision"] = {
                "id": str(uuid.uuid4()), "seq": record["revision"]["seq"] + 1,
                "payload": body["payload"], "lifecycle": "active",
                "correction_of": record["revision"]["id"],
            }
            return httpx.Response(200, json=record | {
                "resolution": {"outcome": "corrected", "slot_version": 2},
            })
        if request.method == "GET" and path.startswith("/events/"):
            wanted = path.split("/")[-1]
            for event in self.events.values():
                if event["id"] == wanted:
                    return httpx.Response(200, json=event)
            return httpx.Response(404, json=_err("not_found", "unknown source event"))
        if request.method == "POST" and path == "/jobs/claim":
            claimed = []
            for job in self.jobs:
                if len(claimed) >= body.get("limit", 1):
                    break
                if job["status"] == "pending" and job["kind"] in body["kinds"]:
                    job["status"] = "leased"
                    job["attempts"] = job.get("attempts", 0) + 1
                    claimed.append(job)
            return httpx.Response(200, json={"items": claimed})
        if request.method == "POST" and path.endswith("/complete"):
            job_id = path.split("/")[-2]
            for job in self.jobs:
                if job["id"] == job_id and job["status"] == "leased":
                    job["status"] = "done"
                    return httpx.Response(200, json=job)
            return httpx.Response(404, json=_err("not_found", "no leased job"))
        if request.method == "POST" and path.endswith("/fail"):
            job_id = path.split("/")[-2]
            for job in self.jobs:
                if job["id"] == job_id and job["status"] == "leased":
                    job["status"] = "pending" if body.get("retry", True) else "failed"
                    job["last_error"] = body["error"]
                    return httpx.Response(200, json=job)
            return httpx.Response(404, json=_err("not_found", "no leased job"))
        if request.method == "GET" and path.startswith("/records/"):
            record = self.records.get(path.split("/")[-1])
            if record is None:
                return httpx.Response(404, json=_err("not_found", "unknown record"))
            return httpx.Response(200, json=record)
        if request.method == "POST" and path == "/manifests":
            manifest = {"id": str(uuid.uuid4()), "created_at": "2026-09-13T00:00:00Z"}
            self.manifests.append(body | manifest)
            return httpx.Response(201, json=manifest)
        return httpx.Response(404, json=_err("not_found", f"unhandled {path}"))


def _err(code, message, details=None):
    return {"error": {"code": code, "message": message, "details": details or {}}}


@pytest.fixture(autouse=True)
def memory_env(monkeypatch):
    monkeypatch.setenv("LOIMI_MEMORY_URL", "http://loimi.test")
    monkeypatch.setenv("LOIMI_MEMORY_TOKEN", "lmm_test-token")


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._memory = None
    app_module._voice = None


@pytest.fixture
def service():
    return FakeMemoryService()


@pytest.fixture
def lifecycle(service, tmp_path):
    profile = _make_profile(memory=MEMORY_BLOCK)
    client = MemoryClient(profile.memory, transport=service.transport())
    return MemoryLifecycle(
        profile.memory, "test-agent", client, state_dir=tmp_path / "memory"
    )


# ── client error vocabulary ──────────────────────────────────────────────────


class TestClient:
    async def test_bearer_token_sent(self, service, lifecycle):
        await lifecycle.client.create_context(scope_id="profile:test-agent")
        assert service.auth_seen == ["Bearer lmm_test-token"]

    async def test_missing_endpoint_env_is_unavailable(self, monkeypatch, service):
        monkeypatch.delenv("LOIMI_MEMORY_URL")
        profile = _make_profile(memory=MEMORY_BLOCK)
        client = MemoryClient(profile.memory, transport=service.transport())
        with pytest.raises(MemoryUnavailable, match="LOIMI_MEMORY_URL"):
            await client.get_context("x")

    async def test_transport_failure_is_unavailable(self, service, lifecycle):
        service.fail_with = httpx.ConnectError("refused")
        with pytest.raises(MemoryUnavailable):
            await lifecycle.client.get_context("x")

    async def test_5xx_is_unavailable_4xx_is_api_error(self, lifecycle):
        def handler(request):
            return httpx.Response(503)
        lifecycle.client._transport = httpx.MockTransport(handler)
        with pytest.raises(MemoryUnavailable):
            await lifecycle.client.get_context("x")

        def handler4(request):
            return httpx.Response(422, json=_err("invalid_request", "bad"))
        lifecycle.client._transport = httpx.MockTransport(handler4)
        with pytest.raises(MemoryAPIError) as exc:
            await lifecycle.client.get_context("x")
        assert exc.value.status_code == 422


# ── boundary packets ─────────────────────────────────────────────────────────


class TestPrepareContext:
    async def test_first_prepare_creates_context_and_persists_map(
        self, lifecycle, service, tmp_path
    ):
        packet = await lifecycle.prepare_context(instance="alpha", run_id="r1", trigger="http")
        assert packet.degraded is None
        assert packet.context_id in service.contexts
        mapping = json.loads((tmp_path / "memory" / "contexts.json").read_text())
        assert mapping["alpha"] == packet.context_id

    async def test_packet_carries_guidance_and_state(self, lifecycle, service):
        first = await lifecycle.prepare_context(instance="alpha", run_id="r1", trigger="http")
        service.contexts[first.context_id]["state"] = {"goal": "finish the report"}
        service.contexts[first.context_id]["state_revision"] = 2

        packet = await lifecycle.prepare_context(instance="alpha", run_id="r2", trigger="http")
        assert f"[memory guide v{GUIDANCE_VERSION}]" in packet.text
        assert "finish the report" in packet.text
        assert "reference data" in packet.text
        assert packet.state_revision == 2

    async def test_instances_get_separate_contexts(self, lifecycle):
        a = await lifecycle.prepare_context(instance="alpha", run_id="r1", trigger="http")
        b = await lifecycle.prepare_context(instance="beta", run_id="r2", trigger="http")
        assert a.context_id != b.context_id

    async def test_lost_context_is_recreated_not_invented(self, lifecycle, service):
        first = await lifecycle.prepare_context(instance="alpha", run_id="r1", trigger="http")
        del service.contexts[first.context_id]
        packet = await lifecycle.prepare_context(instance="alpha", run_id="r2", trigger="http")
        assert packet.degraded is None
        assert packet.context_id != first.context_id
        assert "(empty)" in packet.text  # state genuinely gone, not recalled

    async def test_unreachable_service_degrades_explicitly(self, lifecycle, service):
        service.fail_with = httpx.ConnectError("refused")
        packet = await lifecycle.prepare_context(instance="alpha", run_id="r1", trigger="http")
        assert packet.degraded is not None
        assert "MEMORY DEGRADED" in packet.text
        assert lifecycle.degraded_count == 1

    async def test_manifest_records_the_injection(self, lifecycle, service):
        await lifecycle.prepare_context(instance="alpha", run_id="r1", trigger="cron")
        (manifest,) = service.manifests
        assert manifest["run_ref"] == "r1"
        assert manifest["policy"]["guidance_version"] == GUIDANCE_VERSION
        assert manifest["policy"]["lane"] == "required"


# ── finish capture ───────────────────────────────────────────────────────────


class TestFinishTurn:
    async def test_outcome_captured_idempotently(self, lifecycle, service):
        one = await lifecycle.finish_turn(
            instance="alpha", run_id="r1", trigger="http",
            status="succeeded", summary="done",
        )
        two = await lifecycle.finish_turn(
            instance="alpha", run_id="r1", trigger="http",
            status="succeeded", summary="done",
        )
        assert one["status"] == "captured"
        assert one["event_id"] == two["event_id"]
        assert len(service.events) == 1

    async def test_each_executor_turn_gets_its_own_event(self, lifecycle, service):
        await lifecycle.finish_turn(instance=None, run_id="r1", trigger="launch",
                                    status="suspended", turn=0)
        await lifecycle.finish_turn(instance=None, run_id="r1", trigger="launch",
                                    status="succeeded", turn=1)
        assert len(service.events) == 2

    async def test_unavailable_is_reported_never_swallowed(self, lifecycle, service):
        service.fail_with = httpx.ConnectError("refused")
        result = await lifecycle.finish_turn(
            instance=None, run_id="r1", trigger="http", status="succeeded",
        )
        assert result["status"] == "persistence_unavailable"
        assert lifecycle.degraded_count == 1


# ── tool semantics ───────────────────────────────────────────────────────────


class TestTools:
    async def test_checkpoint_merges_and_bumps_revision(self, lifecycle, service):
        first = await lifecycle.checkpoint(instance="alpha", patch={"goal": "a"})
        assert first["status"] == "accepted"
        second = await lifecycle.checkpoint(instance="alpha", patch={"step": 2})
        assert second["state_revision"] == 3
        context = service.contexts[second["context_id"]]
        assert context["state"] == {"goal": "a", "step": 2}

    async def test_checkpoint_retries_once_on_concurrent_writer(self, lifecycle, service):
        packet = await lifecycle.prepare_context(instance="alpha", run_id="r", trigger="http")
        # A concurrent writer moved the revision after our last read.
        service.contexts[packet.context_id]["state_revision"] = 5
        result = await lifecycle.checkpoint(instance="alpha", patch={"goal": "x"})
        assert result["status"] == "accepted"
        assert result["state_revision"] == 6

    async def test_checkpoint_unavailable_says_so(self, lifecycle, service):
        service.fail_with = httpx.ConnectError("refused")
        result = await lifecycle.checkpoint(instance="alpha", patch={"goal": "x"})
        assert result["status"] == "persistence_unavailable"

    async def test_remember_roots_the_record_in_an_event(self, lifecycle, service):
        result = await lifecycle.remember(
            instance="alpha", run_id="r1", content="the deploy uses blue/green",
        )
        assert result["status"] == "accepted"
        assert result["event_id"] is not None
        assert ("POST", "/events") in service.requests
        assert ("POST", "/records") in service.requests

    async def test_remember_unavailable_is_not_saved(self, lifecycle, service):
        service.fail_with = httpx.ConnectError("refused")
        result = await lifecycle.remember(instance=None, run_id="r1", content="x")
        assert result["status"] == "persistence_unavailable"


# ── profile validation + boot honesty ────────────────────────────────────────


class TestProfileAndBoot:
    def test_default_write_must_be_proposable(self):
        with pytest.raises(Exception, match="default_write"):
            _make_profile(memory={
                "scopes": {"read": ["a"], "propose": ["a"], "default_write": "b"},
            })

    def test_native_required_without_hook_support_refuses_to_boot(self):
        """kimi-code has no verified hook contract yet — demanding
        native hooks there is an explicit boot failure (§18.7)."""
        profile = AgentProfile.model_validate({
            "name": "a", "mode": "interactive", "triggers": [{"type": "http"}],
            "executor": {"executor": "kimi-code", "instructions": "work"},
            "memory": {**MEMORY_BLOCK, "hooks": {"mode": "native_required"}},
        })
        with pytest.raises(ValueError, match="native_required"):
            app_module._build_memory_lifecycle(profile)

    def test_native_required_on_codex_boots_via_hooks_bridge(self):
        profile = AgentProfile.model_validate({
            "name": "a", "mode": "interactive", "triggers": [{"type": "http"}],
            "executor": {"executor": "codex", "instructions": "work"},
            "memory": {**MEMORY_BLOCK, "hooks": {"mode": "native_required"}},
        })
        assert app_module._build_memory_lifecycle(profile) is not None

    def test_boundary_only_executor_boots(self):
        profile = AgentProfile.model_validate({
            "name": "a", "mode": "interactive", "triggers": [{"type": "http"}],
            "executor": {"executor": "codex", "instructions": "work"},
            "memory": MEMORY_BLOCK,
        })
        lifecycle = app_module._build_memory_lifecycle(profile)
        assert lifecycle is not None
        # No MCP servers configured -> tools are honestly unavailable in guidance.
        assert lifecycle.tools_available is False

    def test_native_required_on_model_tier_is_satisfied(self):
        profile = _make_profile(
            memory={**MEMORY_BLOCK, "hooks": {"mode": "native_required"}}
        )
        assert app_module._build_memory_lifecycle(profile) is not None


# ── app boundary wiring ──────────────────────────────────────────────────────


def _mock_run_result(output="agent output"):
    usage = MagicMock()
    usage.requests = 1
    usage.input_tokens = 10
    usage.output_tokens = 5
    result = MagicMock()
    result.output = output
    result.usage = usage
    result.all_messages = MagicMock(return_value=[])
    return result


class TestAppBoundary:
    @pytest.fixture
    async def client(self, service, tmp_path):
        from httpx import ASGITransport, AsyncClient

        from miragen.app import app
        from miragen.runs import RunStore

        profile = _make_profile(memory=MEMORY_BLOCK)
        app_module._profile = profile
        app_module._agent = MagicMock(run=AsyncMock(return_value=_mock_run_result()))
        app_module._run_store = RunStore(root=tmp_path / "runs")
        app_module._memory = MemoryLifecycle(
            profile.memory, "test-agent",
            MemoryClient(profile.memory, transport=service.transport()),
            state_dir=tmp_path / "memory",
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c

    async def test_run_injects_packet_as_instructions_and_captures_finish(
        self, client, service
    ):
        captured = {}

        def fake_build_agent(profile, telemetry=None, *, secret_env=None,
                             extra_tools=None, extra_instructions=None):
            captured["extra_instructions"] = extra_instructions
            captured["tool_names"] = [t.__name__ for t in (extra_tools or [])]
            return app_module._agent, None

        # Seed state so the packet visibly carries it.
        packet = await app_module._memory.prepare_context(
            instance="support", run_id=None, trigger="seed"
        )
        service.contexts[packet.context_id]["state"] = {"goal": "answer tickets"}

        with patch("miragen.app.build_agent", fake_build_agent):
            resp = await client.post("/run", json={"prompt": "hi", "instance": "support"})

        assert resp.status_code == 200, resp.text
        assert "answer tickets" in captured["extra_instructions"]
        assert f"[memory guide v{GUIDANCE_VERSION}]" in captured["extra_instructions"]
        assert "memory_checkpoint" in captured["tool_names"]
        # The finish event landed durably, attributed to this run.
        run_id = resp.json()["run_id"]
        finish = [e for k, e in service.events.items() if run_id in k]
        assert len(finish) == 1
        assert finish[0]["attributes"]["status"] == "succeeded"

    async def test_memory_outage_never_fails_the_run(self, client, service):
        service.fail_with = httpx.ConnectError("refused")

        captured = {}

        def fake_build_agent(profile, telemetry=None, *, secret_env=None,
                             extra_tools=None, extra_instructions=None):
            captured["extra_instructions"] = extra_instructions
            return app_module._agent, None

        with patch("miragen.app.build_agent", fake_build_agent):
            resp = await client.post("/run", json={"prompt": "hi"})
        assert resp.status_code == 200
        assert "MEMORY DEGRADED" in captured["extra_instructions"]

    async def test_health_reports_memory_state(self, client):
        body = (await client.get("/health")).json()
        assert body["memory"]["configured"] is True
        assert body["memory"]["backend"] == "loimi"
        assert body["memory"]["native_hooks"]["mechanism"] == "model_tier_boundary"
        assert "memory/v1" in body["capabilities"]

    async def test_health_reports_degradation(self, client, service):
        service.fail_with = httpx.ConnectError("refused")
        await client.post("/run", json={"prompt": "hi"})
        body = (await client.get("/health")).json()
        assert body["memory"]["degraded_count"] >= 1
        assert "refused" in body["memory"]["last_degraded"]


# ── executor-tier MCP tools ──────────────────────────────────────────────────


class TestMemoryMCP:
    async def test_checkpoint_binds_to_the_single_running_run(
        self, lifecycle, service, tmp_path
    ):
        from miragen.memory_mcp import build_memory_mcp
        from miragen.runs import RunStore

        store = RunStore(root=tmp_path / "runs")
        record = store.start(agent_name="a", trigger="http", prompt="p", instance="ops")
        mcp = build_memory_mcp(lambda: (lifecycle, store))
        blocks, structured = await mcp.call_tool(
            "memory_checkpoint", {"state": {"goal": "x"}}
        )
        assert json.loads(structured["result"])["status"] == "accepted"
        # State landed on the RUN'S instance context, not a default one.
        mapping = json.loads((tmp_path / "memory" / "contexts.json").read_text())
        assert "ops" in mapping

    async def test_unconfigured_memory_is_a_tool_error(self, tmp_path):
        from miragen.memory_mcp import build_memory_mcp

        mcp = build_memory_mcp(lambda: (None, None))
        with pytest.raises(Exception, match="no memory configured"):
            await mcp.call_tool("memory_remember", {"content": "x"})
