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
from miragen.daemon.sessions.plane import SessionPlane
from miragen.daemon.sessions.routes import SESSIONS_CAPABILITY, register_session_routes

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
    core: LifecycleCore | None,
    schedules: ScheduleStore | None = None,
    *,
    token: str = "",
    internal_token: str = "",
    contract_transport=None,
    sessions: SessionPlane | None = None,
    bridge_oauth: dict | None = None,
    harness_setup=None,
) -> FastAPI:
    """`core` is the Docker-bound lifecycle plane; `sessions` the external
    session plane (docs/design/external-sessions.md). Either may be absent:
    a developer-machine daemon runs sessions without Docker, a swarm host
    may run the lifecycle plane alone. /health advertises what is served.

    `bridge_oauth` (optional, with `sessions`): origo settings for the
    bridge MCP mount — {"base_url", "client_id", "client_secret",
    "auto_approve", "redirect_uris", "storage_path"}; None = bearer only.

    `harness_setup` (optional): a HarnessSetupService — the daemon keeps
    Grok Build / Codex on this machine joined (miragen/daemon/harness_setup.py);
    its status is on /health."""
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
        capabilities = list(DAEMON_CAPABILITIES) if core is not None else []
        if core is not None and internal_token:
            # The run-control contract is served only when the shared agent
            # token exists to authenticate it (and to forward downstream), so
            # it is advertised only then — a capability string is a promise.
            capabilities += list(CONTRACT_CAPABILITIES)
        if sessions is not None:
            capabilities.append(SESSIONS_CAPABILITY)
            if sessions.config.mcp.enabled:
                capabilities.append(BRIDGE_MCP_CAPABILITY)
        body = {
            "status": "ok",
            "service": "miragend",
            "version": _miragen_version(),
            "capabilities": capabilities,
        }
        if sessions is not None:
            body["sessions"] = sessions.describe()
            body["sessions"]["mcp"]["oauth"] = bridge_oauth is not None
        if harness_setup is not None:
            body["harness_setup"] = harness_setup.describe()
        return body

    if harness_setup is not None:
        app.router.on_startup.append(harness_setup.start)
        app.router.on_shutdown.append(harness_setup.stop)

    if sessions is not None:
        register_session_routes(app, sessions, dependencies=guarded)

        # Registered on the router's handler lists directly (what
        # on_event does minus its deprecation warning) so main() can keep
        # adding the scheduler's handlers the same way.
        app.router.on_startup.append(sessions.start)
        app.router.on_shutdown.append(sessions.stop)

        if sessions.config.mcp.enabled:
            mount_bridge_mcp(app, sessions, token=token, oauth=bridge_oauth)

    if core is None:
        return app

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
# Bridge MCP mount (/mcp): bearer OR origo OAuth
# ---------------------------------------------------------------------------

BRIDGE_MCP_CAPABILITY = "bridge-mcp/v1"


