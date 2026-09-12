"""
miragend's HTTP API — the machine-facing surface of the lifecycle plane.

Clients: miragen-mcp (MCP adapter for AI clients) and mirarun (control
plane). Auth is a single bearer token (MIRAGEND_TOKEN); like the agents'
own MIRAGEN_INTERNAL_TOKEN, an empty token means "rely on Docker network
isolation" and is logged loudly. GET /health is never guarded — it is the
capability advertisement peers use to discover what this daemon serves.

Errors are structured: DaemonError subclasses map to their HTTP status with
a JSON body {"detail": str, "code": str, ...extra}. No LLM-facing guidance
strings here — that's the MCP adapter's job.
"""

from __future__ import annotations

import hmac
import logging
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from miragen.daemon.contract import CONTRACT_CAPABILITIES, register_contract_routes
from miragen.daemon.core import (
    AGENT_NAME_PATTERN,
    DaemonError,
    LifecycleCore,
    validate_profile_text,
)
from miragen.daemon.schedules import ScheduleStore

logger = logging.getLogger(__name__)

# Contract capabilities this daemon serves, advertised on GET /health so
# clients (miragen-mcp, mirarun) can detect version skew instead of failing
# obscurely — the same pattern agents use for their executor contracts.
DAEMON_CAPABILITIES = (
    "lifecycle/v1",
    "registry/v1",
    "tools/v1",
    "files/v1",
    "transfer/v1",
    "schedules/v1",
    "validate/v1",
)


def _miragen_version() -> str:
    try:
        return version("miragen")
    except PackageNotFoundError:  # pragma: no cover - source checkout
        return "unknown"


class DaemonUnauthorized(DaemonError):
    status = 401
    code = "unauthorized"


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CreateAgentBody(BaseModel):
    name: str = Field(pattern=AGENT_NAME_PATTERN)
    yaml_source: str = Field(min_length=1)
    # Optional tools.py contents, written instead of the empty placeholder.
    # Creation is refused when the profile names tools or on_complete handlers
    # this source does not define, so a control plane can provision a complete
    # agent — profile plus tools — in one call, and cannot accidentally create
    # one that fails on every boot.
    tools_source: str | None = None
    # Recorded on the spawned unit, forwarded verbatim to whichever spawn
    # driver is active, never interpreted by miragen — see core.py's
    # _validate_labels docstring. The orchestrator calling this endpoint
    # assigns whatever meaning these carry; miragen just carries them.
    labels: dict[str, str] = Field(default_factory=dict)


class YamlBody(BaseModel):
    yaml_source: str = Field(min_length=1)


class RegisterToolBody(BaseModel):
    tool_name: str = Field(min_length=1)
    source: str = Field(min_length=1)


class EditBody(BaseModel):
    old_str: str = Field(min_length=1)
    new_str: str


class WriteFileBody(BaseModel):
    path: str = Field(min_length=1)
    content: str


class EditFileBody(BaseModel):
    path: str = Field(min_length=1)
    old_str: str = Field(min_length=1)
    new_str: str


class ImportBody(BaseModel):
    name: str = Field(pattern=AGENT_NAME_PATTERN)
    archive_path: str = Field(min_length=1)
    start: bool = True


