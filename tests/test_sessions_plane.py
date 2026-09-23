"""miragend's session plane end-to-end against the in-process ephemeral
memory backend: registration, project → scope resolution and
auto-provisioning, context injection at start/restore, prompt-time
recall, out-of-band capture (idempotent), compaction + session-end
episodes and checkpoints, explicit degradation when Loimi is unreachable
or a scope is not provisioned, journal replay after a daemon restart,
registry persistence, the liveness sweep, parent/child tracking, the
HTTP surface (auth, malformed-event accounting, health) and OTLP spans."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from miragen.daemon.api import DAEMON_CAPABILITIES, create_app
from miragen.daemon.sessions.config import (
    Housekeeping,
    ProjectBinding,
    ScopePolicy,
    SessionsConfig,
    SessionsRecall,
    load_sessions_config,
)
from miragen.daemon.sessions.models import EventEnvelope, ProjectIdentity
from miragen.daemon.sessions.plane import SessionPlane
from miragen.daemon.sessions.projects import assign_scopes, normalize_remote, project_slug
from miragen.daemon.sessions.store import StoreClient
from miragen.memory.client import MemoryClient
from miragen.memory.ephemeral import EphemeralMemoryService, provision_profile
from miragen.memory.selection import Selection, SelectionResult
from miragen.models import MemoryScopesSpec
from miragen.telemetry import MiragenTelemetry
from miragen_hook.normalize import normalize_hook_payload

PRINCIPAL = "assistant"
SHARED = "profile:assistant"
REPO = ProjectIdentity(
    id="github.com/org/repo", slug="github.com-org-repo", name="repo",
    root="/w/repo", remote="git@github.com:org/repo.git",
)
OTHER = ProjectIdentity(
    id="github.com/org/other", slug="github.com-org-other", name="other",
    root="/w/other", remote="https://github.com/org/other.git",
)
PROJECT_SCOPE = "group:project.github.com-org-repo"
CLAUDE = {"session_id": "s-1", "transcript_path": "/t.jsonl", "cwd": "/w/repo",
          "permission_mode": "default"}


LOCAL_HOST = "desk"
STORE_TOKEN = "store-secret"


def envelope(hook_event: str, *, harness="claude-code", session="s-1", cwd="/w/repo",
             pid=4242, client_extra=None, host=None, remote=None, project_remote=None,
             **payload) -> EventEnvelope:
    """An envelope exactly as miragen_hook would post it for this hook."""
    raw = {**CLAUDE, "session_id": session, "cwd": cwd, "hook_event_name": hook_event, **payload}
    event = normalize_hook_payload(harness, raw)
    assert event is not None, hook_event
    return EventEnvelope.model_validate({
        "harness": harness, "session_id": session, "event": event.to_dict(),
        "client": {"pid": pid, "cwd": cwd, "user": "ilari", "adapter": "miragen-hook/1",
                   "host": host, "remote": remote, "project_remote": project_remote,
                   **(client_extra or {})},
    })


class FakeStore:
    """Loimi's /v0 artifact store, enough of it for the plane and the
    bridge tools: runs, artifacts, lineage, search, namespaces. Bearer
    must be the store token — the operator credential, never a memory
    principal's."""

    def __init__(self, token: str = STORE_TOKEN, namespaces=("mira", "infra", "muutto365")):
        self.token = token
        self.namespaces = {n: {"id": n, "description": n} for n in namespaces}
        self.runs: dict[str, dict] = {}
        self.artifacts: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        import uuid

        path = request.url.path
        self.requests.append((request.method, path))
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"detail": "unauthenticated", "code": "unauthenticated"})
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST" and path == "/v0/runs":
            if body["namespace"] not in self.namespaces:
                return httpx.Response(422, json={"detail": "unknown namespace", "code": "unknown_namespace"})
            run = {"id": str(uuid.uuid4()), "agent_id": body["agent_id"], "task": body["task"],
                   "namespace": body["namespace"], "parent_run_id": body.get("parent_run_id"),
                   "status": "running", "finished_at": None}
            self.runs[run["id"]] = run
            return httpx.Response(201, json=run)
        if request.method == "PATCH" and path.startswith("/v0/runs/"):
            run = self.runs.get(path.rsplit("/", 1)[-1])
            if run is None:
                return httpx.Response(404, json={"detail": "no run", "code": "not_found"})
            if run["status"] != "running":
                return httpx.Response(409, json={"detail": "closed", "code": "illegal_transition"})
            run["status"] = body["status"]
            return httpx.Response(200, json=run)
        if request.method == "GET" and path.endswith("/tree"):
            run_id = path.split("/")[3]
            return httpx.Response(200, json={"run": self.runs.get(run_id), "artifacts": [
                a for a in self.artifacts.values() if a["producer"]["run_id"] == run_id
            ], "children": []})
        if request.method == "POST" and path == "/v0/artifacts":
            run = self.runs.get(str(body["run_id"]))
            if run is None or run["status"] != "running":
                return httpx.Response(409, json={"detail": "run not open", "code": "illegal_transition"})
            artifact = {"id": str(uuid.uuid4()), "kind": body["kind"], "content": body.get("content"),
                        "properties": body.get("properties", {}),
                        "producer": {"agent_id": run["agent_id"], "run_id": run["id"]},
                        "sources": body.get("sources", []),
                        "namespaces": [{"namespace": run["namespace"], "tier": "provenance",
                                        "status": "confirmed", "source": "run"}]}
            self.artifacts[artifact["id"]] = artifact
            return httpx.Response(201, json=artifact)
        if request.method == "GET" and path.startswith("/v0/artifacts/"):
            parts = path.split("/")
            artifact = self.artifacts.get(parts[3])
            if artifact is None:
                return httpx.Response(404, json={"detail": "no artifact", "code": "not_found"})
            if path.endswith("/lineage"):
                return httpx.Response(200, json={"id": artifact["id"], "children": [
                    {"id": s, "children": []} for s in artifact["sources"]]})
            return httpx.Response(200, json={**artifact, "edges": [
                {"other_id": s, "edge_type": "derived_from", "direction": "out"} for s in artifact["sources"]]})
        if request.method == "POST" and path == "/v0/search":
            q = (body.get("q") or "").lower()
            items = [{"id": a["id"], "kind": a["kind"], "preview": (a["content"] or "")[:200],
                      "namespaces": [n["namespace"] for n in a["namespaces"]]}
                     for a in self.artifacts.values() if q in (a["content"] or "").lower()]
            return httpx.Response(200, json={"items": items[: body.get("limit") or 50], "next_cursor": None})
        if request.method == "GET" and path == "/v0/namespaces":
            return httpx.Response(200, json=list(self.namespaces.values()))
        return httpx.Response(404, json={"detail": f"no route {request.method} {path}", "code": "not_found"})


