"""The memory backend protocol package (docs/memory-backend-protocol.md):
the conformance suite runs 12/12 against the built-in ephemeral backend in
CI (the Loimi cross-check runs in Loimi's own repo/live), and the full
memory lifecycle works end-to-end on `backend: ephemeral` with zero
external services — the pip-install story.
"""

import sys

import pytest
from unittest.mock import AsyncMock, MagicMock

import miragen.app  # noqa: F401
app_module = sys.modules["miragen.app"]
from miragen.memory.conformance import CHECKS, run_conformance
from miragen.memory.ephemeral import EphemeralMemoryService
from miragen.models import AgentProfile

EPHEMERAL_MEMORY = {
    "backend": "ephemeral",
    "scopes": {
        "read": ["profile:test-agent"],
        "propose": ["profile:test-agent"],
        "default_write": "profile:test-agent",
    },
}


def _make_profile(**kw):
    return AgentProfile.model_validate({
        "name": "test-agent", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
        **kw,
    })


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._memory = None
    import miragen.memory.ephemeral as ephemeral_module

    ephemeral_module._shared = None


class TestConformanceAgainstEphemeral:
    async def test_full_suite_passes(self):
        service = EphemeralMemoryService()
        results = await run_conformance(
            base_url="http://ephemeral.local",
            operator_token=service.operator_token,
            transport=service.transport(),
        )
        failed = [(r.name, r.detail) for r in results if not r.passed]
        assert failed == [], failed
        assert len(results) == len(CHECKS)

    async def test_wrong_operator_token_fails_provisioning_only(self):
        service = EphemeralMemoryService()
        results = await run_conformance(
            base_url="http://ephemeral.local",
            operator_token="wrong",
            transport=service.transport(),
        )
        assert len(results) == 1
        assert results[0].name == "provisioning" and not results[0].passed


class TestEphemeralBackendLifecycle:
    async def test_pip_install_story_end_to_end(self, tmp_path, monkeypatch):
        """The whole lifecycle — packet, checkpoint, restart-resume, finish
        capture, remember/read — with no env vars and no external service."""
        monkeypatch.delenv("LOIMI_MEMORY_URL", raising=False)
        monkeypatch.delenv("LOIMI_MEMORY_TOKEN", raising=False)
        monkeypatch.setenv("MIRAGEN_MEMORY_STATE_DIR", str(tmp_path / "memory"))

        profile = _make_profile(memory=EPHEMERAL_MEMORY)
        lifecycle = app_module._build_memory_lifecycle(profile)
        assert lifecycle is not None

        packet = await lifecycle.prepare_context(instance="ops", run_id="r1",
                                                 trigger="http")
        assert packet.degraded is None

        checkpoint = await lifecycle.checkpoint(
            instance="ops", patch={"goal": "ship the ephemeral backend"}
        )
        assert checkpoint["status"] == "accepted"

        # Simulated restart: a fresh lifecycle over the same process store.
        resumed = app_module._build_memory_lifecycle(profile)
        packet2 = await resumed.prepare_context(instance="ops", run_id="r2",
                                                trigger="http")
        assert "ship the ephemeral backend" in packet2.text

        finish = await resumed.finish_turn(instance="ops", run_id="r2",
                                           trigger="http", status="succeeded",
                                           summary="done")
        assert finish["status"] == "captured"

        remembered = await resumed.remember(instance="ops", run_id="r2",
                                            content="ephemeral works end to end")
        assert remembered["status"] == "accepted"
        read = await resumed.read(remembered["record_id"])
        assert read["revision"]["payload"]["text"] == "ephemeral works end to end"
        assert read["roots_valid"] is True

    async def test_run_endpoint_over_ephemeral_backend(self, tmp_path, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        from miragen.app import app
        from miragen.runs import RunStore

        monkeypatch.setenv("MIRAGEN_MEMORY_STATE_DIR", str(tmp_path / "memory"))
        profile = _make_profile(memory=EPHEMERAL_MEMORY)
        app_module._profile = profile
        app_module._run_store = RunStore(root=tmp_path / "runs")
        app_module._memory = app_module._build_memory_lifecycle(profile)

        result = MagicMock()
        result.output = "ok"
        usage = MagicMock()
        usage.requests, usage.input_tokens, usage.output_tokens = 1, 5, 5
        result.usage = usage
        result.all_messages = MagicMock(return_value=[])
        app_module._agent = MagicMock(run=AsyncMock(return_value=result))

        captured = {}

        def fake_build_agent(p, telemetry=None, *, secret_env=None,
                             extra_tools=None, extra_instructions=None):
            captured["instructions"] = extra_instructions
            return app_module._agent, None

        from unittest.mock import patch as _patch

        with _patch("miragen.app.build_agent", fake_build_agent):
            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://t") as client:
                resp = await client.post("/run", json={"prompt": "hi",
                                                       "instance": "ops"})
        assert resp.status_code == 200, resp.text
        assert "[memory guide" in captured["instructions"]
        # The finish event landed in the in-process store.
        from miragen.memory.ephemeral import shared_service

        finishes = [e for e in shared_service().events.values()
                    if e["source"]["kind"] == "agent_run"]
        assert len(finishes) == 1

    def test_ephemeral_requires_no_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOIMI_MEMORY_URL", raising=False)
        monkeypatch.delenv("LOIMI_MEMORY_TOKEN", raising=False)
        profile = _make_profile(memory=EPHEMERAL_MEMORY)
        assert app_module._build_memory_lifecycle(profile) is not None

    def test_loimi_backend_still_env_wired(self, monkeypatch):
        monkeypatch.delenv("LOIMI_MEMORY_URL", raising=False)
        profile = _make_profile(memory={
            **EPHEMERAL_MEMORY, "backend": "loimi",
        })
        lifecycle = app_module._build_memory_lifecycle(profile)
        # Client built, but unreachable env degrades explicitly at use.
        assert lifecycle.client._base_url_override is None
