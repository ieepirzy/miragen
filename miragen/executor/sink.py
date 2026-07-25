"""Low-level Loimi ``store_document`` helper — **not** used on success.

Reviewed whole-run graduation is ``miragen.publication`` /
``POST /runs/{id}/publications`` (capability ``reviewed-publication/v1``).

This module is intentionally **not** on the executor finish path. Auto-push
of harvested diffs on success was removed so publication only happens after
explicit human-reviewed graduation. Do not re-wire ``build_sink`` into
``_run_executor_turn`` or equivalent.

``LoimiSink`` remains as a thin one-shot MCP helper (tests / ad-hoc tools).
Prefer ``LoimiPublicationBackend`` for whole-run open_run → store → close.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from miragen.models import ArtifactSinkSpec
from miragen.publication import McpStreamableClient, PublicationError

logger = logging.getLogger("miragen.executor")


class ArtifactSink(Protocol):
    async def store(self, *, diff: str, metadata: dict[str, Any]) -> None:
        """Push one document. Raises on failure."""


class LoimiSink:
    """One-shot ``store_document`` over streamable-HTTP MCP.

    Not a publication backend. For reviewed graduation use
    ``LoimiPublicationBackend`` / ``POST /runs/{id}/publications``.
    """

    def __init__(
        self,
        spec: ArtifactSinkSpec,
        *,
        bearer_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.spec = spec
        self._bearer_token = bearer_token
        self._transport = transport

    async def store(self, *, diff: str, metadata: dict[str, Any]) -> None:
        try:
            async with McpStreamableClient(
                self.spec.url,
                bearer_token=self._bearer_token,
                transport=self._transport,
                client_name="miragen-store_document",
            ) as mcp:
                result = await mcp.call_tool(
                    "store_document",
                    {
                        "kind": self.spec.document_kind,
                        "content": diff,
                        "metadata": metadata,
                    },
                )
        except PublicationError as e:
            raise RuntimeError(str(e)) from e
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(f"store_document returned an error: {result.get('content')}")


def build_sink(spec: ArtifactSinkSpec, *, bearer_token: str | None = None) -> ArtifactSink:
    """Build a low-level one-shot sink. Not used by the executor success path."""
    if spec.kind == "loimi":
        return LoimiSink(spec, bearer_token=bearer_token)
    raise ValueError(f"unknown sink kind: {spec.kind!r}")
