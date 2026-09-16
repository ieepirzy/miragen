"""The hosted bridge (docs/design/hosted-bridge.md): sessions from other
hosts identified by the remote the adapter observed or by directory name
(with adoption), host-aware liveness, the raw HTTP hook endpoint, startup
provisioning of the principal and shared scopes, Loimi artifact-store
participation (a run per session, episodes as artifacts, namespace policy,
explicit degradation), and the bridge MCP surface (tools + the
bearer/OAuth guard)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from miragen.daemon.api import create_app
from miragen.daemon.sessions.config import (
    ScopePolicy,
    SessionsConfig,
    SessionsRecall,
    StoreNamespaceBinding,
    StorePolicy,
)
from miragen.daemon.sessions.projects import identity_from_directory, identity_from_remote
from miragen.memory.ephemeral import EphemeralMemoryService
from miragen_hook.normalize import normalize_hook_payload
from tests.test_sessions_plane import (
    LOCAL_HOST,
    PRINCIPAL,
    PROJECT_SCOPE,
    SHARED,
    STORE_TOKEN,
    Harness,
    envelope,
)

RAW_START = {"session_id": "cloud-1", "transcript_path": "/t.jsonl", "cwd": "/workspace/repo",
             "permission_mode": "default", "hook_event_name": "SessionStart", "source": "startup"}


def _config(**overrides) -> SessionsConfig:
    base = dict(
        principal=PRINCIPAL, scopes=ScopePolicy(shared_read=[SHARED], provision="auto"),
        recall=SessionsRecall(enabled=False),
    )
    base.update(overrides)
    return SessionsConfig(**base)


def _text(result) -> dict:
    """FastMCP.call_tool → the tool's JSON string, parsed."""
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


# ── identity from other hosts ────────────────────────────────────────────────


