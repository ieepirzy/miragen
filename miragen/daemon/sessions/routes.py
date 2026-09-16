"""HTTP surface of the session plane (`sessions/v1`).

Errors follow the daemon's structured shape ({"detail", "code"}). A
malformed envelope is a 422 that is COUNTED (stats.events_rejected) —
an adapter version drift must be visible on /health, not silent.
"""

from __future__ import annotations

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from miragen.daemon.core import DaemonError
from miragen.daemon.sessions.models import EventEnvelope, session_key
from miragen.daemon.sessions.plane import SessionPlane
from miragen_hook.client import build_envelope
from miragen_hook.normalize import (
    CONTEXT_BEARING,
    HARNESSES,
    harness_output,
    normalize_hook_payload,
)

SESSIONS_CAPABILITY = "sessions/v1"


class SessionNotFound(DaemonError):
    status = 404
    code = "session_not_found"


class UnknownHarness(DaemonError):
    status = 404
    code = "unknown_harness"


def register_session_routes(app: FastAPI, plane: SessionPlane, *, dependencies: list) -> None:
    @app.post("/sessions/v1/events", dependencies=dependencies)
    async def post_event(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            envelope = EventEnvelope.model_validate(body)
        except (ValueError, ValidationError) as exc:
            plane.stats.events_rejected += 1
            return JSONResponse(
                status_code=422,
                content={"detail": str(exc)[:1000], "code": "malformed_event"},
            )
        result = await plane.handle(envelope)
        return JSONResponse({
            "session": result.key,
            "state": result.state,
            "context": result.context,
            "detail": result.detail,
            "accepted": True,
        })

    @app.post("/sessions/v1/hooks/{harness}", dependencies=dependencies)
    async def post_raw_hook(harness: str, request: Request) -> JSONResponse:
        """A harness's RAW hook payload (Claude Code `type: http` hooks):
        the daemon does what the stdlib adapter would have done on the
        client — normalize, envelope, handle — and answers in the harness's
        own output shape. Nothing about the client host is claimed (no pid,
        user or hostname; the session is remote by construction), and the
        project is identified by the reported working directory's name
        unless the payload's cwd is visible here. An unmapped event is an
        empty 200: the harness must never see an error for a hook."""
        if harness not in HARNESSES:
            raise UnknownHarness(f"unknown harness '{harness}'", harness=harness)
        try:
            payload = await request.json()
        except ValueError:
            plane.stats.events_rejected += 1
            return JSONResponse(status_code=422,
                                content={"detail": "hook payload is not JSON", "code": "malformed_event"})
        if not isinstance(payload, dict):
            plane.stats.events_rejected += 1
            return JSONResponse(status_code=422,
                                content={"detail": "hook payload must be an object", "code": "malformed_event"})
        event = normalize_hook_payload(harness, payload)
        if event is None or event.session_id is None:
            return JSONResponse({})
        # A machine that runs BOTH the adapter (plugin) and a repository's
        # HTTP hooks reports every event twice. The adapter's envelope is
        # the richer one (pid, host, remote flag, project remote), so once
        # a session is known through an adapter, its raw hooks are
        # acknowledged and dropped — otherwise the context would be
        # injected twice at every start.
        known = plane.registry.get(session_key(harness, event.session_id))
        if known is not None and known.adapter and known.adapter != "http-hook":
            plane.stats.raw_hooks_shadowed += 1
            return JSONResponse({})
        try:
            envelope = EventEnvelope.model_validate(build_envelope(
                harness, payload, event, environ={}, pid=None, host=None, user=None,
                remote=True, project_remote_url=None, cwd=payload.get("cwd") or None,
            ))
        except ValidationError:
            # Counted (adapter/harness drift must show on /health) but an
            # empty 200 — a hook must never surface an error to the user.
            plane.stats.events_rejected += 1
            return JSONResponse({})
        envelope.client.adapter = "http-hook"
        result = await plane.handle(envelope)
        if result.context and event.name in CONTEXT_BEARING:
            return JSONResponse(harness_output(harness, event.original_event, result.context))
        return JSONResponse({})

    @app.get("/sessions/v1/sessions", dependencies=dependencies)
    def list_sessions(active: bool = Query(default=False)) -> dict:
        sessions = plane.registry.list(active_only=active)
        return {"count": len(sessions), "sessions": [s.summary() for s in sessions]}

    @app.get("/sessions/v1/sessions/{key:path}", dependencies=dependencies)
    def get_session(key: str) -> dict:
        session = plane.registry.get(key)
        if session is None:
            raise SessionNotFound(f"unknown session '{key}'", key=key)
        return session.model_dump()

    @app.get("/sessions/v1/stats", dependencies=dependencies)
    def stats() -> dict:
        return plane.describe()
