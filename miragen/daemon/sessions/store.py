"""Loimi's ARTIFACT store (`/v0`) as seen from the session plane.

Distinct from `/memory/v1`: the store holds immutable artifacts with
deterministic provenance under runs, and it authenticates with the store
bearer (the operator credential), not a memory principal. The plane uses
it for two things — every external session gets a run and files its
episodes as artifacts under it, and the bridge MCP tools let the model
write, search and trace artifacts against that same run.

Failure vocabulary mirrors `MemoryClient`: `StoreUnavailable` for
transport/5xx, `StoreAPIError` for a refusal. Nothing here retries; the
callers decide what a failed write means for the session.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from miragen.daemon.sessions.config import StorePolicy
from miragen.daemon.sessions.models import ProjectIdentity
from miragen.daemon.sessions.projects import normalize_remote

_TIMEOUT_S = 15.0


class StoreUnavailable(Exception):
    """The store did not answer, or answered 5xx."""


class StoreAPIError(Exception):
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        detail = body.get("detail") if isinstance(body, dict) else body
        super().__init__(f"store answered {status_code}: {detail}")


class StoreClient:
    def __init__(
        self,
        policy: StorePolicy,
        *,
        environ: dict | None = None,
        memory_endpoint_env: str = "LOIMI_MEMORY_URL",
        operator_token_env: str = "LOIMI_OPERATOR_TOKEN",
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self.policy = policy
        self._environ = os.environ if environ is None else environ
        self._memory_endpoint_env = memory_endpoint_env
        self._operator_token_env = operator_token_env
        self._transport = transport
        self._base_url_override = base_url
        self._token_override = token

    # -- resolution --------------------------------------------------------

    def base_url(self) -> str:
        if self._base_url_override:
            return self._base_url_override.rstrip("/")
        url = self._environ.get(self.policy.endpoint_env) or self._environ.get(
            self._memory_endpoint_env
        )
        if not url:
            raise StoreUnavailable(
                f"store endpoint env {self.policy.endpoint_env} (or "
                f"{self._memory_endpoint_env}) is not set"
            )
        return url.rstrip("/")

    def token(self) -> str:
        if self._token_override:
            return self._token_override
        token = self._environ.get(self.policy.credential_env) or self._environ.get(
            self._operator_token_env
        )
        if not token:
            raise StoreUnavailable(
                f"store credential env {self.policy.credential_env} (or "
                f"{self._operator_token_env}) is not set"
            )
        return token

    def configured(self) -> bool:
        try:
            self.base_url()
            self.token()
        except StoreUnavailable:
            return False
        return True

    def namespace_for(self, project: ProjectIdentity | None) -> str:
        """The namespace a project's runs open in: the first binding whose
        `match` is the project's remote form, a `host/org/*` prefix of it,
        or a path prefix of its root, else the policy default."""
        if project is not None:
            for binding in self.policy.namespaces:
                match = binding.match.rstrip("/")
                if match.startswith("/"):
                    root = (project.root or "").rstrip("/")
                    if root == match or root.startswith(match + "/"):
                        return binding.namespace
                elif match.endswith("/*"):
                    prefix = normalize_remote(match[:-2]) + "/"
                    if project.id.startswith(prefix):
                        return binding.namespace
                elif project.id == match.lower() or project.id == normalize_remote(match):
                    return binding.namespace
        return self.policy.namespace

    # -- transport ---------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url()}/v0{path}"
        headers = {"Authorization": f"Bearer {self.token()}"}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S, transport=self._transport) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise StoreUnavailable(f"artifact store unreachable: {exc}") from exc
        if resp.status_code >= 500:
            raise StoreUnavailable(f"artifact store answered {resp.status_code}")
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise StoreAPIError(resp.status_code, body)
        if not resp.content:
            return {}
        return resp.json()

    # -- runs --------------------------------------------------------------

    async def open_run(
        self, *, task: str, namespace: str, agent_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "agent_id": agent_id or self.policy.agent_id, "task": task[:2000],
            "namespace": namespace,
        }
        if parent_run_id:
            body["parent_run_id"] = parent_run_id
        return await self._request("POST", "/runs", json=body)

    async def close_run(self, run_id: str, status: str) -> dict:
        return await self._request("PATCH", f"/runs/{run_id}", json={"status": status})

    async def get_run(self, run_id: str) -> dict:
        return await self._request("GET", f"/runs/{run_id}")

    async def run_tree(self, run_id: str, *, include_artifacts: bool = True) -> dict:
        return await self._request(
            "GET", f"/runs/{run_id}/tree",
            params={"include_artifacts": str(include_artifacts).lower()},
        )

    async def list_runs(self, **params: Any) -> dict:
        return await self._request(
            "GET", "/runs", params={k: v for k, v in params.items() if v is not None},
        )

    # -- artifacts ----------------------------------------------------------

    async def put_artifact(
        self, *, run_id: str, kind: str, content: str | None = None,
        content_ref: str | None = None, properties: dict | None = None,
        sources: list[str] | None = None, suggested_namespaces: list[str] | None = None,
        as_of: str | None = None, skip_provenance: bool = False,
    ) -> dict:
        body: dict[str, Any] = {
            "kind": kind, "run_id": run_id, "properties": properties or {},
            "sources": sources or [], "suggested_namespaces": suggested_namespaces or [],
            "skip_provenance": skip_provenance,
        }
        if content is not None:
            body["content"] = content
        if content_ref is not None:
            body["content_ref"] = content_ref
        if as_of:
            body["as_of"] = as_of
        return await self._request("POST", "/artifacts", json=body)

    async def supersede_artifact(self, artifact_id: str, **body: Any) -> dict:
        return await self._request(
            "POST", f"/artifacts/{artifact_id}/supersede",
            json={k: v for k, v in body.items() if v is not None},
        )

    async def get_artifact(self, artifact_id: str) -> dict:
        return await self._request("GET", f"/artifacts/{artifact_id}")

    async def lineage(self, artifact_id: str, *, direction: str = "up", max_depth: int = 10) -> dict:
        return await self._request(
            "GET", f"/artifacts/{artifact_id}/lineage",
            params={"direction": direction, "max_depth": max_depth},
        )

    async def search(
        self, *, q: str | None = None, namespaces: list[str] | None = None,
        kinds: list[str] | None = None, limit: int | None = None, cursor: str | None = None,
    ) -> dict:
        body = {k: v for k, v in {
            "q": q, "namespaces": namespaces, "kinds": kinds, "limit": limit, "cursor": cursor,
        }.items() if v is not None}
        return await self._request("POST", "/search", json=body)

    async def list_namespaces(self) -> Any:
        return await self._request("GET", "/namespaces")

    async def namespace_artifacts(self, namespace: str, **params: Any) -> dict:
        return await self._request(
            "GET", f"/namespaces/{namespace}/artifacts",
            params={k: v for k, v in params.items() if v is not None},
        )