def _resolver(cwd):
    return {"/w/repo": REPO, "/w/other": OTHER}.get(cwd or "", ProjectIdentity(
        id=f"path:{cwd}", slug=project_slug(f"path:{cwd}"), name="x", root=cwd or "/",
    ))


class Harness:
    """A plane over a fresh ephemeral Loimi, with everything a real
    deployment would have provisioned by hand: the principal, its token,
    and the shared read scope."""

    def __init__(self, tmp_path, *, config: SessionsConfig | None = None,
                 operator=True, alive=None, transport_error=False, selector=None,
                 telemetry=None, service=None, store=True, store_error=False, environ=None):
        self.service = service or EphemeralMemoryService()
        self.fake_store = FakeStore()
        self.token = provision_profile(self.service, PRINCIPAL, MemoryScopesSpec(
            read=[SHARED], propose=[SHARED], default_write=SHARED,
        ))
        self.config = config or SessionsConfig(
            principal=PRINCIPAL,
            scopes=ScopePolicy(shared_read=[SHARED], provision="auto"),
            recall=SessionsRecall(enabled=selector is not None, on_prompt=True, min_prompt_chars=5),
            housekeeping=Housekeeping(retention_hours=1, stale_after_minutes=30, sweep_interval_seconds=5),
        )
        self.environ = {"LOIMI_MEMORY_URL": "http://loimi.test", "LOIMI_MEMORY_TOKEN": self.token}
        if environ is not None:
            self.environ = environ
        if operator:
            self.environ["LOIMI_OPERATOR_TOKEN"] = self.service.operator_token
        self.alive = set(alive or {4242})
        self.state_dir = tmp_path / "state"

        def failing_transport(request):
            raise httpx.ConnectError("connection refused", request=request)

        transport = httpx.MockTransport(failing_transport) if transport_error else self.service.transport()
        self.transport = transport
        if store:
            store_transport = (httpx.MockTransport(failing_transport) if store_error
                               else self.fake_store.transport())
            store_client = StoreClient(
                self.config.store, environ=self.environ, transport=store_transport,
                base_url="http://loimi.test", token=STORE_TOKEN,
            )
        else:
            store_client = None
        self.plane = SessionPlane(
            self.config, state_dir=self.state_dir, environ=self.environ,
            client_factory=lambda spec: MemoryClient(
                spec, transport=transport, base_url="http://loimi.test", token=self.token),
            operator_client_factory=lambda tok: MemoryClient(
                _dummy_spec(), transport=transport, base_url="http://loimi.test", token=tok),
            resolver=_resolver, is_alive=lambda pid: pid in self.alive,
            selector=selector, telemetry=telemetry,
            store_client=store_client, local_host=LOCAL_HOST,
        )
        if not store:
            self.plane.store = None

    async def send(self, *args, **kwargs):
        result = await self.plane.handle(envelope(*args, **kwargs))
        return result

    async def drain(self):
        await self.plane.drain()

    def events(self, kind_prefix: str | None = None):
        events = list(self.service.events.values())
        if kind_prefix:
            events = [e for e in events if str(e["source"]["kind"]).startswith(kind_prefix)]
        return events

    def context_state(self, scope=PROJECT_SCOPE):
        for context in self.service.contexts.values():
            if context["scope_id"] == scope:
                return context["state"]
        return None


