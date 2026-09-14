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
from miragen.daemon.sessions.models import EventEnvelope
from miragen.daemon.sessions.plane import SessionPlane

SESSIONS_CAPABILITY = "sessions/v1"


class SessionNotFound(DaemonError):
    status = 404
    code = "session_not_found"


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
