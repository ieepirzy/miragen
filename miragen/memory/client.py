"""HTTP client for Loimi's /memory/v1 (the PR-1 surface).

Two failure vocabularies, kept apart on purpose:
- `MemoryUnavailable` — the service cannot be reached (network, timeout,
  5xx). The lifecycle degrades EXPLICITLY on this: never misreported as
  "no relevant memories" (§8.6).
- `MemoryAPIError` — the service answered and refused (4xx): a contract
  or authorization problem the caller must surface, not retry blindly.

Credentials arrive by env var NAME from the profile's memory block; the
minted principal token is read at call time (after _load_file_secrets).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from miragen.models import MemorySpec

_TIMEOUT_S = 10.0


class MemoryUnavailable(Exception):
    """The memory service could not be reached — degrade explicitly."""


class MemoryAPIError(Exception):
    """The memory service refused the request."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        message = body
        if isinstance(body, dict):
            message = body.get("error", {}).get("message", body)
        super().__init__(f"memory API {status_code}: {message}")


class MemoryClient:
    """Thin, typed-enough wrapper over /memory/v1. `transport` is the test
    seam (httpx.MockTransport)."""

    def __init__(
        self,
        spec: MemorySpec,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self.spec = spec
        self._transport = transport
        # Explicit overrides beat env resolution — the ephemeral backend
        # wires its in-process endpoint/token without any environment.
        self._base_url_override = base_url
        self._token_override = token

    def _base_url(self) -> str:
        if self._base_url_override:
            return self._base_url_override.rstrip("/")
        url = os.environ.get(self.spec.endpoint_env)
        if not url:
            raise MemoryUnavailable(
                f"memory endpoint env {self.spec.endpoint_env} is not set"
            )
        return url.rstrip("/")

    def _token(self) -> str:
        if self._token_override:
            return self._token_override
        token = os.environ.get(self.spec.credential_env)
        if not token:
            raise MemoryUnavailable(
                f"memory credential env {self.spec.credential_env} is not set"
            )
        return token

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base_url()}/memory/v1{path}"
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_S, transport=self._transport
            ) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise MemoryUnavailable(f"memory service unreachable: {exc}") from exc
        if resp.status_code >= 500:
            raise MemoryUnavailable(f"memory service answered {resp.status_code}")
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise MemoryAPIError(resp.status_code, body)
        return resp.json()

    # -- work contexts ----------------------------------------------------

    async def create_context(
        self, *, scope_id: str, kind: str = "task", title: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict:
        return await self._request("POST", "/contexts", json={
            "scope_id": scope_id, "kind": kind, "title": title, "state": state or {},
        })

    async def get_context(self, context_id: str) -> dict:
        return await self._request("GET", f"/contexts/{context_id}")

    async def patch_context(
        self, context_id: str, *, expected_revision: int,
        patch: dict[str, Any] | None = None, state: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"expected_revision": expected_revision}
        if patch is not None:
            body["patch"] = patch
        if state is not None:
            body["state"] = state
        if title is not None:
            body["title"] = title
        return await self._request("PATCH", f"/contexts/{context_id}", json=body)

    # -- events / records --------------------------------------------------

    async def append_event(
        self, *, scope_id: str, idempotency_key: str, source: dict[str, Any],
        content: str, attributes: dict[str, Any] | None = None,
        occurred_at: str | None = None, context_ids: list[str] | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "scope_id": scope_id, "idempotency_key": idempotency_key,
            "source": source, "content": content,
            "attributes": attributes or {},
        }
        if occurred_at is not None:
            body["occurred_at"] = occurred_at
        if context_ids:
            body["context_ids"] = context_ids
        return await self._request("POST", "/events", json=body)

    async def propose_record(self, body: dict[str, Any]) -> dict:
        return await self._request("POST", "/records", json=body)

    async def get_record(self, record_id: str, *, seq: int | None = None) -> dict:
        params = {"seq": seq} if seq is not None else None
        return await self._request("GET", f"/records/{record_id}", params=params)

    async def query_claims(
        self, *, scope_id: str, subject: str, predicate: str | None = None,
        at: str | None = None, as_of_record: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"scope_id": scope_id, "subject": subject}
        if predicate is not None:
            params["predicate"] = predicate
        if at is not None:
            params["at"] = at
        if as_of_record is not None:
            params["as_of_record"] = as_of_record
        return await self._request("GET", "/claims", params=params)

    async def correct_record(self, record_id: str, body: dict[str, Any]) -> dict:
        return await self._request("POST", f"/records/{record_id}/correct", json=body)

    # -- worker jobs (maintain capability) ---------------------------------

    async def claim_jobs(
        self, *, kinds: list[str], limit: int = 1, lease_seconds: int = 120
    ) -> list[dict]:
        result = await self._request("POST", "/jobs/claim", json={
            "kinds": kinds, "limit": limit, "lease_seconds": lease_seconds,
        })
        return result["items"]

    async def complete_job(self, job_id: str) -> dict:
        return await self._request("POST", f"/jobs/{job_id}/complete")

    async def fail_job(self, job_id: str, *, error: str, retry: bool = True) -> dict:
        return await self._request("POST", f"/jobs/{job_id}/fail",
                                   json={"error": error, "retry": retry})

    async def search_memory(self, body: dict[str, Any]) -> dict:
        return await self._request("POST", "/search", json=body)

    async def get_projection(self, revision_id: str) -> dict:
        return await self._request("GET", f"/projections/{revision_id}")

    async def set_projection_embedding(
        self, revision_id: str, *, embedding: list[float], space: str
    ) -> dict:
        return await self._request(
            "PUT", f"/projections/{revision_id}/embedding",
            json={"embedding": embedding, "space": space},
        )

    async def get_event(self, event_id: str) -> dict:
        return await self._request("GET", f"/events/{event_id}")

    # -- manifests ---------------------------------------------------------

    async def create_manifest(self, body: dict[str, Any]) -> dict:
        return await self._request("POST", "/manifests", json=body)