class ScheduleBody(BaseModel):
    agent: str = Field(pattern=AGENT_NAME_PATTERN)
    prompt: str = Field(min_length=1)
    delay_seconds: int | None = Field(default=None, ge=1)
    at: str | None = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    core: LifecycleCore,
    schedules: ScheduleStore | None = None,
    *,
    token: str = "",
    internal_token: str = "",
    contract_transport=None,
) -> FastAPI:
    app = FastAPI(title="miragend", version=_miragen_version())

    async def require_token(request: Request) -> None:
        if not token:
            return
        # request.headers.get() returns a str that Starlette produced by
        # decoding the raw wire bytes as latin-1 (never raises, since
        # latin-1 maps every byte 0-255 to a codepoint). That decode is not
        # invertible via .encode("utf-8"): a client that sent a non-ASCII
        # token UTF-8-encoded on the wire ends up, after latin-1 decode,
        # with a str whose UTF-8 re-encoding does not reproduce the
        # original bytes — so no UTF-8 non-ASCII token could ever match,
        # even the correct one. Use request.headers.raw instead, which
        # exposes the untouched wire bytes directly (ASGI servers always
        # deliver header names lowercased), and compare those bytes as-is
        # against the configured token UTF-8-encoded (what a UTF-8-aware
        # client actually puts on the wire).
        supplied_bytes = b""
        for raw_name, raw_value in request.headers.raw:
            if raw_name == b"authorization":
                supplied_bytes = raw_value
                break
        expected_bytes = f"Bearer {token}".encode("utf-8")
        if not hmac.compare_digest(supplied_bytes, expected_bytes):
            raise DaemonUnauthorized("missing or invalid bearer token")

    guarded = [Depends(require_token)]

    @app.exception_handler(DaemonError)
    async def daemon_error_handler(_request: Request, exc: DaemonError) -> JSONResponse:
        body: dict = {"detail": str(exc), "code": exc.code}
        body.update(exc.extra)
        return JSONResponse(status_code=exc.status, content=body)

    # -- health (never guarded) ---------------------------------------------

    @app.get("/health")
    def health() -> dict:
        capabilities = list(DAEMON_CAPABILITIES)
        if internal_token:
            # The run-control contract is served only when the shared agent
            # token exists to authenticate it (and to forward downstream), so
            # it is advertised only then — a capability string is a promise.
            capabilities += list(CONTRACT_CAPABILITIES)
        return {
            "status": "ok",
            "service": "miragend",
            "version": _miragen_version(),
            "capabilities": capabilities,
        }

    # -- registry -----------------------------------------------------------

    @app.get("/agents", dependencies=guarded)
    def list_agents() -> dict:
        agents = core.list_agents()
        return {"count": len(agents), "agents": agents}

    @app.get("/agents/{name}", dependencies=guarded)
    def get_agent(name: str) -> dict:
        return core.get_agent(name)

    # -- lifecycle ----------------------------------------------------------

    @app.post("/agents", status_code=201, dependencies=guarded)
    def create_agent(body: CreateAgentBody) -> dict:
        core.create_agent(
            body.name, body.yaml_source, body.tools_source, labels=body.labels
        )
        return {"name": body.name, "status": core.container_status(body.name)}

    @app.put("/agents/{name}/config", dependencies=guarded)
    def update_agent_config(name: str, body: YamlBody) -> dict:
        return core.update_agent_config(name, body.yaml_source)

    @app.post("/agents/{name}/start", dependencies=guarded)
    def start_agent(name: str) -> dict:
        core.start_agent(name)
        return {"name": name, "status": core.container_status(name)}

    @app.post("/agents/{name}/stop", dependencies=guarded)
    def stop_agent(name: str) -> dict:
        core.stop_agent(name)
        return {"name": name, "status": core.container_status(name)}

    @app.post("/agents/{name}/restart", dependencies=guarded)
    def restart_agent(name: str) -> dict:
        core.restart_agent(name)
        return {"name": name, "status": core.container_status(name)}

    @app.delete("/agents/{name}", dependencies=guarded)
    def delete_agent(name: str) -> dict:
        core.delete_agent(name)
        return {"name": name, "deleted": True}

    @app.get("/agents/{name}/logs", dependencies=guarded)
    def agent_logs(
        name: str, tail: int = Query(default=50, ge=1, le=1000)
    ) -> dict:
        return {"name": name, "logs": core.agent_logs(name, tail=tail)}

    # -- tool management ----------------------------------------------------

    @app.get("/agents/{name}/tools", dependencies=guarded)
    def list_tools(name: str) -> dict:
        tools = core.list_tools(name)
        return {"count": len(tools), "tools": tools}

    @app.get("/agents/{name}/tools/{tool_name}", dependencies=guarded)
    def tool_source(name: str, tool_name: str) -> dict:
        return {"tool_name": tool_name, "source": core.tool_source(name, tool_name)}

    @app.post("/agents/{name}/tools", status_code=201, dependencies=guarded)
    def register_tool(name: str, body: RegisterToolBody) -> dict:
        core.register_tool(name, body.tool_name, body.source)
        return {"tool_name": body.tool_name, "registered": True}

    @app.patch("/agents/{name}/tools/{tool_name}", dependencies=guarded)
    def edit_tool(name: str, tool_name: str, body: EditBody) -> dict:
        core.edit_tool(name, tool_name, body.old_str, body.new_str)
        return {"tool_name": tool_name, "edited": True}

    @app.delete("/agents/{name}/tools/{tool_name}", dependencies=guarded)
    def delete_tool(name: str, tool_name: str) -> dict:
        core.delete_tool(name, tool_name)
        return {"tool_name": tool_name, "deleted": True}

    # -- workspace files ----------------------------------------------------

    @app.get("/agents/{name}/files", dependencies=guarded)
    def read_file(name: str, path: str = Query(min_length=1)) -> dict:
        return {"path": path, "content": core.read_file(name, path)}

    @app.put("/agents/{name}/files", dependencies=guarded)
    def write_file(name: str, body: WriteFileBody) -> dict:
        core.write_file(name, body.path, body.content)
        return {"path": body.path, "written": True}

    @app.patch("/agents/{name}/files", dependencies=guarded)
    def edit_file(name: str, body: EditFileBody) -> dict:
        core.edit_file(name, body.path, body.old_str, body.new_str)
        return {"path": body.path, "edited": True}

    # -- export / import ----------------------------------------------------

    @app.post("/agents/{name}/export", dependencies=guarded)
    def export_agent(name: str) -> dict:
        return core.export_agent(name)

    @app.post("/agents/import", status_code=201, dependencies=guarded)
    def import_agent(body: ImportBody) -> dict:
        core.import_agent(body.name, body.archive_path, start=body.start)
        return {"name": body.name, "imported": True, "started": body.start}

    # -- validation ---------------------------------------------------------

    @app.post("/validate", dependencies=guarded)
    def validate(body: YamlBody) -> dict:
        return {"valid": True, "profile": validate_profile_text(body.yaml_source)}

    # -- schedules ----------------------------------------------------------

    if schedules is not None:

        @app.post("/schedules", status_code=201, dependencies=guarded)
        def set_schedule(body: ScheduleBody) -> dict:
            core.check_name(body.agent)
            return schedules.set(
                body.agent,
                body.prompt,
                delay_seconds=body.delay_seconds,
                at=body.at,
            )

        @app.get("/schedules", dependencies=guarded)
        def list_schedules(agent: str | None = Query(default=None)) -> dict:
            if agent is not None:
                core.check_name(agent)
            jobs = schedules.list(agent)
            return {"count": len(jobs), "retriggers": jobs}

        @app.delete("/schedules/{job_id}", dependencies=guarded)
        def cancel_schedule(job_id: str) -> dict:
            schedules.cancel(job_id)
            return {"job_id": job_id, "cancelled": True}

    # -- run-control contract (X-Miragen-Token, not the bearer) -------------
    # Registered even without an internal token so the routes answer 503
    # "unconfigured" rather than 404 — a client can tell "daemon too old"
    # from "daemon missing its token". Capabilities advertise only when
    # configured (see health()).
    register_contract_routes(
        app, core, internal_token=internal_token, transport=contract_transport
    )

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover - exercised only in a real deployment
    import uvicorn

    try:
        import docker
    except ImportError as exc:
        raise SystemExit(
            "miragend requires the daemon extra: pip install miragen[daemon]"
        ) from exc

    from miragen.daemon.schedules import build_scheduler

    logging.basicConfig(level=logging.INFO)

    workspace = Path(os.getenv("MIRAGEN_WORKSPACE", "/opt/miragen"))
    token = os.getenv("MIRAGEND_TOKEN", "")
    if not token:
        logger.warning(
            "MIRAGEND_TOKEN is empty — the lifecycle API is unguarded and relies "
            "entirely on Docker network isolation. Set MIRAGEND_TOKEN before "
            "exposing miragend beyond miragen-net."
        )

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "agents").mkdir(parents=True, exist_ok=True)

    # Which substrate spawned agents run on — a miragend deployment-level
    # choice, orthogonal to which control plane (mirarun or otherwise) is
    # calling this daemon's API: every caller drives the same /agents
    # endpoints regardless. Image-contract validation still needs a Docker
    # client either way (see LifecycleCore.__init__), so docker.from_env()
    # runs unconditionally.
    substrate = os.getenv("MIRAGEN_SPAWN_SUBSTRATE", "docker-compose")
    spawn_driver = None
    if substrate == "kubernetes":
        from miragen.daemon.spawn.kubernetes import KubernetesSpawnDriver

        spawn_driver = KubernetesSpawnDriver.from_incluster_config()
    elif substrate != "docker-compose":
        raise SystemExit(
            f"MIRAGEN_SPAWN_SUBSTRATE={substrate!r} is not recognized "
            "(expected 'docker-compose' or 'kubernetes')"
        )

    core = LifecycleCore(
        workspace,
        docker.from_env(),
        base_image=os.getenv("MIRAGEN_BASE_IMAGE", "ghcr.io/ieepirzy/miragen:latest"),
        internal_token=os.getenv("MIRAGEN_INTERNAL_TOKEN", ""),
        spawn_driver=spawn_driver,
    )
    core.ensure_network()

    import miragen.daemon.schedules as schedules_module

    schedules_module.set_endpoint_resolver(core.endpoint)

    schedules = ScheduleStore(build_scheduler(workspace / "retriggers.sqlite"))
    app = create_app(
        core,
        schedules,
        token=token,
        internal_token=os.getenv("MIRAGEN_INTERNAL_TOKEN", ""),
    )

    @app.router.on_event("startup")
    async def _start_scheduler() -> None:
        schedules.start()

    @app.router.on_event("shutdown")
    async def _stop_scheduler() -> None:
        schedules.shutdown()

    uvicorn.run(
        app,
        host=os.getenv("MIRAGEND_HOST", "0.0.0.0"),
        port=int(os.getenv("MIRAGEND_PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