class _BridgeMcpGuard:
    """ASGI guard for the mounted FastMCP app. Route dependencies don't
    reach a mounted sub-app, so the two credential classes are checked
    here: the daemon bearer (automation, `claude mcp add --header`) and,
    when configured, an origo access token (claude.ai custom connector).
    An empty daemon token keeps the existing "network isolation" contract
    — the mount is then open unless origo is configured, in which case
    origo is the only gate."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.provider = None
        self.inner = _bridge_not_ready

    def _authorized(self, auth_header: bytes) -> bool:
        if self.token and hmac.compare_digest(auth_header, f"Bearer {self.token}".encode()):
            return True
        if self.provider is not None:
            value = auth_header.decode("latin-1", "replace")
            scheme, _, token = value.partition(" ")
            if scheme.lower() == "bearer" and token and self.provider.verify_token(
                token, resource=self.provider.resource_identifier
            ) is not None:
                return True
            return False
        return not self.token  # unguarded daemon, no origo: network isolation

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            supplied = b""
            for raw_name, raw_value in scope.get("headers") or []:
                if raw_name == b"authorization":
                    supplied = raw_value
                    break
            if not self._authorized(supplied):
                headers = [(b"content-type", b"application/json")]
                if self.provider is not None:
                    # RFC 9728 challenge: an OAuth-capable client discovers
                    # the authorization server from resource_metadata.
                    headers.append((b"www-authenticate", (
                        f'Bearer realm="{self.provider.base_url}", '
                        f'resource_metadata="{self.provider.protected_resource_metadata_url}"'
                    ).encode()))
                await send({"type": "http.response.start", "status": 401, "headers": headers})
                await send({"type": "http.response.body",
                            "body": b'{"detail": "missing or invalid bearer token", "code": "unauthorized"}'})
                return
        await self.inner(scope, receive, send)


async def _bridge_not_ready(scope, receive, send):
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": b'{"detail": "bridge MCP not started", "code": "unavailable"}'})


def mount_bridge_mcp(app: FastAPI, sessions: SessionPlane, *, token: str, oauth: dict | None) -> None:
    from contextlib import AsyncExitStack

    from miragen.daemon.sessions.bridge_mcp import build_bridge_mcp

    guard = _BridgeMcpGuard(token)
    mcp = build_bridge_mcp(lambda: sessions)
    app.state.bridge_mcp = mcp

    if oauth is not None:
        from origo import OAuthProvider

        provider = OAuthProvider(
            base_url=oauth["base_url"],
            clients={oauth["client_id"]: oauth["client_secret"]},
            client_redirect_uris={oauth["client_id"]: list(oauth["redirect_uris"])},
            auto_approve=bool(oauth.get("auto_approve", False)),
            mcp_path="/mcp",
            storage_path=oauth.get("storage_path"),
        )
        guard.provider = provider
        # Adopt origo's flow routes AND its state (its endpoints read
        # request.app.state; the set grows between releases — README:
        # never hand-copy a subset).
        oauth_app = provider.asgi_app()
        for route in reversed(oauth_app.routes):
            app.router.routes.insert(0, route)
        for key, value in vars(oauth_app.state)["_state"].items():
            setattr(app.state, key, value)
        logger.info(f"bridge MCP: origo OAuth enabled for client {oauth['client_id']!r}")

    @app.middleware("http")
    async def _mcp_mount_slash(request: Request, call_next):
        # claude.ai's connector client POSTs to /mcp exactly and does not
        # follow the mount's 307 to /mcp/ (Loimi, observed live 2026-08-12).
        if request.scope["path"] == "/mcp":
            request.scope["path"] = "/mcp/"
        return await call_next(request)

    app.mount("/mcp", guard)

    stack = AsyncExitStack()

    async def _start() -> None:
        guard.inner = mcp.streamable_http_app()
        await stack.enter_async_context(mcp.session_manager.run())

    async def _stop() -> None:
        guard.inner = _bridge_not_ready
        await stack.aclose()

    app.router.on_startup.append(_start)
    app.router.on_shutdown.append(_stop)


def bridge_oauth_from_env(state_dir: Path) -> dict | None:  # pragma: no cover - deployment wiring
    """MCP_BASE_URL + MCP_CLIENT_ID + MCP_CLIENT_SECRET, all three or none
    (the miradeploy/mirarun/Loimi convention). Token storage persists in
    the state dir so a restart does not log every connector out."""
    base_url = os.getenv("MCP_BASE_URL")
    client_id = os.getenv("MCP_CLIENT_ID")
    client_secret = os.getenv("MCP_CLIENT_SECRET")
    if not (base_url and client_id and client_secret):
        if any((base_url, client_id, client_secret)):
            logger.warning("bridge MCP: MCP_BASE_URL/MCP_CLIENT_ID/MCP_CLIENT_SECRET are all "
                           "required together; OAuth disabled, bearer only")
        return None
    extras = [u.strip() for u in os.getenv("MCP_CLIENT_REDIRECT_URIS", "").split(",") if u.strip()]
    redirect_uris = list(BRIDGE_DEFAULT_REDIRECT_URIS) + [
        u for u in extras if u not in BRIDGE_DEFAULT_REDIRECT_URIS
    ]
    return {
        "base_url": base_url, "client_id": client_id, "client_secret": client_secret,
        "auto_approve": os.getenv("MCP_AUTO_APPROVE", "false").lower() == "true",
        "redirect_uris": redirect_uris,
        "storage_path": str(state_dir / "oauth-state.sqlite"),
    }


BRIDGE_DEFAULT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_session_plane(config_path: str):  # pragma: no cover - deployment wiring
    """The external-session plane from MIRAGEND_SESSIONS_CONFIG: secrets
    via the *_FILE loader, the recall selector only when a model is
    configured (pydantic-ai is imported lazily for it), OTLP telemetry
    under the daemon's own service name when MIRAGEN_OTLP_ENDPOINT is set."""
    from miragen.daemon.sessions.config import load_sessions_config

    config = load_sessions_config(config_path)
    selector = None
    if config.recall.enabled and config.recall.model:
        from miragen.memory.selection import build_model_selector

        selector = build_model_selector(config.recall.model)
    telemetry = None
    otlp_endpoint = os.getenv("MIRAGEN_OTLP_ENDPOINT")
    if otlp_endpoint:
        from miragen.telemetry import MiragenTelemetry

        telemetry = MiragenTelemetry(
            endpoint=otlp_endpoint, agent_name="miragend", agent_mode="daemon",
            service_name="miragend", service_version=_miragen_version(),
            deployment_environment=os.getenv("MIRAGEN_DEPLOYMENT_ENV"),
            token=os.getenv("MIRAGEN_OTLP_TOKEN"), auth_header=os.getenv("MIRAGEN_OTLP_AUTH"),
        )
    plane = SessionPlane(config, selector=selector, telemetry=telemetry)
    logger.info(
        f"session plane enabled: principal={config.principal} state={plane.state_dir} "
        f"recall={'on' if selector else 'off'} provision={config.scopes.provision}"
    )
    return plane


