"""The bridge's MCP surface: memory_* and store_* tools for external
sessions (§18.8), served by miragend at /mcp.

A MiraGen agent gets these tools inside its own container (/mcp/memory,
bound to a run). An external session — Claude Code on this machine, a
cloud VM, a claude.ai chat — has no container, so the daemon serves one
shared MCP server and every call names what it acts on: a `project`
(remote URL, slug, repository name or session key; None = the configured
default project) for memory, a `run_id` or `session` for artifacts. The
daemon resolves those into scopes and runs exactly as it does for hook
events — a tool argument can name a project, never a scope, a principal
or a credential.

Two credential classes open the mount (api.py): the daemon bearer
(automation, `claude mcp add --header`) and an origo OAuth token
(claude.ai custom connector). Results are JSON strings; a write counts
only when its status says accepted/captured.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from miragen.daemon.sessions.store import StoreAPIError, StoreUnavailable

logger = logging.getLogger("miragend.bridge_mcp")

INSTRUCTIONS = (
    "miragen-bridge: memory and artifact tools for external agent sessions. "
    "Memory tools take `project` (the repository remote or name, or a session "
    "key from the injected session header); omit it for the default project. "
    "store_put_artifact needs a run: pass this session's `run_id` (from the "
    "session header's store_run=…) or `session`, or open one with "
    "store_open_run. Provenance is one hop: list only DIRECT sources. A write "
    "is saved only when the result says accepted/captured; pending or "
    "persistence_unavailable is not saved."
)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def build_bridge_mcp(get_plane: Callable[[], Any]) -> FastMCP:
    """`get_plane` is a zero-arg callable returning the SessionPlane (read
    per call so the mounted server sees what the lifespan wired)."""
    mcp = FastMCP(
        "miragen-bridge",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        # Behind NPM the Host header is the public name; the mount is
        # guarded by bearer/OAuth instead (see api.py).
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def _lifecycle(project: str | None):
        plane = get_plane()
        identity = plane.resolve_identity(project)
        lifecycle, write, detail = await plane.lifecycle_for_project(identity)
        return plane, identity, lifecycle, write, detail

    def _store(plane):
        if plane.store is None:
            raise RuntimeError("the artifact store is disabled on this daemon (store.enabled: false)")
        if not plane.store.configured():
            raise RuntimeError("the artifact store is not configured (no endpoint/credential)")
        return plane.store

    async def _store_call(plane, coro):
        try:
            result = await coro
        except StoreAPIError as exc:
            plane.stats.note_store(True)
            return {"status": "rejected", "detail": str(exc), "http_status": exc.status_code}
        except StoreUnavailable as exc:
            plane.stats.note_store(False, str(exc))
            return {"status": "persistence_unavailable", "detail": str(exc)}
        plane.stats.note_store(True)
        return result

    # ── orientation ──────────────────────────────────────────────────────────

    @mcp.tool()
    async def bridge_status() -> str:
        """What this bridge is connected to: principal, Loimi/store health,
        known projects and how to name one in the other tools."""
        plane = get_plane()
        described = plane.describe()
        projects = sorted({
            p.id for p in list(plane._projects.values()) + list(plane._known_by_name.values())
        })
        return _dump({
            "principal": described["principal"], "loimi": described["loimi"],
            "store": described["store"], "known_projects": projects,
            "default_project": plane.config.mcp.default_project,
            "naming": "project = remote URL ('github.com/org/repo'), slug, repository name, "
                      "or a session key ('claude-code:<session_id>')",
        })

    @mcp.tool()
    async def bridge_sessions(active: bool = True, limit: int = 20) -> str:
        """External sessions the daemon knows (key, harness, project, store
        run, namespace, first prompt) — to find your own run or a sibling
        session's project.

        Args:
            active: Only sessions still active.
            limit: Newest N.
        """
        plane = get_plane()
        rows = []
        for session in plane.registry.list(active_only=active)[:max(1, min(limit, 200))]:
            rows.append({
                "session": session.key, "harness": session.harness, "state": session.state,
                "project": session.project.id if session.project else None,
                "scope": session.scope, "run_id": session.run_id, "namespace": session.namespace,
                "remote": session.remote, "host": session.host,
                "first_prompt": session.prompts[0] if session.prompts else None,
                "last_seen_at": session.last_seen_at,
            })
        return _dump({"count": len(rows), "sessions": rows})

    # ── memory ────────────────────────────────────────────────────────────────

    @mcp.tool()
    async def memory_recall(
        query: str, project: str | None = None, limit: int = 8, types: list[str] | None = None,
    ) -> str:
        """Search memory readable from a project (its own scope plus the
        shared read layers): lexical recall over accepted records. An empty
        result does not mean nothing is stored — it means nothing matched.

        Args:
            query: Free text to match (Finnish or English).
            project: Repository remote/name or session key; omit for the default project.
            limit: Max records (1-50).
            types: Restrict to record types: observation, claim, procedure, intention.
        """
        plane, identity, lifecycle, write, detail = await _lifecycle(project)
        body: dict[str, Any] = {
            "scope_ids": list(lifecycle.spec.scopes.read), "query_text": query,
            "limit": max(1, min(int(limit), 50)),
        }
        if types:
            body["types"] = types
        try:
            found = await lifecycle.client.search_memory(body)
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the model
            return _dump({"status": "persistence_unavailable", "detail": str(exc),
                          "project": identity.id, "scopes": body["scope_ids"]})
        return _dump({"status": "ok", "project": identity.id, "scopes": body["scope_ids"],
                      "scope_detail": detail, **found})

    @mcp.tool()
    async def memory_read(record_id: str, project: str | None = None) -> str:
        """Read one memory record by id (current revision; history stays queryable).

        Args:
            record_id: The record id.
            project: Which project's principal view to read through (usually irrelevant).
        """
        _, _, lifecycle, _, _ = await _lifecycle(project)
        return _dump(await lifecycle.read(record_id))

    @mcp.tool()
    async def memory_remember(content: str, project: str | None = None, session: str | None = None) -> str:
        """Propose one focused, self-contained durable observation into a
        project's memory scope. Rooted in its own source event (the agent
        said this, then). Saved only when status is accepted.

        Args:
            content: The observation — specific, self-contained, one thing.
            project: Repository remote/name or session key; omit for the default project.
            session: Session key to attribute the note to (its run ref).
        """
        plane, identity, lifecycle, write, detail = await _lifecycle(session or project)
        result = await lifecycle.remember(
            instance=identity.slug, run_id=session, content=content,
        )
        return _dump({**result, "project": identity.id, "scope": write, "scope_detail": detail})

    @mcp.tool()
    async def memory_correct(
        record_id: str, correction: dict, reason: str = "", project: str | None = None,
    ) -> str:
        """Correct an erroneous stored memory record with evidence; the
        correction is its own event and history stays queryable.

        Args:
            record_id: The record to correct.
            correction: Corrected payload, e.g. {"text": "..."}.
            reason: Why — quote the user's correction when relaying one.
            project: Repository remote/name or session key.
        """
        _, identity, lifecycle, _, _ = await _lifecycle(project)
        return _dump(await lifecycle.correct(
            instance=identity.slug, run_id=None, record_id=record_id,
            corrected_payload=correction, reason=reason,
        ))

    @mcp.tool()
    async def memory_checkpoint(state: dict, project: str | None = None) -> str:
        """Persist durable working state for a project (shallow-merged; a
        null value removes a key). Use before finishing or when the goal,
        constraints or pending actions materially change.

        Args:
            state: Fields to merge, e.g. {"goal": ..., "pending_actions": [...]}.
            project: Repository remote/name or session key; omit for the default project.
        """
        _, identity, lifecycle, write, _ = await _lifecycle(project)
        result = await lifecycle.checkpoint(instance=identity.slug, patch=state)
        return _dump({**result, "project": identity.id, "scope": write})

    # ── artifact store ────────────────────────────────────────────────────────

    def _resolve_run(plane, run_id: str | None, session: str | None) -> str | None:
        if run_id:
            return run_id
        found = plane.find_session(session)
        return found.run_id if found is not None else None

    @mcp.tool()
    async def store_open_run(
        task: str, namespace: str | None = None, project: str | None = None,
        parent_run_id: str | None = None,
    ) -> str:
        """Open a Loimi run — the unit of provenance — for work that is not
        already covered by this session's run. Returns the Run; pass its id
        as run_id to store_put_artifact and store_close_run.

        Args:
            task: What the run is for.
            namespace: Loimi namespace; omit to derive it from the project (or the default).
            project: Used only to derive the namespace when it is omitted.
            parent_run_id: The run this work was delegated from, if any.
        """
        plane = get_plane()
        store = _store(plane)
        if namespace is None:
            identity = plane.resolve_identity(project) if project else None
            namespace = store.namespace_for(identity)
        return _dump(await _store_call(plane, store.open_run(
            task=task, namespace=namespace, parent_run_id=parent_run_id,
        )))

    @mcp.tool()
    async def store_close_run(run_id: str, status: str = "succeeded") -> str:
        """Close a run you opened with store_open_run: succeeded, failed or
        cancelled. Session runs are closed by the daemon at session end.

        Args:
            run_id: The run.
            status: succeeded | failed | cancelled.
        """
        plane = get_plane()
        return _dump(await _store_call(plane, _store(plane).close_run(run_id, status)))

    @mcp.tool()
    async def store_put_artifact(
        kind: str,
        content: str | None = None,
        run_id: str | None = None,
        session: str | None = None,
        content_ref: str | None = None,
        properties: dict | None = None,
        sources: list[str] | None = None,
        suggested_namespaces: list[str] | None = None,
        as_of: str | None = None,
        skip_provenance: bool = False,
    ) -> str:
        """Write an immutable artifact to Loimi under a run. Pass `run_id`, or
        `session` (this session's key from the injected header) to use its
        run. Provenance inheritance is exactly ONE HOP from direct sources —
        list in `sources` only the artifact ids this was DIRECTLY derived
        from, never everything you read. The server derives the content
        hash, derived_from edges and confirmed provenance tags.

        Args:
            kind: e.g. review, note, decision, report, code_diff, markdown.
            content: Inline text (or give content_ref).
            run_id: An open run's id.
            session: Session key whose run to use when run_id is omitted.
            content_ref: URI to a large/binary payload instead of content.
            properties: Kind-specific bag, e.g. {"title": "..."}.
            sources: DIRECT source artifact ids.
            suggested_namespaces: Extra namespaces you believe relevant (recorded as suggestions).
            as_of: RFC3339 — when the claim became true, if that differs from now.
            skip_provenance: Waive the provenance floor (rare; leave false).
        """
        plane = get_plane()
        store = _store(plane)
        resolved = _resolve_run(plane, run_id, session)
        if not resolved:
            return _dump({"status": "rejected", "detail": (
                "no run: pass run_id, or session=<session key> for a session with a store run, "
                "or open one with store_open_run")})
        result = await _store_call(plane, store.put_artifact(
            run_id=resolved, kind=kind, content=content, content_ref=content_ref,
            properties=properties, sources=sources, suggested_namespaces=suggested_namespaces,
            as_of=as_of, skip_provenance=skip_provenance,
        ))
        if isinstance(result, dict) and result.get("id"):
            plane.stats.artifacts_written += 1
            found = plane.find_session(session) if session else None
            if found is not None:
                found.artifacts_written.append(f"tool:{result['id']}")
            return _dump({"status": "accepted", "artifact": result})
        return _dump(result)

    @mcp.tool()
    async def store_get_artifact(artifact_id: str) -> str:
        """Fetch one artifact with its one-hop edges.

        Args:
            artifact_id: The artifact id.
        """
        plane = get_plane()
        return _dump(await _store_call(plane, _store(plane).get_artifact(artifact_id)))

    @mcp.tool()
    async def store_lineage(artifact_id: str, direction: str = "up", max_depth: int = 10) -> str:
        """Full-depth lineage tree of an artifact: `up` = what it derives
        from, `down` = what derives from it.

        Args:
            artifact_id: The artifact id.
            direction: up | down.
            max_depth: Traversal depth (1-50).
        """
        plane = get_plane()
        return _dump(await _store_call(plane, _store(plane).lineage(
            artifact_id, direction=direction, max_depth=max(1, min(int(max_depth), 50)),
        )))

    @mcp.tool()
    async def store_search(
        q: str | None = None, namespaces: list[str] | None = None,
        kinds: list[str] | None = None, limit: int = 20,
    ) -> str:
        """Search artifacts (full text over content and properties; confirmed
        namespace tags outrank suggested). Compact summaries — fetch full
        artifacts by id with store_get_artifact.

        Args:
            q: Free text.
            namespaces: Restrict to these namespaces.
            kinds: Restrict to these kinds.
            limit: Max items (1-200).
        """
        plane = get_plane()
        return _dump(await _store_call(plane, _store(plane).search(
            q=q, namespaces=namespaces, kinds=kinds, limit=max(1, min(int(limit), 200)),
        )))

    @mcp.tool()
    async def store_list_namespaces() -> str:
        """The registered Loimi namespaces (runs and artifacts must name one)."""
        plane = get_plane()
        return _dump(await _store_call(plane, _store(plane).list_namespaces()))

    @mcp.tool()
    async def store_run_tree(run_id: str) -> str:
        """A run's delegation tree with artifact summaries — a session's
        run shows everything it produced.

        Args:
            run_id: The run.
        """
        plane = get_plane()
        return _dump(await _store_call(plane, _store(plane).run_tree(run_id)))

    return mcp