def _dummy_spec():
    from miragen.models import MemorySpec

    return MemorySpec(scopes=MemoryScopesSpec(read=[], propose=["x"], default_write="x"))


# ── project identity + scope policy (pure) ──────────────────────────────────


class TestProjectIdentity:
    @pytest.mark.parametrize("url,expected", [
        ("git@github.com:org/Repo.git", "github.com/org/repo"),
        ("https://user@github.com/org/repo/", "github.com/org/repo"),
        ("ssh://git@gitea.local:2222/org/repo.git", "gitea.local/org/repo"),
        ("https://github.com/org/repo", "github.com/org/repo"),
    ])
    def test_remote_normalization(self, url, expected):
        assert normalize_remote(url) == expected

    def test_slug_is_scope_id_safe(self):
        slug = project_slug("path:/home/ilari/Software/Repositories/movingfirm-backend")
        assert slug == "path-home-ilari-software-repositories-movingfirm-backend"
        assert f"group:project.{slug}"[:128] == f"group:project.{slug}"

    def test_template_and_binding(self):
        policy = ScopePolicy(shared_read=[SHARED])
        templated = assign_scopes(policy, [], REPO)
        assert (templated.write, templated.templated) == (PROJECT_SCOPE, True)
        assert templated.read == [SHARED, PROJECT_SCOPE]
        bound = assign_scopes(policy, [ProjectBinding(
            match="git@github.com:org/repo.git", scope="group:repo-x", read=["shared:platform"],
        )], REPO)
        assert (bound.write, bound.templated) == ("group:repo-x", False)
        assert bound.read == [SHARED, "shared:platform", "group:repo-x"]
        by_path = assign_scopes(policy, [ProjectBinding(match="/w", scope="group:w")], REPO)
        assert by_path.write == "group:w"

    def test_config_file_round_trip(self, tmp_path):
        path = tmp_path / "sessions.yaml"
        path.write_text("principal: assistant\nscopes:\n  shared_read: [profile:assistant]\n"
                        "projects:\n  - match: github.com/org/repo\n    scope: group:x\n")
        config = load_sessions_config(path)
        assert config.projects[0].scope == "group:x"
        assert config.recall.on_prompt is True
        path.write_text("principal: assistant\nscopes:\n  project_scope: no-slug-here\n")
        with pytest.raises(ValueError, match="slug"):
            load_sessions_config(path)


def test_file_secrets_resolve_before_use(tmp_path):
    """The daemon reads MIRAGEND_TOKEN and the Loimi credentials from
    *_FILE variables the way the systemd unit delivers them; an empty
    (defined) value must stay empty, never fall back to a default."""
    from miragen.daemon.sessions.config import load_file_secrets

    secret = tmp_path / "token"
    secret.write_text("s3cret\n")
    env = {"MIRAGEND_TOKEN_FILE": str(secret), "LOIMI_MEMORY_TOKEN": "",
           "MISSING_FILE": str(tmp_path / "nope")}
    load_file_secrets(env)
    assert env["MIRAGEND_TOKEN"] == "s3cret"
    assert "MIRAGEND_TOKEN_FILE" not in env
    assert env["LOIMI_MEMORY_TOKEN"] == ""
    assert "MISSING" not in env and "MISSING_FILE" in env


# ── the lifecycle paths ─────────────────────────────────────────────────────