def _build_harness_setup(sessions: SessionPlane | None):
    """The harness setup service from sessions.yaml's `harness_setup` block
    (+ MIRAGEND_HARNESS_SETUP_* env). A daemon without a session plane has
    no bridge to point harnesses at, and none is built."""
    if sessions is None:
        return None
    from miragen.daemon.harness_setup import HarnessSetupService, resolve_harness_setup

    return HarnessSetupService(resolve_harness_setup(sessions.config.harness_setup))


def main() -> None:  # pragma: no cover - exercised only in a real deployment
    import uvicorn

    logging.basicConfig(level=logging.INFO)

    # *_FILE secrets FIRST — before anything reads MIRAGEND_TOKEN or the
    # Loimi credentials. (Found live: a MIRAGEND_TOKEN_FILE resolved after
    # the token was read left the API unguarded while logging nothing
    # worse than the usual empty-token warning.)
    from miragen.daemon.sessions.config import load_file_secrets

    load_file_secrets()

    token = os.getenv("MIRAGEND_TOKEN", "")
    if not token:
        logger.warning(
            "MIRAGEND_TOKEN is empty — the API is unguarded and relies entirely "
            "on network isolation (Docker network, or a loopback bind). Set "
            "MIRAGEND_TOKEN before exposing miragend beyond that."
        )

    sessions_config = os.getenv("MIRAGEND_SESSIONS_CONFIG")
    sessions = _build_session_plane(sessions_config) if sessions_config else None
    harness_setup = _build_harness_setup(sessions)

    lifecycle_enabled = os.getenv("MIRAGEND_LIFECYCLE", "on").lower() not in (
        "off", "0", "false", "no",
    )
    if not lifecycle_enabled:
        if sessions is None:
            raise SystemExit(
                "MIRAGEND_LIFECYCLE=off and no MIRAGEND_SESSIONS_CONFIG: nothing to serve"
            )
        app = create_app(
            None, token=token, sessions=sessions,
            bridge_oauth=bridge_oauth_from_env(sessions.state_dir) if sessions.config.mcp.enabled else None,
            harness_setup=harness_setup,
        )
        uvicorn.run(
            app,
            host=os.getenv("MIRAGEND_HOST", "127.0.0.1"),
            port=int(os.getenv("MIRAGEND_PORT", "8420")),
        )
        return

    try:
        import docker
    except ImportError as exc:
        raise SystemExit(
            "miragend requires the daemon extra: pip install miragen[daemon] "
            "(or set MIRAGEND_LIFECYCLE=off to run the session plane alone)"
        ) from exc

    from miragen.daemon.schedules import build_scheduler

    workspace = Path(os.getenv("MIRAGEN_WORKSPACE", "/opt/miragen"))

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
        sessions=sessions,
        bridge_oauth=(
            bridge_oauth_from_env(sessions.state_dir)
            if sessions is not None and sessions.config.mcp.enabled else None
        ),
        harness_setup=harness_setup,
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
