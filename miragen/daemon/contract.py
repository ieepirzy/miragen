"""Run-control contract on the daemon: one address for a control plane.

A control plane pointed at miragend used to fail with every run-control
capability missing at once — the daemon and the agent apps serve disjoint
contracts, and the operator was expected to know which address was which.
Rather than treating that as operator error forever, the daemon now serves
the contract itself: `/profiles/resolve` natively (resolve_edf is a pure
function in this same package), `/executor-runs` routed to the agent the
EDF's `metadata.name` selects, and run-scoped reads proxied to the agent
that owns the run. One control plane, one address, N agents.

Auth is the CONTRACT's credential, not the daemon's: these routes check
`X-Miragen-Token` against `MIRAGEN_INTERNAL_TOKEN` — the same shared token
this daemon injects into every agent it creates and forwards downstream
here — so a client (mirarun) uses one credential for the run-control
contract wherever it is served. The daemon's own lifecycle surface keeps
its separate `MIRAGEND_TOKEN` bearer guard. With no internal token
configured the contract routes fail closed and the capabilities are not
advertised: a capability string is a promise, not a hope.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from miragen.edf import EDFValidationError, ResolutionContext, resolve_edf
from miragen.models import AgentProfile

logger = logging.getLogger(__name__)

# Advertised on /health only when MIRAGEN_INTERNAL_TOKEN is configured.
CONTRACT_CAPABILITIES = (
    "edf-resolve/mirarun.io-v1alpha1",
    "executor-launch/v1",
    "events-cursor/v1",
    "run-snapshot/v1",
    "reviewed-publication/v1",
)

# Run-scoped subpaths forwarded verbatim to the owning agent. Anything not
# listed 404s here rather than reaching an agent with a route this daemon
# never promised to serve.
_PROXY_GET = frozenset({"", "events", "diff", "snapshot"})
_PROXY_POST = frozenset({"publications", "resume", "abandon"})


class _ResolveBody(BaseModel):
    edf: dict
    context: Optional[ResolutionContext] = None


def _compatibility(resolved: Any, profile: AgentProfile | None, name: str) -> dict:
    """The agent app compares an EDF against the ONE agent it runs; the
    daemon compares it against the managed agent the EDF names."""
    if profile is None:
        return {
            "compatible": False,
            "issues": [f"agent '{name}' is not managed by this daemon"],
        }
    issues: list[str] = []
    wants_model_tier = resolved.resolved_profile.get("spec") is not None
    if wants_model_tier:
        if profile.spec is None:
            issues.append(
                "EDF resolves to the model tier but this agent is executor-tier"
            )
        else:
            want_model = resolved.resolved_profile["spec"]["model"]
            if want_model != profile.spec.model:
                issues.append(
                    f"EDF resolves to model '{want_model}' but this agent is "
                    f"configured for '{profile.spec.model}'"
                )
    elif profile.executor is None:
        issues.append(
            "EDF resolves to an executor tier but this agent is model-tier"
        )
    else:
        want = resolved.resolved_profile["executor"]
        if want["executor"] != profile.executor.executor:
            issues.append(
                f"EDF resolves to executor '{want['executor']}' but this agent "
                f"runs '{profile.executor.executor}'"
            )
    return {"compatible": not issues, "issues": issues}


def register_contract_routes(
    app: FastAPI,
    core: Any,
    *,
    internal_token: str,
    transport: httpx.BaseTransport | None = None,
) -> None:
    client = httpx.Client(timeout=120.0, transport=transport)
    # Launch responses tell us which agent owns which run; anything launched
    # before this process started (or through an agent directly) is found by
    # asking the managed agents, so the index is an optimization that is
    # allowed to be lost on restart — never a source of truth.
    run_owners: dict[str, str] = {}

    def _authorized(request: Request) -> Response | None:
        if not internal_token:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "run-control on the daemon requires "
                    "MIRAGEN_INTERNAL_TOKEN to be configured",
                    "code": "contract_unconfigured",
                },
            )
        supplied = b""
        for raw_name, raw_value in request.headers.raw:
            if raw_name == b"x-miragen-token":
                supplied = raw_value
                break
        if not hmac.compare_digest(supplied, internal_token.encode("utf-8")):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "missing or invalid X-Miragen-Token",
                    "code": "unauthorized",
                },
            )
        return None

    def _managed_profile(name: str) -> AgentProfile | None:
        try:
            yaml_text = core.get_agent(name).get("yaml", "")
        except Exception:
            return None
        if not yaml_text:
            return None
        try:
            import yaml

            return AgentProfile.model_validate(yaml.safe_load(yaml_text))
        except Exception:
            # An unparseable stored profile reads as unmanaged rather than
            # crashing resolve — compatibility is informational here.
            return None

    def _forward(
        method: str, name: str, path: str, request_body: bytes, params: dict
    ) -> Response:
        try:
            upstream = client.request(
                method,
                f"{core.endpoint(name)}{path}",
                content=request_body or None,
                params=params or None,
                headers={
                    "X-Miragen-Token": internal_token,
                    "Content-Type": "application/json",
                },
            )
        except httpx.RequestError as error:
            return JSONResponse(
                status_code=502,
                content={
                    "detail": f"agent '{name}' is unreachable: {error!r}",
                    "code": "agent_unreachable",
                },
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    def _find_owner(run_id: str) -> str | None:
        cached = run_owners.get(run_id)
        if cached is not None:
            return cached
        for agent in core.list_agents():
            if agent.get("status") != "running":
                continue
            name = agent["name"]
            try:
                probe = client.get(
                    f"{core.endpoint(name)}/runs/{run_id}",
                    headers={"X-Miragen-Token": internal_token},
                    timeout=10.0,
                )
            except httpx.RequestError:
                continue
            if probe.status_code == 200:
                run_owners[run_id] = name
                return name
        return None

    @app.post("/profiles/resolve")
    async def resolve_profile(request: Request):
        denied = _authorized(request)
        if denied is not None:
            return denied
        try:
            body = _ResolveBody.model_validate_json(await request.body())
        except ValidationError as error:
            return JSONResponse(status_code=422, content={"detail": error.errors()})
        try:
            resolved = resolve_edf(body.edf, context=body.context)
        except EDFValidationError as error:
            return JSONResponse(
                status_code=422,
                content={"detail": {"error": "invalid EDF", "errors": error.errors}},
            )
        name = resolved.name
        payload = resolved.model_dump(mode="json")
        payload["agent_compatibility"] = _compatibility(
            resolved, _managed_profile(name), name
        )
        return JSONResponse(content=payload)

    @app.post("/executor-runs")
    async def launch_executor_run(request: Request):
        denied = _authorized(request)
        if denied is not None:
            return denied
        raw = await request.body()
        try:
            parsed = json.loads(raw)
        except ValueError:
            return JSONResponse(
                status_code=422, content={"detail": "body must be JSON"}
            )
        edf = parsed.get("edf") if isinstance(parsed, dict) else None
        if not isinstance(edf, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "daemon launches require an EDF — its "
                    "metadata.name selects the target agent. Launch through "
                    "the agent's own address for EDF-less runs",
                    "code": "edf_required",
                },
            )
        try:
            resolved = resolve_edf(edf)
        except EDFValidationError as error:
            return JSONResponse(
                status_code=422,
                content={"detail": {"error": "invalid EDF", "errors": error.errors}},
            )
        name = resolved.name
        statuses = {a["name"]: a.get("status") for a in core.list_agents()}
        if name not in statuses:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"EDF names agent '{name}', which this daemon "
                    "does not manage",
                    "code": "agent_not_found",
                },
            )
        if statuses[name] != "running":
            return JSONResponse(
                status_code=409,
                content={
                    "detail": f"agent '{name}' is not running "
                    f"(status: {statuses[name] or 'unknown'}); start it first",
                    "code": "agent_not_running",
                },
            )
        response = _forward("POST", name, "/executor-runs", raw, {})
        if response.status_code in (200, 202):
            try:
                run_id = json.loads(bytes(response.body)).get("run_id")
            except ValueError:
                run_id = None
            if run_id:
                run_owners[run_id] = name
        return response

    @app.get("/runs/{run_id}")
    @app.get("/runs/{run_id}/{subpath}")
    async def read_run(request: Request, run_id: str, subpath: str = ""):
        denied = _authorized(request)
        if denied is not None:
            return denied
        if subpath not in _PROXY_GET:
            return JSONResponse(
                status_code=404,
                content={"detail": f"unknown run resource '{subpath}'"},
            )
        name = _find_owner(run_id)
        if name is None:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"no running managed agent owns run '{run_id}'",
                    "code": "run_not_found",
                },
            )
        path = f"/runs/{run_id}" + (f"/{subpath}" if subpath else "")
        return _forward("GET", name, path, b"", dict(request.query_params))

    @app.post("/runs/{run_id}/{subpath}")
    async def act_on_run(request: Request, run_id: str, subpath: str):
        denied = _authorized(request)
        if denied is not None:
            return denied
        if subpath not in _PROXY_POST:
            return JSONResponse(
                status_code=404,
                content={"detail": f"unknown run action '{subpath}'"},
            )
        name = _find_owner(run_id)
        if name is None:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"no running managed agent owns run '{run_id}'",
                    "code": "run_not_found",
                },
            )
        return _forward(
            "POST",
            name,
            f"/runs/{run_id}/{subpath}",
            await request.body(),
            dict(request.query_params),
        )
