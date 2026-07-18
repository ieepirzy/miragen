"""Artifact sink — optional "put things that were produced somewhere" hook.

Not a primary channel (refinement plan §5): when a profile configures
`executor.artifact_sink`, the harvested diff is ALSO pushed there after a
successful run. Sink errors are logged and surfaced as `artifact_stored=False`
on the run record — they never raise into the run path and never change run
status. The diff on disk stays the source of truth.

LoimiSink speaks just enough MCP streamable-HTTP to call `store_document`:
initialize → notifications/initialized → tools/call, carrying the
mcp-session-id header the server hands back. Provenance rides on Loimi's
existing `open_run` auto-mint server-side — no extra plumbing here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import httpx

from miragen.models import ArtifactSinkSpec

logger = logging.getLogger("miragen.executor")

_PROTOCOL_VERSION = "2025-03-26"


class ArtifactSink(Protocol):
    async def store(self, *, diff: str, metadata: dict[str, Any]) -> None:
        """Push one produced artifact. Raises on failure; the caller decides
        what failure means (for miragen: log + artifact_stored=False)."""


class LoimiSink:
    """Stores harvested diffs as Loimi documents via `store_document`."""

    def __init__(
        self,
        spec: ArtifactSinkSpec,
        *,
        bearer_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.spec = spec
        self._bearer_token = bearer_token
        self._transport = transport  # test seam (httpx.MockTransport)

    async def store(self, *, diff: str, metadata: dict[str, Any]) -> None:
        headers = {"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else {}
        async with httpx.AsyncClient(
            transport=self._transport, headers=headers, timeout=30.0
        ) as client:
            session_headers = await self._initialize(client)
            result = await self._rpc(
                client,
                session_headers,
                id=2,
                method="tools/call",
                params={
                    "name": "store_document",
                    "arguments": {
                        "kind": self.spec.document_kind,
                        "content": diff,
                        "metadata": metadata,
                    },
                },
            )
            if isinstance(result, dict) and result.get("isError"):
                raise RuntimeError(f"store_document returned an error: {result.get('content')}")

    async def _initialize(self, client: httpx.AsyncClient) -> dict[str, str]:
        response = await self._post(
            client,
            {},
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "miragen", "version": "0"},
                },
            },
        )
        session_headers: dict[str, str] = {}
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            session_headers["mcp-session-id"] = session_id
        # initialized is a notification — no id, no response body expected.
        await self._post(
            client,
            session_headers,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return session_headers

    async def _rpc(
        self,
        client: httpx.AsyncClient,
        session_headers: dict[str, str],
        *,
        id: int,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        response = await self._post(
            client,
            session_headers,
            {"jsonrpc": "2.0", "id": id, "method": method, "params": params},
        )
        body = _parse_body(response)
        if body is None:
            raise RuntimeError(f"empty response to {method}")
        if "error" in body:
            raise RuntimeError(f"{method} failed: {body['error']}")
        return body.get("result")

    async def _post(
        self, client: httpx.AsyncClient, session_headers: dict[str, str], payload: dict
    ) -> httpx.Response:
        response = await client.post(
            self.spec.url,
            json=payload,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                **session_headers,
            },
        )
        response.raise_for_status()
        return response


def _parse_body(response: httpx.Response) -> dict | None:
    """Streamable-HTTP servers answer either plain JSON or a one-shot SSE
    stream; take the last data: frame in the SSE case."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        body = None
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    body = json.loads(line[len("data:"):].strip())
                except ValueError:
                    continue
        return body
    if not response.content:
        return None
    return response.json()


def build_sink(spec: ArtifactSinkSpec, *, bearer_token: str | None = None) -> ArtifactSink:
    # Single kind today; the Literal in ArtifactSinkSpec keeps this exhaustive.
    return LoimiSink(spec, bearer_token=bearer_token)