class TestRemoteIdentity:
    def test_identity_helpers(self):
        remote = identity_from_remote("git@github.com:Org/Repo.git", root="/workspace/Repo")
        assert (remote.id, remote.slug, remote.name) == (
            "github.com/org/repo", "github.com-org-repo", "repo")
        assert remote.root == "/workspace/Repo"
        by_dir = identity_from_directory("/workspace/Repo/")
        assert (by_dir.id, by_dir.name, by_dir.root) == ("dir:repo", "Repo", "/workspace/Repo")

    async def test_remote_hint_wins_over_daemon_filesystem(self, tmp_path):
        """A cloud VM's adapter reports the origin URL: the session lands in
        the SAME project (and scope) as a local session of that repository,
        even though its cwd means nothing on the daemon host."""
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")  # local, resolved via git
        result = await h.send(
            "SessionStart", source="startup", session="c-1", cwd="/workspace/repo",
            host="cloud-vm", remote=True, project_remote="https://github.com/org/repo.git",
        )
        cloud = h.plane.registry.get("claude-code:c-1")
        assert cloud.project.id == "github.com/org/repo"
        assert cloud.scope == PROJECT_SCOPE and cloud.remote is True and cloud.host == "cloud-vm"
        assert "host=cloud-vm" in result.context
        assert h.plane.stats.provisioned_scopes == 1  # shared scope, not a second one
        assert h.plane.stats.remote_sessions == 1

    async def test_directory_name_adopts_a_known_project(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")  # teaches the daemon "repo"
        await h.send("SessionStart", source="startup", session="c-1", cwd="/home/user/repo",
                     host="cloud-vm", remote=True)
        cloud = h.plane.registry.get("claude-code:c-1")
        assert cloud.project.id == "github.com/org/repo"
        assert cloud.project.root == "/home/user/repo"
        assert h.plane.stats.adopted_by_name == 1

    async def test_unknown_directory_name_stays_its_own_project(self, tmp_path):
        h = Harness(tmp_path)
        result = await h.send("SessionStart", source="startup", session="c-1",
                              cwd="/home/user/mystery", host="cloud-vm", remote=True)
        session = h.plane.registry.get("claude-code:c-1")
        assert session.project.id == "dir:mystery"
        assert session.scope == "group:project.dir-mystery"
        assert "project=dir:mystery" in result.context

    async def test_adoption_can_be_disabled(self, tmp_path):
        config = _config(scopes=ScopePolicy(shared_read=[SHARED], adopt_by_name=False))
        h = Harness(tmp_path, config=config)
        await h.send("SessionStart", source="startup")
        await h.send("SessionStart", source="startup", session="c-1", cwd="/home/user/repo",
                     host="cloud-vm", remote=True)
        assert h.plane.registry.get("claude-code:c-1").project.id == "dir:repo"

    async def test_known_projects_survive_a_restart(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.drain()
        h.plane.registry.save()
        again = Harness(tmp_path, service=h.service)
        await again.send("SessionStart", source="startup", session="c-2", cwd="/x/repo",
                         host="elsewhere", remote=True)
        assert again.plane.registry.get("claude-code:c-2").project.id == "github.com/org/repo"

    async def test_same_host_without_remote_hint_uses_the_filesystem(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup", host=LOCAL_HOST)
        session = h.plane.registry.get("claude-code:s-1")
        assert session.project.id == "github.com/org/repo" and session.remote is False

    def test_resolve_identity_forms(self, tmp_path):
        h = Harness(tmp_path)
        plane = h.plane
        assert plane.resolve_identity(None).id == "mcp:default"
        assert plane.resolve_identity("git@github.com:org/repo.git").id == "github.com/org/repo"
        assert plane.resolve_identity("github.com/org/repo").slug == "github.com-org-repo"
        assert plane.resolve_identity("mystery").id == "dir:mystery"

    async def test_resolve_identity_by_session_and_name(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        assert h.plane.resolve_identity("claude-code:s-1").id == "github.com/org/repo"
        assert h.plane.resolve_identity("repo").id == "github.com/org/repo"
        assert h.plane.resolve_identity("github.com-org-repo").id == "github.com/org/repo"


class TestHostAwareLiveness:
    async def test_remote_pid_is_never_checked(self, tmp_path):
        h = Harness(tmp_path, alive={4242})
        await h.send("SessionStart", source="startup")  # local, alive
        await h.send("SessionStart", source="startup", session="c-1", cwd="/w/repo",
                     host="cloud-vm", remote=True, pid=4242)  # same pid number, other host
        h.alive.clear()  # local process gone
        stale = await h.plane.sweep(now=datetime.now(timezone.utc) + timedelta(seconds=10))
        assert stale == 1
        assert h.plane.registry.get("claude-code:s-1").state == "stale"
        assert h.plane.registry.get("claude-code:c-1").state == "active"
        late = datetime.now(timezone.utc) + timedelta(minutes=31)
        assert await h.plane.sweep(now=late) == 1
        assert h.plane.registry.get("claude-code:c-1").end_reason == "silent"


# ── the raw HTTP hook endpoint ───────────────────────────────────────────────


class TestRawHookEndpoint:
    def _client(self, tmp_path, token="secret"):
        h = Harness(tmp_path)
        app = create_app(None, token=token, sessions=h.plane)
        return h, TestClient(app), {"Authorization": f"Bearer {token}"}

    def test_raw_session_start_answers_in_harness_shape(self, tmp_path):
        h, client, headers = self._client(tmp_path)
        with client:
            assert client.post("/sessions/v1/hooks/claude-code", json=RAW_START).status_code == 401
            answer = client.post("/sessions/v1/hooks/claude-code", json=RAW_START, headers=headers)
            assert answer.status_code == 200
            out = answer.json()["hookSpecificOutput"]
            assert out["hookEventName"] == "SessionStart"
            assert "[memory guide" in out["additionalContext"]
            assert "project=dir:repo" in out["additionalContext"]
            session = h.plane.registry.get("claude-code:cloud-1")
            assert session.remote is True and session.pid is None and session.host is None
            assert session.adapter == "http-hook"
            # A capture answers with an empty object, never an error.
            stop = client.post("/sessions/v1/hooks/claude-code", headers=headers, json={
                **RAW_START, "hook_event_name": "Stop", "last_assistant_message": "done"})
            assert (stop.status_code, stop.json()) == (200, {})
            # Unmapped events (PostToolUse success) are empty 200s too.
            ok = client.post("/sessions/v1/hooks/claude-code", headers=headers, json={
                **RAW_START, "hook_event_name": "PostToolUse", "tool_name": "Bash"})
            assert (ok.status_code, ok.json()) == (200, {})
            assert client.post("/sessions/v1/hooks/kimi", json={}, headers=headers).status_code == 404
            bad = client.post("/sessions/v1/hooks/claude-code", headers=headers, json=[1, 2])
            assert (bad.status_code, bad.json()["code"]) == (422, "malformed_event")
        assert h.plane.stats.events_rejected == 1

    async def test_raw_hook_adopts_known_project(self, tmp_path):
        h, client, headers = self._client(tmp_path)
        await h.send("SessionStart", source="startup")  # local: "repo" is github.com/org/repo
        with client:
            answer = client.post("/sessions/v1/hooks/claude-code", json=RAW_START, headers=headers)
        assert "project=github.com/org/repo" in answer.json()["hookSpecificOutput"]["additionalContext"]


# ── startup provisioning ─────────────────────────────────────────────────────


class TestStartupProvisioning:
    async def test_principal_from_environment_wins(self, tmp_path):
        h = Harness(tmp_path)
        assert await h.plane.ensure_principal() == "environment"

    async def test_principal_minted_when_it_exists(self, tmp_path):
        environ = {"LOIMI_MEMORY_URL": "http://loimi.test"}  # no credential
        h = Harness(tmp_path, environ=environ)  # provision_profile already created `assistant`
        assert await h.plane.ensure_principal() == "minted"
        token = environ["LOIMI_MEMORY_TOKEN"]
        assert token and h.service.tokens  # a real token the service knows
        assert (h.plane.state_dir / "principal.token").read_text().strip() == token
        # Restart: the persisted token is reused, nothing is minted again.
        environ2 = {"LOIMI_MEMORY_URL": "http://loimi.test"}
        again = Harness(tmp_path, environ=environ2, service=h.service)
        assert await again.plane.ensure_principal() == "state_dir"
        assert environ2["LOIMI_MEMORY_TOKEN"] == token

    async def test_principal_created_on_a_fresh_loimi(self, tmp_path):
        service = EphemeralMemoryService()
        environ = {"LOIMI_MEMORY_URL": "http://loimi.test"}
        h = Harness(tmp_path, environ=environ, service=service)
        service.principals.clear()  # provision_profile ran in Harness; undo it
        assert await h.plane.ensure_principal() == "created"
        assert PRINCIPAL in service.principals

    async def test_missing_operator_degrades_explicitly(self, tmp_path):
        environ = {"LOIMI_MEMORY_URL": "http://loimi.test"}
        h = Harness(tmp_path, environ=environ, operator=False)
        assert await h.plane.ensure_principal() == "missing"
        assert "LOIMI_MEMORY_TOKEN" not in environ

    async def test_provisioning_can_be_disabled(self, tmp_path):
        environ = {"LOIMI_MEMORY_URL": "http://loimi.test"}
        h = Harness(tmp_path, environ=environ, config=_config(provision_principal=False))
        assert await h.plane.ensure_principal() == "missing"

    async def test_shared_scopes_are_ensured(self, tmp_path):
        service = EphemeralMemoryService()
        h = Harness(tmp_path, service=service)
        service.scopes.pop(SHARED, None)
        service.grants.discard((PRINCIPAL, SHARED, "read"))
        assert await h.plane.ensure_shared_scopes() == {SHARED: "ready"}
        assert service.scopes[SHARED]["kind"] == "profile"
        assert (PRINCIPAL, SHARED, "read") in service.grants
        # Idempotent: an existing scope is fine.
        assert await h.plane.ensure_shared_scopes() == {SHARED: "ready"}

    async def test_shared_scopes_without_operator_are_unverified(self, tmp_path):
        h = Harness(tmp_path, operator=False)
        assert (await h.plane.ensure_shared_scopes())[SHARED].startswith("unverified")

    def test_start_runs_provisioning_via_http_lifespan(self, tmp_path):
        environ = {"LOIMI_MEMORY_URL": "http://loimi.test"}
        h = Harness(tmp_path, environ=environ)
        app = create_app(None, sessions=h.plane)
        with TestClient(app) as client:
            body = client.get("/health").json()["sessions"]
        assert body["principal_source"] == "minted"
        assert body["loimi"]["shared_scopes"] == {SHARED: "ready"}
        assert body["host"] == LOCAL_HOST


# ── artifact store participation ─────────────────────────────────────────────


class TestArtifactStore:
    async def test_session_gets_a_run_and_episodes_become_artifacts(self, tmp_path):
        h = Harness(tmp_path)
        result = await h.send("SessionStart", source="startup")
        session = h.plane.registry.get("claude-code:s-1")
        assert session.run_id in h.fake_store.runs
        run = h.fake_store.runs[session.run_id]
        assert (run["agent_id"], run["namespace"], run["status"]) == ("mira", "mira", "running")
        assert "claude-code session s-1 in github.com/org/repo" == run["task"]
        assert f"store_run={session.run_id}" in result.context
        assert "namespace=mira" in result.context
        assert "[bridge tools]" in result.context and "store_put_artifact" in result.context
        assert "Tools: memory_checkpoint" in result.context  # guide advertises the tools

        await h.send("UserPromptSubmit", prompt="ship the bridge", prompt_id="p-1")
        await h.send("PreCompact", trigger="auto")
        await h.drain()
        artifacts = list(h.fake_store.artifacts.values())
        assert len(artifacts) == 1
        assert artifacts[0]["kind"] == "session_episode"
        assert artifacts[0]["properties"]["occurrence"] == "compact-1"
        assert artifacts[0]["properties"]["memory_event_id"]  # linked to the memory episode
        assert "ship the bridge" in artifacts[0]["content"]

        await h.send("SessionEnd", reason="exit")
        await h.drain()
        artifacts = list(h.fake_store.artifacts.values())
        assert [a["properties"]["occurrence"] for a in artifacts] == ["compact-1", "end"]
        assert h.fake_store.runs[session.run_id]["status"] == "succeeded"
        assert session.run_status == "succeeded"
        assert h.plane.stats.artifacts_written == 2 and h.plane.stats.runs_closed == 1
        assert h.context_state()["last_session"]["store_run"] == session.run_id

    async def test_replayed_finalization_does_not_duplicate_artifacts(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("UserPromptSubmit", prompt="work happened", prompt_id="p-1")
        await h.send("SessionEnd", reason="exit")
        await h.drain()
        session = h.plane.registry.get("claude-code:s-1")
        await h.plane._finalize(session, occurrence="end")  # a replay
        assert len(h.fake_store.artifacts) == 1

    async def test_swept_session_run_is_cancelled(self, tmp_path):
        h = Harness(tmp_path, alive={4242})
        await h.send("SessionStart", source="startup")
        h.alive.clear()
        await h.plane.sweep(now=datetime.now(timezone.utc) + timedelta(seconds=10))
        await h.drain()
        session = h.plane.registry.get("claude-code:s-1")
        assert h.fake_store.runs[session.run_id]["status"] == "cancelled"

    async def test_namespace_binding(self, tmp_path):
        config = _config(store=StorePolicy(namespaces=[
            StoreNamespaceBinding(match="git@github.com:org/repo.git", namespace="infra"),
        ]))
        h = Harness(tmp_path, config=config)
        await h.send("SessionStart", source="startup")
        await h.send("SessionStart", source="startup", session="s-2", cwd="/w/other")
        runs = {r["task"].split(" in ")[-1]: r["namespace"] for r in h.fake_store.runs.values()}
        assert runs == {"github.com/org/repo": "infra", "github.com/org/other": "mira"}

    def test_namespace_binding_forms(self, tmp_path):
        policy = StorePolicy(namespace="mira", namespaces=[
            StoreNamespaceBinding(match="https://github.com/Muutto365/*", namespace="muutto365"),
            StoreNamespaceBinding(match="/home/ilari/Software/Repositories/Loimi", namespace="infra"),
            StoreNamespaceBinding(match="github.com/org/repo", namespace="infra"),
        ])
        from miragen.daemon.sessions.store import StoreClient
        client = StoreClient(policy, environ={})
        assert client.namespace_for(identity_from_remote("git@github.com:muutto365/movingfirm-backend.git")) == "muutto365"
        assert client.namespace_for(identity_from_remote("git@github.com:org/repo.git")) == "infra"
        assert client.namespace_for(identity_from_directory("/home/ilari/Software/Repositories/Loimi")) == "infra"
        assert client.namespace_for(identity_from_remote("git@github.com:org/other.git")) == "mira"
        assert client.namespace_for(None) == "mira"

    async def test_store_unreachable_degrades_explicitly_memory_still_injected(self, tmp_path):
        h = Harness(tmp_path, store_error=True)
        result = await h.send("SessionStart", source="startup")
        assert "[memory guide" in result.context
        assert "store_run=" not in result.context
        assert "artifact store run not opened" in result.detail
        await h.send("UserPromptSubmit", prompt="work happened", prompt_id="p-1")
        await h.send("SessionEnd", reason="exit")
        await h.drain()
        assert h.plane.stats.store_failures >= 2
        assert h.plane.stats.episodes == 1  # memory side unaffected
        # Finalize retries the run first; that is the error it records.
        assert h.plane.describe()["store"]["last_error"].startswith("open run: artifact store unreachable")
        assert not h.fake_store.artifacts

    async def test_store_disabled(self, tmp_path):
        h = Harness(tmp_path, config=_config(store=StorePolicy(enabled=False)), store=False)
        result = await h.send("SessionStart", source="startup")
        assert result.detail is None and "store_run" not in result.context
        assert h.plane.describe()["store"] == {
            "enabled": False, "configured": False, "agent_id": None, "default_namespace": None,
            "last_ok_at": None, "last_error": None, "last_error_at": None}

    async def test_unknown_namespace_is_a_refusal_not_an_outage(self, tmp_path):
        h = Harness(tmp_path, config=_config(store=StorePolicy(namespace="nope")))
        result = await h.send("SessionStart", source="startup")
        assert "artifact store run not opened" in result.detail
        assert h.plane.stats.last_store_error.startswith("open run: store answered 422")


# ── the bridge MCP surface ───────────────────────────────────────────────────


class TestBridgeMcp:
    def _mcp(self, tmp_path, **kw):
        h = Harness(tmp_path, **kw)
        app = create_app(None, token="secret", sessions=h.plane)
        return h, app.state.bridge_mcp

    async def test_tool_inventory(self, tmp_path):
        _, mcp = self._mcp(tmp_path)
        names = {t.name for t in await mcp.list_tools()}
        assert names == {
            "bridge_status", "bridge_sessions",
            "memory_recall", "memory_read", "memory_remember", "memory_correct", "memory_checkpoint",
            "store_open_run", "store_close_run", "store_put_artifact", "store_get_artifact",
            "store_lineage", "store_search", "store_list_namespaces", "store_run_tree",
        }

    async def test_memory_tools_are_scoped_by_project(self, tmp_path):
        h, mcp = self._mcp(tmp_path)
        remembered = _text(await mcp.call_tool("memory_remember", {
            "content": "The bridge deploys to the agent stack.", "project": "github.com/org/repo"}))
        assert remembered["status"] == "accepted"
        assert remembered["scope"] == PROJECT_SCOPE
        assert h.plane.stats.provisioned_scopes == 1
        recalled = _text(await mcp.call_tool("memory_recall", {
            "query": "bridge agent stack", "project": "github.com/org/repo"}))
        assert recalled["status"] == "ok" and recalled["scopes"] == [SHARED, PROJECT_SCOPE]
        assert any(remembered["record_id"] == item.get("record_id") for item in recalled["items"])
        # Another project cannot see it.
        other = _text(await mcp.call_tool("memory_recall", {
            "query": "bridge agent stack", "project": "github.com/org/other"}))
        assert other["items"] == []
        read = _text(await mcp.call_tool("memory_read", {"record_id": remembered["record_id"]}))
        assert read["record_id"] == remembered["record_id"]
        checkpoint = _text(await mcp.call_tool("memory_checkpoint", {
            "state": {"goal": "ship"}, "project": "github.com/org/repo"}))
        assert checkpoint["status"] == "accepted"
        assert h.context_state()["goal"] == "ship"
        corrected = _text(await mcp.call_tool("memory_correct", {
            "record_id": remembered["record_id"], "correction": {"text": "It deploys to agent-stack."},
            "reason": "user said so", "project": "github.com/org/repo"}))
        assert corrected["status"] == "accepted"

    async def test_default_project_when_none_named(self, tmp_path):
        h, mcp = self._mcp(tmp_path)
        result = _text(await mcp.call_tool("memory_checkpoint", {"state": {"topic": "chat"}}))
        assert result["project"] == "mcp:default"
        assert result["scope"] == "group:project.mcp-default"

    async def test_store_tools_use_the_session_run(self, tmp_path):
        h, mcp = self._mcp(tmp_path)
        await h.send("SessionStart", source="startup")
        session = h.plane.registry.get("claude-code:s-1")
        sessions = _text(await mcp.call_tool("bridge_sessions", {}))
        assert sessions["sessions"][0]["run_id"] == session.run_id
        put = _text(await mcp.call_tool("store_put_artifact", {
            "kind": "review", "content": "Review: the bridge is sound.", "session": "claude-code:s-1",
            "properties": {"title": "bridge review"}}))
        assert put["status"] == "accepted"
        artifact = put["artifact"]
        assert artifact["producer"]["run_id"] == session.run_id
        assert f"tool:{artifact['id']}" in session.artifacts_written
        derived = _text(await mcp.call_tool("store_put_artifact", {
            "kind": "decision", "content": "Ship it.", "run_id": session.run_id,
            "sources": [artifact["id"]]}))
        lineage = _text(await mcp.call_tool("store_lineage", {"artifact_id": derived["artifact"]["id"]}))
        assert lineage["children"][0]["id"] == artifact["id"]
        found = _text(await mcp.call_tool("store_search", {"q": "sound"}))
        assert [i["id"] for i in found["items"]] == [artifact["id"]]
        got = _text(await mcp.call_tool("store_get_artifact", {"artifact_id": artifact["id"]}))
        assert got["kind"] == "review"
        tree = _text(await mcp.call_tool("store_run_tree", {"run_id": session.run_id}))
        assert len(tree["artifacts"]) == 2
        namespaces = _text(await mcp.call_tool("store_list_namespaces", {}))
        assert {n["id"] for n in namespaces} == {"mira", "infra", "muutto365"}
        no_run = _text(await mcp.call_tool("store_put_artifact", {"kind": "x", "content": "y"}))
        assert no_run["status"] == "rejected" and "store_open_run" in no_run["detail"]

    async def test_own_run_lifecycle_and_refusals(self, tmp_path):
        h, mcp = self._mcp(tmp_path)
        opened = _text(await mcp.call_tool("store_open_run", {
            "task": "audit", "project": "github.com/org/repo"}))
        assert opened["namespace"] == "mira" and opened["status"] == "running"
        closed = _text(await mcp.call_tool("store_close_run", {"run_id": opened["id"], "status": "failed"}))
        assert closed["status"] == "failed"
        again = _text(await mcp.call_tool("store_close_run", {"run_id": opened["id"]}))
        assert again == {"status": "rejected", "detail": "store answered 409: closed", "http_status": 409}
        missing = _text(await mcp.call_tool("store_get_artifact", {"artifact_id": "nope"}))
        assert missing["status"] == "rejected" and missing["http_status"] == 404

    async def test_store_outage_is_persistence_unavailable(self, tmp_path):
        h, mcp = self._mcp(tmp_path, store_error=True)
        result = _text(await mcp.call_tool("store_search", {"q": "x"}))
        assert result["status"] == "persistence_unavailable"
        status = _text(await mcp.call_tool("bridge_status", {}))
        assert status["store"]["last_error"].startswith("artifact store unreachable")

    async def test_store_disabled_is_a_tool_error(self, tmp_path):
        from mcp.server.fastmcp.exceptions import ToolError

        h, mcp = self._mcp(tmp_path, config=_config(store=StorePolicy(enabled=False)), store=False)
        with pytest.raises(ToolError, match="disabled"):
            await mcp.call_tool("store_search", {"q": "x"})

    def test_mount_guard_bearer(self, tmp_path):
        h = Harness(tmp_path)
        app = create_app(None, token="secret", sessions=h.plane)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}}
        headers = {"Accept": "application/json, text/event-stream"}
        with TestClient(app) as client:
            assert client.post("/mcp", json=init, headers=headers).status_code == 401
            assert client.post("/mcp/", json=init, headers=headers).status_code == 401
            assert client.post("/mcp", json=init, headers={
                **headers, "Authorization": "Bearer wrong"}).status_code == 401
            ok = client.post("/mcp", json=init, headers={**headers, "Authorization": "Bearer secret"})
            assert ok.status_code == 200
            assert ok.json()["result"]["serverInfo"]["name"] == "miragen-bridge"
            tools = client.post("/mcp", headers={**headers, "Authorization": "Bearer secret"},
                                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            assert "store_put_artifact" in {t["name"] for t in tools.json()["result"]["tools"]}
        # Stopped: the mount answers 503 rather than hanging.
        assert client.get("/health").status_code == 200

    def test_mount_guard_oauth(self, tmp_path):
        h = Harness(tmp_path)
        oauth = {"base_url": "https://memory.example", "client_id": "claude", "client_secret": "s3",
                 "auto_approve": True, "storage_path": None,
                 "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}
        app = create_app(None, token="secret", sessions=h.plane, bridge_oauth=oauth)
        with TestClient(app) as client:
            health = client.get("/health").json()
            assert health["sessions"]["mcp"]["oauth"] is True
            challenge = client.post("/mcp", json={})
            assert challenge.status_code == 401
            assert 'resource_metadata="https://memory.example/.well-known/oauth-protected-resource' in \
                challenge.headers["www-authenticate"]
            meta = client.get("/.well-known/oauth-authorization-server")
            assert meta.status_code == 200
            assert meta.json()["authorization_endpoint"] == "https://memory.example/authorize"
            resource = client.get("/.well-known/oauth-protected-resource")
            assert resource.status_code == 200
            # The daemon bearer still works alongside OAuth.
            ok = client.post("/mcp", headers={"Authorization": "Bearer secret",
                                              "Accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
            assert ok.status_code == 200
            # Health and the session routes are untouched by origo.
            assert client.get("/sessions/v1/stats", headers={"Authorization": "Bearer secret"}).status_code == 200


# ── review findings (PR #97) ─────────────────────────────────────────────────


class TestReviewFindings:
    def test_raw_hook_without_cwd_claims_nothing(self, tmp_path):
        """The daemon's own working directory is NOT the session's: a
        payload without cwd yields a session without a project, and a
        degraded (unscoped) answer — never `dir:<daemon workdir>`."""
        h = Harness(tmp_path)
        app = create_app(None, token="secret", sessions=h.plane)
        headers = {"Authorization": "Bearer secret"}
        with TestClient(app) as client:
            payload = {k: v for k, v in RAW_START.items() if k != "cwd"}
            answer = client.post("/sessions/v1/hooks/claude-code", json=payload, headers=headers)
            assert answer.status_code == 200 and answer.json() == {}
            session = h.plane.registry.get("claude-code:cloud-1")
            assert session.cwd is None and session.project is None
            # Malformed client facts: counted, empty 200, never a 500.
            odd = client.post("/sessions/v1/hooks/claude-code", headers=headers,
                              json={**RAW_START, "session_id": "odd", "transcript_path": 12345})
            assert (odd.status_code, odd.json()) == (200, {})
            long = client.post("/sessions/v1/hooks/claude-code", headers=headers,
                               json={**RAW_START, "session_id": "odd2", "cwd": "x" * 5000})
            assert (long.status_code, long.json()) == (200, {})
        assert h.plane.stats.events_rejected == 2

    async def test_second_life_gets_a_fresh_run_and_episodes(self, tmp_path):
        """A session that went stale (or was resumed under the same id)
        must not be tied to its closed run: new run, new artifacts, new
        memory episode keys."""
        h = Harness(tmp_path, alive={4242})
        await h.send("SessionStart", source="startup")
        await h.send("UserPromptSubmit", prompt="first life work", prompt_id="p-0")
        first_run = h.plane.registry.get("claude-code:s-1").run_id
        h.alive.clear()
        await h.plane.sweep(now=datetime.now(timezone.utc) + timedelta(seconds=10))
        await h.drain()
        assert h.fake_store.runs[first_run]["status"] == "cancelled"
        h.alive.add(4242)
        # The user comes back under the same session id.
        await h.send("SessionStart", source="resume")
        session = h.plane.registry.get("claude-code:s-1")
        assert session.lives == 1 and session.state == "active"
        assert session.run_id != first_run and session.run_status == "running"
        await h.send("PreCompact", trigger="auto")
        await h.send("SessionEnd", reason="exit")
        await h.drain()
        by_run = {}
        for artifact in h.fake_store.artifacts.values():
            by_run.setdefault(artifact["producer"]["run_id"], []).append(
                artifact["properties"]["occurrence"])
        assert by_run[first_run] == ["end"]
        assert by_run[session.run_id] == ["compact-1", "end"]
        assert h.fake_store.runs[session.run_id]["status"] == "succeeded"
        assert h.plane.stats.store_failures == 0
        episodes = [e for e in h.events("session_episode")]
        assert len(episodes) == 3  # first life end, second life compact + end

    async def test_run_closed_even_when_end_artifact_fails(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("UserPromptSubmit", prompt="work happened", prompt_id="p-1")
        session = h.plane.registry.get("claude-code:s-1")
        # Make the store refuse artifacts for this run while still accepting the close.
        original = h.fake_store._handle

        def refuse_artifacts(request):
            if request.method == "POST" and request.url.path == "/v0/artifacts":
                import httpx
                return httpx.Response(422, json={"detail": "nope", "code": "invalid"})
            return original(request)

        h.fake_store._handle = refuse_artifacts
        h.plane.store._transport = __import__("httpx").MockTransport(refuse_artifacts)
        await h.send("SessionEnd", reason="exit")
        await h.drain()
        assert h.fake_store.runs[session.run_id]["status"] == "succeeded"
        assert h.plane.stats.store_failures == 1

    def test_resolve_identity_default_and_paths(self, tmp_path):
        h = Harness(tmp_path)
        plane = h.plane
        assert plane.resolve_identity("mcp:default").id == "mcp:default"
        assert plane.resolve_identity("MCP:DEFAULT").slug == "mcp-default"
        assert plane.resolve_identity("path:/w/repo").id == "path:/w/repo"

    async def test_resolve_identity_by_cwd(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        assert h.plane.resolve_identity("/w/repo").id == "github.com/org/repo"
        assert h.plane.resolve_identity("/w/repo/").id == "github.com/org/repo"
        assert h.plane.resolve_identity("s-1").id == "github.com/org/repo"  # bare harness id


class TestMidLifeSessions:
    async def test_session_first_seen_on_a_capture_gets_a_run(self, tmp_path):
        """Hooks installed while a session was already running: the first
        event the daemon sees is a prompt, not a SessionStart."""
        h = Harness(tmp_path)
        await h.send("UserPromptSubmit", prompt="already running", prompt_id="p-1")
        await h.drain()
        session = h.plane.registry.get("claude-code:s-1")
        assert session.run_id in h.fake_store.runs
        await h.send("SessionEnd", reason="exit")
        await h.drain()
        assert [a["properties"]["occurrence"] for a in h.fake_store.artifacts.values()] == ["end"]


class TestRawHookShadowing:
    def test_raw_hooks_are_dropped_for_adapter_known_sessions(self, tmp_path):
        """Plugin adapter + repository HTTP hooks on one machine: the
        adapter's session wins, raw hooks for it answer empty."""
        h = Harness(tmp_path)
        app = create_app(None, token="secret", sessions=h.plane)
        headers = {"Authorization": "Bearer secret"}
        with TestClient(app) as client:
            adapter = client.post("/sessions/v1/events", headers=headers,
                                  json=envelope("SessionStart", source="startup").model_dump())
            assert "[memory guide" in adapter.json()["context"]
            raw = client.post("/sessions/v1/hooks/claude-code", headers=headers,
                              json={**RAW_START, "session_id": "s-1", "cwd": "/w/repo"})
            assert (raw.status_code, raw.json()) == (200, {})
            # The other way round, a raw-hook-only session still gets context.
            other = client.post("/sessions/v1/hooks/claude-code", headers=headers, json=RAW_START)
            assert "[memory guide" in other.json()["hookSpecificOutput"]["additionalContext"]
        assert h.plane.stats.raw_hooks_shadowed == 1
        assert h.plane.stats.events_received == 2


class TestLateOpen:
    async def test_first_prompt_opens_context_when_start_was_missed(self, tmp_path):
        """A SessionStart that never reached the daemon (cloud VMs fire it
        before their environment variables exist): the first prompt gets
        the opening context instead, exactly once."""
        h = Harness(tmp_path)
        first = await h.send("UserPromptSubmit", prompt="first thing", prompt_id="p-1")
        assert first.context is not None
        assert "[memory guide" in first.context and "[session context" in first.context
        session = h.plane.registry.get("claude-code:s-1")
        assert session.counters.injections == 1 and session.run_id in h.fake_store.runs
        assert h.plane.stats.late_opens == 1
        second = await h.send("UserPromptSubmit", prompt="second thing", prompt_id="p-2")
        assert second.context is None  # recall lane off; no re-injection
        assert h.plane.stats.late_opens == 1

    async def test_normal_start_is_not_a_late_open(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        prompt = await h.send("UserPromptSubmit", prompt="hello there", prompt_id="p-1")
        assert prompt.context is None and h.plane.stats.late_opens == 0

    def test_raw_first_prompt_gets_context_in_harness_shape(self, tmp_path):
        h = Harness(tmp_path)
        app = create_app(None, token="secret", sessions=h.plane)
        with TestClient(app) as client:
            answer = client.post("/sessions/v1/hooks/claude-code",
                                 headers={"Authorization": "Bearer secret"},
                                 json={**RAW_START, "hook_event_name": "UserPromptSubmit",
                                       "prompt": "hello from the vm", "prompt_id": "p-1"})
            out = answer.json()["hookSpecificOutput"]
            assert out["hookEventName"] == "UserPromptSubmit"
            assert "[memory guide" in out["additionalContext"]


class TestCaptureKeyAttributes:
    def test_same_message_different_attributes_are_distinct(self):
        from miragen_hook.normalize import event_idempotency_key

        base = {"session_id": "s", "hook_event_name": "Stop", "last_assistant_message": "Done."}
        a = normalize_hook_payload("claude-code", {**base, "stop_reason": "end_turn"})
        b = normalize_hook_payload("claude-code", {**base, "stop_reason": "max_tokens"})
        assert event_idempotency_key(a) != event_idempotency_key(b)
        assert event_idempotency_key(a) == event_idempotency_key(
            normalize_hook_payload("claude-code", {**base, "stop_reason": "end_turn"}))


class TestEmptySessionsAndWorkspaceRoots:
    async def test_empty_session_leaves_no_trail_and_cancels_its_run(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        session = h.plane.registry.get("claude-code:s-1")
        run_id = session.run_id
        await h.send("SessionEnd", reason="other")
        await h.drain()
        assert h.fake_store.artifacts == {}
        assert h.fake_store.runs[run_id]["status"] == "cancelled"
        assert h.events("session_episode") == []
        assert h.plane.stats.empty_sessions == 1 and h.plane.stats.episodes == 0
        assert h.context_state() is None or "last_session" not in (h.context_state() or {})

    async def test_session_with_a_prompt_is_not_empty(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup")
        await h.send("UserPromptSubmit", prompt="do the thing", prompt_id="p-1")
        await h.send("SessionEnd", reason="exit")
        await h.drain()
        assert len(h.fake_store.artifacts) == 1 and h.plane.stats.empty_sessions == 0

    async def test_directory_above_several_clones_is_a_workspace(self, tmp_path):
        h = Harness(tmp_path)
        # The cloud harness touches each clone first (remote known per repo)...
        for i, repo in enumerate(("repo", "other")):
            await h.send("SessionStart", source="startup", session=f"c-{i}", cwd=f"/home/user/{repo}",
                         host="vm", remote=True, project_remote=f"git@github.com:org/{repo}.git")
        # ...then the main session runs above them.
        result = await h.send("SessionStart", source="startup", session="main", cwd="/home/user",
                              host="vm", remote=True)
        main = h.plane.registry.get("claude-code:main")
        assert main.project.id == "workspace:user"
        assert main.scope == "group:project.workspace-user"
        assert "project=workspace:user" in result.context

    async def test_lone_remote_directory_is_still_dir(self, tmp_path):
        h = Harness(tmp_path)
        await h.send("SessionStart", source="startup", session="c-1", cwd="/home/user",
                     host="vm", remote=True)
        assert h.plane.registry.get("claude-code:c-1").project.id == "dir:user"