class TestSessionStart:
    async def test_registers_provisions_and_injects(self, tmp_path):
        h = Harness(tmp_path)
        result = await h.send("SessionStart", source="startup")
        assert result.state == "active"
        assert result.context is not None
        assert "[session context" in result.context
        assert "project=github.com/org/repo" in result.context
        assert "[memory guide v" in result.context
        assert "working state" in result.context
        assert result.detail is None  # not degraded

        session = h.plane.registry.get("claude-code:s-1")
        assert session.project.id == "github.com/org/repo"
        assert session.scope == PROJECT_SCOPE
        assert session.pid == 4242 and session.cwd == "/w/repo"
        assert PROJECT_SCOPE in h.service.service_scopes() if hasattr(h.service, "service_scopes") else PROJECT_SCOPE in h.service.scopes
        assert (PRINCIPAL, PROJECT_SCOPE, "propose") in h.service.grants
        stats = h.plane.stats
        assert (stats.sessions_registered, stats.provisioned_scopes, stats.injections) == (1, 1, 1)
        assert stats.retrieval_failures == 0
        assert stats.last_loimi_ok_at is not None
        # A manifest recorded the injection (what was ACTUALLY injected).
        assert any(m["run_ref"] == "claude-code:s-1" for m in h.service.manifests)

    async def test_second_session_same_project_reuses_scope(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("SessionStart", source="startup", session="s-2", pid=4243)
        assert h.plane.stats.provisioned_scopes == 1
        assert len(h.plane.registry.list(active_only=True)) == 2
        assert len(h.plane._lifecycles) == 1

    async def test_projects_are_isolated(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("SessionStart", source="startup", session="s-2", cwd="/w/other")
        a = h.plane.registry.get("claude-code:s-1")
        b = h.plane.registry.get("claude-code:s-2")
        assert a.scope != b.scope
        spec_a = h.plane._lifecycles[a.scope].spec
        assert b.scope not in spec_a.scopes.read
        assert spec_a.scopes.read == [SHARED, a.scope]

    async def test_explicit_binding_wins_and_is_not_provisioned(self, tmp_path):
        config = SessionsConfig(
            principal=PRINCIPAL, scopes=ScopePolicy(shared_read=[SHARED]),
            projects=[ProjectBinding(match="github.com/org/repo", scope="group:repo-x")],
            recall=SessionsRecall(enabled=False),
        )
        h = Harness(tmp_path, config=config)
        # Bound scopes are the operator's responsibility: provision by hand.
        h.service.scopes["group:repo-x"] = {"kind": "group"}
        for verb in ("read", "propose", "resolve", "retract"):
            h.service.grants.add((PRINCIPAL, "group:repo-x", verb))
        result = await h.send("SessionStart", source="startup")
        assert "scope=group:repo-x" in result.context
        assert h.plane.stats.provisioned_scopes == 0

    async def test_unprovisioned_manual_scope_degrades_explicitly(self, tmp_path):
        config = SessionsConfig(
            principal=PRINCIPAL, scopes=ScopePolicy(shared_read=[SHARED], provision="manual"),
            recall=SessionsRecall(enabled=False),
        )
        h = Harness(tmp_path, config=config)
        result = await h.send("SessionStart", source="startup")
        assert result.context is not None
        assert "MEMORY DEGRADED" in result.context
        assert result.detail and "refused" in result.detail
        assert h.plane.stats.retrieval_failures == 1
        assert h.plane.stats.last_loimi_error

    async def test_auto_without_operator_uses_fallback_or_degrades(self, tmp_path):
        config = SessionsConfig(
            principal=PRINCIPAL,
            scopes=ScopePolicy(shared_read=[SHARED], provision="auto", fallback_write=SHARED),
            recall=SessionsRecall(enabled=False),
        )
        h = Harness(tmp_path, config=config, operator=False)
        result = await h.send("SessionStart", source="startup")
        assert "scope=profile:assistant" in result.context
        assert "MEMORY DEGRADED" not in result.context
        assert "not provisioned" in result.detail and "fallback profile:assistant" in result.detail
        assert PROJECT_SCOPE in h.plane.describe()["scopes"]["unprovisionable"]

        strict = SessionsConfig(principal=PRINCIPAL, scopes=ScopePolicy(
            shared_read=[SHARED], provision="auto"), recall=SessionsRecall(enabled=False))
        h2 = Harness(tmp_path / "2", config=strict, operator=False)
        result = await h2.send("SessionStart", source="startup")
        # No fallback: the project scope is tried anyway and Loimi's refusal
        # reaches the model as an explicit degradation, never as silence.
        assert result.context is not None and "MEMORY DEGRADED" in result.context
        assert "not provisioned" in result.detail and "refused" in result.detail
        assert h2.plane.stats.retrieval_failures == 1

    async def test_no_cwd_means_no_scope_never_a_random_one(self, tmp_path):
        h = Harness(tmp_path)
        result = await h.plane.handle(EventEnvelope.model_validate({
            "harness": "codex", "session_id": "cx-1",
            "event": {"name": "context.started", "original_event": "SessionStart"},
            "client": {},
        }))
        assert result.context is None
        assert "no working directory" in result.detail
        await h.plane.handle(EventEnvelope.model_validate({
            "harness": "codex", "session_id": "cx-1",
            "event": {"name": "turn.finished", "original_event": "Stop", "content": "x"},
        }))
        await h.drain()
        assert h.events() == []
        assert h.plane.stats.capture_failures == 1


class TestCapture:
    async def test_prompt_and_turn_are_captured_idempotently(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("UserPromptSubmit", prompt="fix the flaky test", prompt_id="p-1")
        await h.send("Stop", last_assistant_message="fixed it", prompt_id="p-1")
        await h.drain()
        kinds = sorted(e["source"]["kind"] for e in h.events("harness:"))
        assert kinds == ["harness:input.received", "harness:turn.finished"]
        prompt_event = next(e for e in h.events("harness:input"))
        assert prompt_event["content"] == "fix the flaky test"
        assert prompt_event["attributes"]["harness"] == "claude-code"
        assert prompt_event["attributes"]["instance"] == REPO.slug

        # Redelivery (hook retried / journal replayed) writes nothing new.
        await h.send("Stop", last_assistant_message="fixed it", prompt_id="p-1")
        await h.drain()
        assert len(h.events("harness:")) == 2
        session = h.plane.registry.get("claude-code:s-1")
        assert (session.counters.prompts, session.counters.turns) == (1, 2)
        assert session.prompts == ["fix the flaky test"]

    async def test_tool_failure_and_children(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("PostToolUseFailure", tool_name="Bash", tool_use_id="t-1",
                     error="Exit code 1", tool_input={"command": "secret"})
        await h.send("SubagentStart", agent_id="a-1", agent_type="Explore")
        await h.send("SubagentStop", agent_id="a-1", agent_type="Explore",
                     last_assistant_message="found it")
        await h.drain()
        session = h.plane.registry.get("claude-code:s-1")
        assert session.counters.tool_failures == 1
        assert session.children["a-1"].agent_type == "Explore"
        assert session.children["a-1"].finished_at is not None
        kinds = sorted(e["source"]["kind"] for e in h.events("harness:"))
        assert kinds == ["harness:context.child_finished", "harness:tool.finished"]
        assert "secret" not in json.dumps(h.events("harness:tool"))

    async def test_parent_session_is_preserved(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup", harness="codex", session="child",
                     client_extra={"parent_session": "claude-code:s-1", "agent": "reviewer"})
        session = h.plane.registry.get("codex:child")
        assert session.parent_session == "claude-code:s-1"
        assert session.agent == "reviewer"


class TestCompactionAndFinalization:
    async def test_precompact_writes_episode_and_checkpoint_then_restore_sees_it(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("UserPromptSubmit", prompt="migrate the booking table", prompt_id="p-1")
        await h.send("Stop", last_assistant_message="migration written, tests green", prompt_id="p-1")
        await h.send("PreCompact", compaction_trigger="auto")
        await h.drain()

        episodes = h.events("session_episode")
        assert len(episodes) == 1
        content = episodes[0]["content"]
        assert "Session episode (compact-1)" in content
        assert "migrate the booking table" in content
        assert "migration written, tests green" in content
        assert episodes[0]["attributes"]["occurrence"] == "compact-1"
        assert episodes[0]["attributes"]["project"] == "github.com/org/repo"

        state = h.context_state()
        assert state["last_session"]["occurrence"] == "compact-1"
        assert state["last_session"]["last_prompt"] == "migrate the booking table"
        assert h.plane.stats.checkpoints == 1

        restored = await h.send("SessionStart", source="compact")
        assert "migrate the booking table" in restored.context  # working state came back
        assert h.plane.registry.get("claude-code:s-1").counters.injections == 2

    async def test_session_end_finalizes_idempotently_and_clears_journal(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("UserPromptSubmit", prompt="hello there", prompt_id="p-1")
        journal_file = next((h.state_dir / "journal").glob("*.jsonl"))
        assert journal_file.exists()
        await h.send("SessionEnd", reason="prompt_input_exit")
        await h.drain()
        session = h.plane.registry.get("claude-code:s-1")
        assert (session.state, session.end_reason) == ("ended", "prompt_input_exit")
        assert session.episodes_written == ["end"]
        assert not journal_file.exists()
        assert h.plane.stats.episodes == 1

        await h.send("SessionEnd", reason="prompt_input_exit")
        await h.drain()
        assert len(h.events("session_episode")) == 1
        assert len(h.events("harness:context.closed")) == 1


class TestDegradation:
    async def test_loimi_unreachable_fails_open_and_is_counted(self, tmp_path):
        h = Harness(tmp_path, transport_error=True)
        result = await h.send("SessionStart", source="startup")
        assert result.context is not None
        assert "MEMORY DEGRADED" in result.context
        assert "unreachable" in result.detail
        await h.send("Stop", last_assistant_message="x")
        await h.send("SessionEnd", reason="other")
        await h.drain()
        stats = h.plane.stats
        assert stats.retrieval_failures == 1
        assert stats.capture_failures >= 2  # the Stop capture + finalization
        assert "unreachable" in stats.last_loimi_error
        assert stats.last_loimi_ok_at is None
        # Unreachability is transient: the scope is NOT marked unprovisionable.
        assert h.plane.describe()["scopes"]["unprovisionable"] == {}
        # The journal is kept: nothing was written, so replay can still do it.
        assert list((h.state_dir / "journal").glob("*.jsonl"))
        assert await h.plane.probe_loimi() is False

    async def test_journal_replays_after_restart_into_a_healthy_loimi(self, tmp_path):
        service = EphemeralMemoryService()
        broken = Harness(tmp_path, transport_error=True, service=service)
        await broken.send("SessionStart", source="startup")
        await broken.send("UserPromptSubmit", prompt="remember this", prompt_id="p-1")
        await broken.send("SessionEnd", reason="other")
        await broken.drain()
        assert broken.events() == []

        healthy = Harness(tmp_path, service=service)  # same state dir, same store
        await healthy.plane.start()
        await healthy.drain()
        await healthy.plane.stop()
        assert healthy.plane.stats.replayed == 2
        kinds = sorted(e["source"]["kind"] for e in healthy.events())
        assert kinds == ["harness:context.closed", "harness:input.received", "session_episode"]
        assert not list((healthy.state_dir / "journal").glob("*.jsonl"))
        # The registry survived the restart with what the session did.
        session = healthy.plane.registry.get("claude-code:s-1")
        assert session.state == "ended"
        assert session.prompts == ["remember this"]

    async def test_sweep_finalizes_a_dead_process(self, tmp_path):
        h = Harness(tmp_path, alive={4242})
        await h.send("SessionStart", source="startup")
        await h.send("UserPromptSubmit", prompt="do the thing", prompt_id="p-1")
        await h.drain()
        h.alive.clear()  # claude was killed; SessionEnd never fires
        session = h.plane.registry.get("claude-code:s-1")
        session.last_seen_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        assert await h.plane.sweep() == 1
        await h.drain()
        assert (session.state, session.end_reason) == ("stale", "process_gone")
        assert len(h.events("session_episode")) == 1
        assert h.context_state()["last_session"]["end_reason"] == "process_gone"
        assert h.plane.stats.finalized_by_sweep == 1

        # Ended sessions are pruned after retention; the journal goes with them.
        session.ended_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        await h.plane.sweep()
        assert h.plane.registry.get("claude-code:s-1") is None

    async def test_late_event_after_end_reactivates(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("SessionEnd", reason="other")
        await h.drain()
        await h.send("UserPromptSubmit", prompt="still here", prompt_id="p-9")
        assert h.plane.registry.get("claude-code:s-1").state == "active"


class TestPromptRecall:
    async def test_prompt_recall_injects_selected_memory_only(self, tmp_path):
        calls = []

        async def selector(request, cards):
            calls.append(request)
            return SelectionResult(selections=[
                Selection(record_id=c["record_id"], reason="same vault mount")
                for c in cards if "vault" in c["payload"]["text"]
            ])

        h = Harness(tmp_path, selector=selector)
        await h.send("SessionStart", source="startup")
        lifecycle = h.plane._lifecycles[PROJECT_SCOPE]
        remembered = await lifecycle.remember(
            instance=REPO.slug, run_id="seed",
            content="the hel1 deploy needs the vault mounted first",
        )
        assert remembered["status"] == "accepted"
        await lifecycle.remember(instance=REPO.slug, run_id="seed2", content="lunch was good")

        result = await h.send("UserPromptSubmit", prompt="why does the hel1 deploy fail on vault",
                              prompt_id="p-1")
        assert result.context is not None
        assert "[recalled memories" in result.context
        assert "vault mounted" in result.context
        assert "lunch" not in result.context
        assert "[memory guide" not in result.context  # not re-injected per prompt
        assert h.plane.stats.prompt_recalls == 1
        assert len(calls) == 1
        assert any(m["policy"]["lane"] == "optional" for m in h.service.manifests)

        short = await h.send("UserPromptSubmit", prompt="ok", prompt_id="p-2")
        assert short.context is None  # under min_prompt_chars: no selector call
        assert len(calls) == 1


# ── HTTP surface ─────────────────────────────────────────────────────────────


class TestHttp:
    def _client(self, tmp_path, token="", telemetry=None):
        h = Harness(tmp_path, telemetry=telemetry)
        app = create_app(None, token=token, sessions=h.plane)
        return h, TestClient(app)

    def test_health_advertises_sessions_without_lifecycle(self, tmp_path):
        h, client = self._client(tmp_path)
        with client:
            body = client.get("/health").json()
        assert body["capabilities"] == ["sessions/v1", "bridge-mcp/v1"]
        assert not set(DAEMON_CAPABILITIES) & set(body["capabilities"])
        assert body["sessions"]["mcp"] == {"enabled": True, "default_project": "mcp:default",
                                           "oauth": False}
        assert body["sessions"]["store"]["configured"] is True
        assert body["sessions"]["principal"] == PRINCIPAL
        assert body["sessions"]["loimi"]["credential_configured"] is True
        assert body["sessions"]["stats"]["events_received"] == 0
        assert client.get("/agents").status_code == 404  # lifecycle plane absent

    def test_event_round_trip_auth_and_listing(self, tmp_path):
        h, client = self._client(tmp_path, token="secret")
        with client:
            unauth = client.post("/sessions/v1/events", json=envelope("Stop").model_dump())
            assert unauth.status_code == 401
            headers = {"Authorization": "Bearer secret"}
            start = client.post("/sessions/v1/events", headers=headers,
                                json=envelope("SessionStart", source="startup").model_dump())
            assert start.status_code == 200
            body = start.json()
            assert body["accepted"] is True and "[memory guide" in body["context"]
            stop = client.post("/sessions/v1/events", headers=headers,
                               json=envelope("Stop", last_assistant_message="d").model_dump())
            assert stop.json()["context"] is None
            listing = client.get("/sessions/v1/sessions?active=true", headers=headers).json()
            assert listing["count"] == 1
            assert listing["sessions"][0]["harness"] == "claude-code"
            assert "prompts" not in listing["sessions"][0]
            one = client.get("/sessions/v1/sessions/claude-code:s-1", headers=headers).json()
            assert one["project"]["id"] == "github.com/org/repo"
            missing = client.get("/sessions/v1/sessions/nope", headers=headers)
            assert (missing.status_code, missing.json()["code"]) == (404, "session_not_found")
            stats = client.get("/sessions/v1/stats", headers=headers).json()
            assert stats["stats"]["events_received"] == 2
            assert client.get("/health").json()["sessions"]["active_sessions"] == 1

    def test_malformed_events_are_rejected_and_counted(self, tmp_path):
        h, client = self._client(tmp_path)
        with client:
            bad = client.post("/sessions/v1/events", json={"harness": "claude-code"})
            assert (bad.status_code, bad.json()["code"]) == (422, "malformed_event")
            unknown_event = client.post("/sessions/v1/events", json={
                "harness": "claude-code", "session_id": "s",
                "event": {"name": "context.exploded", "original_event": "Boom"},
            })
            assert unknown_event.status_code == 422
            not_json = client.post("/sessions/v1/events", content=b"{nope",
                                   headers={"Content-Type": "application/json"})
            assert not_json.status_code == 422
            # A newer adapter's extra fields are tolerated, not rejected.
            newer = client.post("/sessions/v1/events", json={
                **envelope("Stop", last_assistant_message="d").model_dump(),
                "future_field": 1, "client": {"pid": 4242, "cwd": "/w/repo", "gpu": "yes"},
            })
            assert newer.status_code == 200
        assert h.plane.stats.events_rejected == 3
        assert h.plane.stats.events_received == 1

    def test_spans_carry_mechanical_facts_only(self, tmp_path):
        exporter = InMemorySpanExporter()
        telemetry = MiragenTelemetry(endpoint="unused", agent_name="miragend",
                                     service_name="miragend", span_exporter=exporter)
        h, client = self._client(tmp_path, telemetry=telemetry)
        with client:
            client.post("/sessions/v1/events",
                        json=envelope("UserPromptSubmit", prompt="the secret plan",
                                      prompt_id="p-1").model_dump())
        telemetry.provider.force_flush()
        spans = [s for s in exporter.get_finished_spans() if s.name == "session.event"]
        assert len(spans) == 1
        attrs = dict(spans[0].attributes)
        assert attrs["mira.session.harness"] == "claude-code"
        assert attrs["mira.run.id"] == "claude-code:s-1"
        assert attrs["mira.run.trigger"] == "input.received"
        assert attrs["mira.project.id"] == "github.com/org/repo"
        assert "secret plan" not in json.dumps(attrs)


class TestStatusLine:
    """The opening context always ends with one `[memory status]` line, so
    an agent never has to read meaning into silence (memory-effectiveness
    P3)."""

    async def test_recall_off_is_said_out_loud(self, tmp_path):
        h = Harness(tmp_path)
        result = await h.send("SessionStart", source="startup")
        status = result.context.splitlines()[-1]
        assert status.startswith("[memory status] automatic recall is OFF")
        assert "memory_recall searches on demand" in status
        assert status.endswith("capture ok")

    async def test_recall_on_without_a_query_says_it_runs_per_prompt(self, tmp_path):
        async def selector(request, cards):
            return SelectionResult(selections=[])

        h = Harness(tmp_path, selector=selector)
        result = await h.send("SessionStart", source="startup")
        assert "automatic recall on; it runs on each prompt of 5+ characters" in result.context.splitlines()[-1]

    async def test_recent_capture_failures_are_announced(self, tmp_path):
        h = Harness(tmp_path)
        h.plane.stats.note_capture(True, PROJECT_SCOPE)
        h.plane.stats.note_capture(False, PROJECT_SCOPE)
        result = await h.send("SessionStart", source="startup")
        assert "capture FAILING: 1 of 2 recent writes to this project" in result.context.splitlines()[-1]
        snapshot = h.plane.stats.snapshot()
        assert snapshot["recent_capture_failures"] == 1
        assert "recent_captures" not in snapshot  # the deque never reaches /health

    async def test_other_projects_and_old_failures_are_not_blamed(self, tmp_path):
        import time as _time

        from miragen.daemon.sessions import plane as plane_mod

        h = Harness(tmp_path)
        h.plane.stats.note_capture(False, "group:project.somewhere-else")
        h.plane.stats.recent_captures.append(
            (_time.monotonic() - plane_mod.RECENT_CAPTURE_SECONDS - 1, PROJECT_SCOPE, False))
        result = await h.send("SessionStart", source="startup")
        assert result.context.splitlines()[-1].endswith("capture ok")

    async def test_failed_episodes_count_as_capture_failures(self, tmp_path):
        h = Harness(tmp_path)
        h.plane.stats.note_outcome(False, PROJECT_SCOPE)  # what a lost episode records
        result = await h.send("SessionStart", source="startup")
        assert "capture FAILING" in result.context.splitlines()[-1]

    async def test_no_tools_means_no_tool_advice(self, tmp_path):
        h = Harness(tmp_path)
        h.plane.config.mcp.enabled = False
        status = (await h.send("SessionStart", source="startup")).context.splitlines()[-1]
        assert "automatic recall is OFF" in status and "memory_recall" not in status

    async def test_recall_only_at_open_is_not_claimed_per_prompt(self, tmp_path):
        async def selector(request, cards):
            return SelectionResult(selections=[])

        h = Harness(tmp_path, selector=selector)
        h.plane.config.recall.on_prompt = False
        status = (await h.send("SessionStart", source="startup")).context.splitlines()[-1]
        assert "only at session open" in status and "each prompt" not in status

    async def test_retrieval_timeout_is_announced_once_not_every_prompt(self, tmp_path, monkeypatch):
        import asyncio as _asyncio

        from miragen.daemon.sessions import plane as plane_mod
        from miragen.memory.lifecycle import MemoryLifecycle

        async def hang(self, **kwargs):
            await _asyncio.sleep(1)

        monkeypatch.setattr(plane_mod, "RETRIEVAL_TIMEOUT_S", 0.05)
        monkeypatch.setattr(MemoryLifecycle, "prepare_context", hang)
        h = Harness(tmp_path)
        first = await h.send("SessionStart", source="startup")
        assert first.context.startswith("[memory status] memory UNAVAILABLE for this session (retrieval timed out)")
        again = await h.send("UserPromptSubmit", prompt="still slow?", prompt_id="p-1")
        assert again.context is None  # the late-open retry stays quiet

    async def test_injected_memories_are_counted_and_citation_asked(self, tmp_path):
        from miragen.memory.lifecycle import MemoryPacket

        h = Harness(tmp_path)
        packet = MemoryPacket(text="", optional_status="ok", items=[
            {"kind": "working_state"}, {"revision_id": "r1"}, {"revision_id": "r2"}])
        line = h.plane._status_line(packet, project_scope="group:project.x")
        assert "2 memories in group:project.x injected above — cite the ids" in line


def test_only_rendered_recall_entries_are_tracked():
    """Entries past the optional budget are not in the text, so they must
    not be in the manifest or counted for citation either."""
    from miragen.memory.selection import fit_optional_entries, render_optional_section

    entries = [{"record_id": f"rec-{i}aaaaaa", "type": "claim", "text": "x" * 60 + "\nline two",
                "reason": "r"} for i in range(5)]
    fitted = fit_optional_entries(entries, 250)
    section = render_optional_section(entries, 250)
    assert 0 < len(fitted) < len(entries)
    assert all(e["record_id"][:8] in section for e in fitted)
    assert not any(e["record_id"][:8] in section for e in entries[len(fitted):])


@pytest.mark.parametrize("status,degraded,expected", [
    ("empty", None, "searched in group:project.x: nothing stored matches yet"),
    ("none_selected", None, "searched in group:project.x: nothing relevant to this"),
    ("degraded: selector: boom", None, "recall DEGRADED"),
    (None, "prepare: unreachable", "recall DEGRADED"),
])
def test_status_line_recall_states(tmp_path, status, degraded, expected):
    from miragen.memory.lifecycle import MemoryPacket

    h = Harness(tmp_path)
    packet = MemoryPacket(text="", optional_status=status, degraded=degraded)
    assert expected in h.plane._status_line(packet, project_scope="group:project.x")


class TestEpisodeRefinalize:
    async def test_refinalize_with_a_different_digest_is_dedupe_not_failure(self, tmp_path):
        """First digest per occurrence wins. A re-finalize whose digest
        differs (journal replay rebuilding it from events that now capture —
        seen on the VPS right after the #111 deploy) must not count as a
        lost write or degrade memory."""
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("UserPromptSubmit", prompt="hello there", prompt_id="p-1")
        await h.send("SessionEnd", reason="prompt_input_exit")
        await h.drain()
        session = h.plane.registry.get("claude-code:s-1")
        failures = h.plane.stats.capture_failures
        session.turns.append("a turn the first digest never saw")
        await h.plane._finalize(session, occurrence="end")
        assert len(h.events("session_episode")) == 1
        assert h.plane.stats.capture_failures == failures
        assert "episode capture" not in (h.plane.stats.last_loimi_error or "")
        assert h.plane.stats.episodes == 1 and session.episodes_written == ["end"]
