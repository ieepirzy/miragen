"""Reviewed whole-run publication — explicit, backend-agnostic graduation.

Orchestrators (e.g. MiraRun) never write artifacts or provenance to a document
store themselves. After human review they call POST /runs/{id}/publications;
miragen reads its authoritative run/diff and hands the package to a configured
**publication backend** (first implementation: Loimi over MCP).

This is deliberately *not* auto-fired on executor success. The legacy
auto-push path is retired; `executor.artifact_sink` only configures the
backend used by the reviewed-publication endpoint.
`miragen.executor.sink` is a low-level store_document helper only — do not
re-wire it into the executor finish path.

Idempotency lives in miragen (per idempotency_key → PublicationRecord). Retry
after Loimi/network failure re-enters the backend; duplicate keys return the
prior opaque references without writing again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from miragen.models import ArtifactSinkSpec, RunRecord

logger = logging.getLogger("miragen.publication")

_PROTOCOL_VERSION = "2025-03-26"

# In-process locks per idempotency key — serializes concurrent claims inside
# one agent process. Cross-process safety uses O_EXCL claim files on disk.
_key_locks: dict[str, asyncio.Lock] = {}
_key_locks_guard = asyncio.Lock()


# ── Contract models ──────────────────────────────────────────────────────────


class PublicationProvenance(BaseModel):
    """Caller-supplied provenance for the *publication request* (not the run).

    Extra fields allowed so orchestrators can evolve without miragen changes;
    known MiraRun fields are typed for validation when present.
    """

    model_config = {"extra": "allow"}

    mirarun_run_intent_id: str | None = None
    environment_id: str | None = None
    environment_revision: int | None = None
    requested_by: str | None = None


class PublicationRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=512)
    provenance: PublicationProvenance = Field(default_factory=PublicationProvenance)


class PublicationResult(BaseModel):
    """Opaque outcome from a publication backend (before miragen ids are stamped)."""

    backend: str
    external_run_id: str
    external_artifact_ids: list[str] = Field(default_factory=list)


class PublicationRecord(BaseModel):
    """Persisted publication — miragen's memory of a reviewed graduation."""

    publication_id: str
    run_id: str
    idempotency_key: str
    status: Literal["published"] = "published"
    backend: str
    external_run_id: str
    external_artifact_ids: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PublicationResponse(BaseModel):
    """HTTP response. Field names stay backend-agnostic; values are opaque.

    MiraRun (and other orchestrators) store only these references — never
    re-write artifacts into the backend store.
    """

    publication_id: str
    run_id: str
    status: Literal["published"] = "published"
    duplicate: bool = False
    backend: str
    # Opaque backend references (canonical for all backends).
    external_run_id: str
    external_artifact_ids: list[str] = Field(default_factory=list)
    # Convenience aliases when backend == "loimi" (same opaque values).
    # Kept so MiraRun can keep loimi_* names without forking the contract.
    loimi_run_id: str | None = None
    loimi_artifact_ids: list[str] | None = None

    @classmethod
    def from_record(cls, record: PublicationRecord, *, duplicate: bool) -> "PublicationResponse":
        loimi_run = record.external_run_id if record.backend == "loimi" else None
        loimi_arts = list(record.external_artifact_ids) if record.backend == "loimi" else None
        return cls(
            publication_id=record.publication_id,
            run_id=record.run_id,
            status="published",
            duplicate=duplicate,
            backend=record.backend,
            external_run_id=record.external_run_id,
            external_artifact_ids=list(record.external_artifact_ids),
            loimi_run_id=loimi_run,
            loimi_artifact_ids=loimi_arts,
        )


class PublicationError(Exception):
    """Base for publication failures. `retryable` drives HTTP 503 vs 4xx/502."""

    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code or (503 if retryable else 502)


class PublicationConfigError(PublicationError):
    def __init__(self, message: str):
        super().__init__(message, retryable=False, status_code=400)


class PublicationUnavailableError(PublicationError):
    def __init__(self, message: str):
        super().__init__(message, retryable=True, status_code=503)


# ── Backend protocol ─────────────────────────────────────────────────────────


class PublicationBackend(Protocol):
    """Pluggable graduation target. miragen never hard-codes product semantics
    beyond the first built-in Loimi MCP backend; new kinds register here."""

    async def publish_run(
        self,
        *,
        diff: str,
        run: RunRecord,
        provenance: dict[str, Any],
    ) -> PublicationResult:
        """Publish one whole reviewed run. Raises PublicationError on failure."""
        ...


class McpStreamableClient:
    """Minimal streamable-HTTP MCP client (initialize + tools/call)."""

    def __init__(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client_name: str = "miragen",
    ):
        self.url = url
        self._bearer_token = bearer_token
        self._transport = transport
        self._client_name = client_name
        self._rpc_id = 0

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def __aenter__(self) -> "McpStreamableClient":
        headers = {"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else {}
        self._client = httpx.AsyncClient(
            transport=self._transport, headers=headers, timeout=60.0
        )
        self._session_headers = await self._initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self._rpc(
            method="tools/call",
            params={"name": name, "arguments": arguments},
        )
        if isinstance(result, dict) and result.get("isError"):
            raise PublicationError(
                f"tool {name!r} returned an error: {result.get('content')}",
                retryable=False,
                status_code=502,
            )
        return result

    async def _initialize(self) -> dict[str, str]:
        response = await self._post(
            {},
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": self._client_name, "version": "0"},
                },
            },
        )
        session_headers: dict[str, str] = {}
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            session_headers["mcp-session-id"] = session_id
        await self._post(
            session_headers,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return session_headers

    async def _rpc(self, *, method: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._post(
                self._session_headers,
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": method,
                    "params": params,
                },
            )
        except httpx.HTTPError as e:
            raise PublicationUnavailableError(f"MCP transport error on {method}: {e}") from e
        try:
            body = _parse_body(response)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            # Successful HTTP with garbage body — treat as retryable upstream flake.
            raise PublicationUnavailableError(
                f"malformed MCP response to {method}: {e}"
            ) from e
        if body is None:
            raise PublicationUnavailableError(f"empty response to {method}")
        if "error" in body:
            err = body["error"]
            # Treat server/internal errors as retryable; application errors not.
            code = err.get("code") if isinstance(err, dict) else None
            retryable = isinstance(code, int) and code in (-32000, -32603)
            raise PublicationError(
                f"{method} failed: {err}",
                retryable=retryable,
                status_code=503 if retryable else 502,
            )
        return body.get("result")

    async def _post(
        self, session_headers: dict[str, str], payload: dict
    ) -> httpx.Response:
        response = await self._client.post(
            self.url,
            json=payload,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                **session_headers,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                raise PublicationUnavailableError(
                    f"MCP HTTP {e.response.status_code} on {self.url}"
                ) from e
            raise PublicationError(
                f"MCP HTTP {e.response.status_code} on {self.url}",
                retryable=False,
                status_code=502,
            ) from e
        return response


def _parse_body(response: httpx.Response) -> dict | None:
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
    # May raise JSONDecodeError / ValueError — callers map to PublicationUnavailableError.
    return response.json()


def _extract_id(result: Any, *keys: str) -> str | None:
    """Pull an opaque id from a tools/call result (structured or text JSON)."""
    if result is None:
        return None
    if isinstance(result, str) and result.strip():
        return result.strip()
    if isinstance(result, dict):
        for k in keys:
            v = result.get(k)
            if isinstance(v, str) and v:
                return v
        # MCP content blocks
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                try:
                    parsed = json.loads(text)
                except ValueError:
                    # bare id string
                    if re.fullmatch(r"[\w.:@/-]+", text.strip()):
                        return text.strip()
                    continue
                if isinstance(parsed, dict):
                    for k in keys:
                        v = parsed.get(k)
                        if isinstance(v, str) and v:
                            return v
                elif isinstance(parsed, str) and parsed:
                    return parsed
    return None


class LoimiPublicationBackend:
    """Loimi MCP backend: store_open_run → store_put_artifact → store_close_run.

    Tool names/args are the Loimi-facing contract (loimi/src/loimi/mcp_server.py
    — exactly 8 tools, none named open_run/store_document/close_run); miragen
    only depends on the PublicationBackend protocol. Other backends implement
    the same protocol without Loimi tool names.
    """

    kind = "loimi"

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

    async def publish_run(
        self,
        *,
        diff: str,
        run: RunRecord,
        provenance: dict[str, Any],
    ) -> PublicationResult:
        properties = {
            "miragen_run_id": run.run_id,
            # The miragen profile/agent name — Loimi's Run.agent_id and this
            # artifact's producer identity. routine_slug (below) rides
            # alongside it, never in place of it: the profile says who ran
            # this, the slug says which mirarun routine asked for it, and an
            # agent can be invoked by more than one routine.
            "agent": run.agent_name,
            "thread_id": run.thread_id,
            "executor": run.executor,
            "model": run.model,
            "snapshot_sha256": run.snapshot_sha256,
            "publication_provenance": provenance,
            "role": "executor_diff",
        }
        routine_slug = provenance.get("routine_slug")
        if routine_slug:
            properties["routine_slug"] = routine_slug

        async with McpStreamableClient(
            self.spec.url,
            bearer_token=self._bearer_token,
            transport=self._transport,
            client_name="miragen-publication",
        ) as mcp:
            open_result = await mcp.call_tool(
                "store_open_run",
                {
                    "agent_id": run.agent_name,
                    "task": run.prompt,
                    # Loimi auto-mints an agent's home namespace (same id as
                    # the agent) the first time that agent_id opens a run
                    # (loimi/src/loimi/service.py:open_run) and never
                    # auto-creates any other namespace — passing anything
                    # else here risks UnknownNamespace on a namespace nobody
                    # registered yet.
                    "namespace": run.agent_name,
                },
            )
            external_run_id = _extract_id(open_result, "id", "run_id")
            if not external_run_id:
                raise PublicationError(
                    f"store_open_run did not return a run id: {open_result!r}",
                    retryable=False,
                    status_code=502,
                )

            store_result = await mcp.call_tool(
                "store_put_artifact",
                {
                    "run_id": external_run_id,
                    "kind": self.spec.document_kind,
                    "content": diff,
                    "properties": {**properties, "loimi_run_id": external_run_id},
                },
            )
            artifact_id = _extract_id(
                store_result, "id", "document_id", "artifact_id", "doc_id"
            )
            if not artifact_id:
                # Fall back to a stable synthetic id only when the backend
                # acknowledges success without an id (some MCP tools return
                # plain text "stored"). Prefer an explicit opaque token.
                text = _extract_id(store_result, "text")
                artifact_id = text or f"doc:{external_run_id}:diff"

            await mcp.call_tool(
                "store_close_run",
                {"run_id": external_run_id, "status": "succeeded"},
            )

        return PublicationResult(
            backend=self.kind,
            external_run_id=external_run_id,
            external_artifact_ids=[artifact_id],
        )


def build_publication_backend(
    spec: ArtifactSinkSpec,
    *,
    bearer_token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PublicationBackend:
    if spec.kind == "loimi":
        return LoimiPublicationBackend(spec, bearer_token=bearer_token, transport=transport)
    raise PublicationConfigError(f"unknown publication backend kind: {spec.kind!r}")


# ── Idempotent store ─────────────────────────────────────────────────────────


class PublicationStore:
    """Append-only publication records keyed by idempotency_key and publication_id.

    Directory creation is deferred to first write so lifespan can construct a
    store against a default root that may not exist yet (same pattern as
    RunStore — unit tests that only exercise the scheduler must not require
    /agent/runs to be writable).
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def get_by_idempotency_key(self, key: str) -> PublicationRecord | None:
        path = self._key_path(key)
        if not path.exists():
            return None
        try:
            return PublicationRecord.model_validate_json(path.read_text())
        except Exception:
            logger.warning("corrupt publication record at %s", path, exc_info=True)
            return None

    def get(self, publication_id: str) -> PublicationRecord | None:
        path = self.root / f"id-{publication_id}.json"
        if not path.exists():
            return None
        try:
            return PublicationRecord.model_validate_json(path.read_text())
        except Exception:
            return None

    def save(self, record: PublicationRecord) -> PublicationRecord:
        # Dual index: by idempotency key (dedupe) and by publication id (lookup).
        self.root.mkdir(parents=True, exist_ok=True)
        payload = record.model_dump_json(indent=2)
        key_path = self._key_path(record.idempotency_key)
        id_path = self.root / f"id-{record.publication_id}.json"
        claim_path = self._claim_path(record.idempotency_key)
        tmp = key_path.with_suffix(".tmp")
        tmp.write_text(payload)
        tmp.replace(key_path)
        id_path.write_text(payload)
        # Drop in-flight claim once the durable record is in place.
        with contextlib.suppress(FileNotFoundError):
            claim_path.unlink()
        return record

    def try_begin(self, key: str, run_id: str) -> PublicationRecord | None:
        """Atomically claim `key` for an in-flight publication.

        Returns an existing completed record if the key was already published.
        Returns None if this caller now holds the claim and should proceed.
        Raises PublicationUnavailableError if another publication is in flight
        for the same key (caller should retry).
        """
        existing = self.get_by_idempotency_key(key)
        if existing is not None:
            return existing
        self.root.mkdir(parents=True, exist_ok=True)
        claim_path = self._claim_path(key)
        payload = json.dumps({"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat()})
        try:
            fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # Another worker holds the claim, or a crashed claim was left behind.
            # If a completed record appeared, return it; else ask the client to retry.
            existing = self.get_by_idempotency_key(key)
            if existing is not None:
                return existing
            raise PublicationUnavailableError(
                f"publication for idempotency_key already in progress for run {run_id}"
            )
        try:
            os.write(fd, payload.encode())
        finally:
            os.close(fd)
        # Re-check after claim in case a complete record raced in (shouldn't, but cheap).
        existing = self.get_by_idempotency_key(key)
        if existing is not None:
            with contextlib.suppress(FileNotFoundError):
                claim_path.unlink()
            return existing
        return None

    def release_claim(self, key: str) -> None:
        """Drop an in-flight claim after a failed backend attempt so retries work."""
        with contextlib.suppress(FileNotFoundError):
            self._claim_path(key).unlink()

    def _key_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.root / f"key-{digest}.json"

    def _claim_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.root / f"key-{digest}.claim"


def preconditions_ok(run: RunRecord) -> str | None:
    """Return an error message if the run is not eligible for publication, else None."""
    if run.status != "succeeded":
        return f"run status is {run.status!r}; only succeeded runs can be published"
    if not run.diff_path:
        return "run has no harvested diff_path; incomplete or non-executor success"
    if not Path(run.diff_path).is_file():
        return f"diff file missing on disk: {run.diff_path}"
    return None


async def _lock_for_key(key: str) -> asyncio.Lock:
    async with _key_locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _key_locks[key] = lock
        return lock


async def publish_reviewed_run(
    *,
    run: RunRecord,
    request: PublicationRequest,
    store: PublicationStore,
    backend: PublicationBackend,
    annotate_run: Callable[..., RunRecord],
) -> PublicationResponse:
    """Idempotent reviewed publication. Does not mutate run.status."""
    reason = preconditions_ok(run)
    if reason:
        raise PublicationConfigError(reason)

    lock = await _lock_for_key(request.idempotency_key)
    async with lock:
        claimed = store.try_begin(request.idempotency_key, run.run_id)
        if claimed is not None:
            if claimed.run_id != run.run_id:
                raise PublicationConfigError(
                    f"idempotency_key already bound to run {claimed.run_id}, not {run.run_id}"
                )
            return PublicationResponse.from_record(claimed, duplicate=True)

        try:
            diff = Path(run.diff_path).read_text()  # type: ignore[arg-type]
            result = await backend.publish_run(
                diff=diff,
                run=run,
                provenance=request.provenance.model_dump(exclude_none=True),
            )

            record = PublicationRecord(
                publication_id=uuid.uuid4().hex,
                run_id=run.run_id,
                idempotency_key=request.idempotency_key,
                status="published",
                backend=result.backend,
                external_run_id=result.external_run_id,
                external_artifact_ids=list(result.external_artifact_ids),
                provenance=request.provenance.model_dump(exclude_none=True),
                created_at=datetime.now(timezone.utc),
            )
            store.save(record)
        except Exception:
            # Allow a retry of the same key after a failed attempt.
            store.release_claim(request.idempotency_key)
            raise

        try:
            annotate_run(run, artifact_stored=True)
        except Exception:
            logger.warning(
                "publication %s saved but run annotate failed for %s",
                record.publication_id,
                run.run_id,
                exc_info=True,
            )
        return PublicationResponse.from_record(record, duplicate=False)
