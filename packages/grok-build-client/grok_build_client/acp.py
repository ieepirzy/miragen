"""ACP stdio transport: long-lived `grok agent stdio` JSON-RPC client.

Zero third-party deps. Implements the subset miragen needs:
initialize → authenticate → session/new → session/prompt, plus
session/update notifications and session/request_permission for host gates.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from grok_build_client.normalize import normalize_acp_update
from grok_build_client.util import resolve_grok_bin

PermissionDecision = Literal["allow", "deny"]
PermissionHandler = Callable[[dict[str, Any]], PermissionDecision | Awaitable[PermissionDecision]]


@dataclass
class AcpSession:
    """One ACP agent process (stdio). Use as an async context manager."""

    grok_bin: str | None = None
    grok_home: str | None = None
    always_approve: bool = True
    model: str | None = None
    env: Mapping[str, str] | None = None
    permission_handler: PermissionHandler | None = None
    # Test seam: inject a pre-built (reader, writer, kill) triple instead of spawning.
    _transport_factory: Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter, Callable[[], None]]]] | None = None

    _proc: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _reader: asyncio.StreamReader | None = field(default=None, init=False, repr=False)
    _writer: asyncio.StreamWriter | None = field(default=None, init=False, repr=False)
    _kill: Callable[[], None] | None = field(default=None, init=False, repr=False)
    _next_id: int = field(default=1, init=False, repr=False)
    _pending: dict[int, asyncio.Future[Any]] = field(default_factory=dict, init=False, repr=False)
    _updates: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue, init=False, repr=False)
    _reader_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def __aenter__(self) -> "AcpSession":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._reader is not None:
            return
        if self._transport_factory is not None:
            self._reader, self._writer, self._kill = await self._transport_factory()
        else:
            await self._spawn()
        self._reader_task = asyncio.create_task(self._read_loop(), name="grok-acp-reader")
        await self._handshake()

    async def _spawn(self) -> None:
        grok = resolve_grok_bin(self.grok_bin)
        if not grok:
            raise FileNotFoundError(
                "grok CLI not found on PATH (set GROK_BIN or install Grok Build)"
            )
        argv = [grok, "agent"]
        if self.always_approve:
            argv.append("--always-approve")
        if self.model:
            argv.extend(["-m", self.model])
        argv.extend(["--no-leader", "stdio"])

        env = dict(self.env) if self.env is not None else os.environ.copy()
        if self.grok_home:
            env["GROK_HOME"] = self.grok_home

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
            limit=16 * 1024 * 1024,
        )
        assert proc.stdin and proc.stdout
        self._proc = proc
        self._reader = proc.stdout
        # StreamWriter-like wrapper around stdin
        self._writer = proc.stdin  # type: ignore[assignment]

        def _kill() -> None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

        self._kill = _kill

    async def _handshake(self) -> None:
        init = await self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
            "clientInfo": {"name": "grok-build-client", "version": "0.1.0"},
        })
        methods = {
            (m.get("id") if isinstance(m, dict) else None)
            for m in (init.get("authMethods") or [])
            if isinstance(m, dict)
        }
        # Prefer cached subscription token; fall back to API key auth method id.
        if os.environ.get("XAI_API_KEY") and "xai.api_key" in methods:
            method_id = "xai.api_key"
        elif "cached_token" in methods:
            method_id = "cached_token"
        elif methods:
            method_id = next(iter(x for x in methods if x))
        else:
            method_id = "cached_token"
        try:
            await self.request("authenticate", {
                "methodId": method_id,
                "_meta": {"headless": True},
            })
        except Exception:
            # Some agent builds auto-auth from GROK_HOME; non-fatal if already in.
            pass

    async def session_new(
        self,
        cwd: str,
        *,
        mcp_servers: list[dict[str, Any]] | None = None,
        yolo: bool | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "cwd": cwd,
            "mcpServers": mcp_servers or [],
        }
        _meta = dict(meta or {})
        if yolo is None:
            yolo = self.always_approve
        if yolo:
            _meta.setdefault("yoloMode", True)
        if _meta:
            body["_meta"] = _meta
        result = await self.request("session/new", body)
        session_id = result.get("sessionId") or result.get("session_id")
        if not session_id:
            raise RuntimeError(f"session/new missing sessionId: {result!r}")
        return str(session_id)

    async def session_load(self, session_id: str, cwd: str | None = None) -> str:
        params: dict[str, Any] = {"sessionId": session_id}
        if cwd:
            params["cwd"] = cwd
        result = await self.request("session/load", params)
        return str(result.get("sessionId") or session_id)

    async def prompt(self, session_id: str, text: str) -> AsyncIterator[dict[str, Any]]:
        """Send a prompt; yield normalized updates until the request completes."""
        # Drain any stale updates
        while not self._updates.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._updates.get_nowait()

        req_task = asyncio.create_task(self.request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        }))

        while True:
            get_update = asyncio.create_task(self._updates.get())
            done, pending = await asyncio.wait(
                {req_task, get_update},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get_update in done:
                item = get_update.result()
                if item is None:
                    break
                yield item
            else:
                get_update.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await get_update
            if req_task in done:
                # Flush remaining updates briefly
                while True:
                    try:
                        item = self._updates.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item is not None:
                        yield item
                try:
                    result = req_task.result()
                except Exception as e:
                    yield {"type": "error", "message": str(e)}
                    return
                # Terminal end event with optional usage from result/_meta
                usage = {}
                meta = result.get("_meta") if isinstance(result, dict) else None
                if isinstance(meta, dict) and isinstance(meta.get("usage"), dict):
                    usage = meta["usage"]
                yield {
                    "type": "end",
                    "sessionId": session_id,
                    "stopReason": (result or {}).get("stopReason") if isinstance(result, dict) else None,
                    "usage": usage,
                    "raw": result,
                }
                return

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 3600) -> Any:
        if self._writer is None:
            raise RuntimeError("AcpSession not started")
        req_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        payload = (json.dumps(msg) + "\n").encode()
        # Process.stdin is a StreamWriter in create_subprocess_exec
        writer = self._writer
        if hasattr(writer, "write"):
            writer.write(payload)  # type: ignore[union-attr]
            await writer.drain()  # type: ignore[union-attr]
        else:
            raise RuntimeError("ACP writer does not support write()")
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            self._pending.pop(req_id, None)
            raise

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._dispatch(msg)
        finally:
            # Unblock waiters
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("ACP agent closed"))
            self._pending.clear()
            await self._updates.put(None)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        # Response to our request
        if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"]
                detail = err.get("message") if isinstance(err, dict) else str(err)
                fut.set_exception(RuntimeError(detail or "ACP error"))
            else:
                fut.set_result(msg.get("result") or {})
            return

        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        # Permission request (server → client request with id)
        if method in ("session/request_permission", "request_permission") and "id" in msg:
            decision = await self._decide_permission(params)
            await self._respond(msg["id"], {"outcome": {"outcome": decision}})
            await self._updates.put({
                "type": "permission_request",
                "decision": decision,
                "raw": params,
            })
            return

        if method == "session/update":
            for norm in normalize_acp_update(params):
                await self._updates.put(norm)
            return

        # Ignore other notifications / unknown methods

    async def _decide_permission(self, params: dict[str, Any]) -> str:
        # Map allow/deny onto ACP outcome strings used by Grok (allow | reject)
        if self.always_approve or self.permission_handler is None:
            return "allow"
        result = self.permission_handler(params)
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            result = await result  # type: ignore[assignment]
        return "allow" if result == "allow" else "reject"

    async def _respond(self, req_id: Any, result: dict[str, Any]) -> None:
        if self._writer is None:
            return
        msg = {"jsonrpc": "2.0", "id": req_id, "result": result}
        payload = (json.dumps(msg) + "\n").encode()
        self._writer.write(payload)  # type: ignore[union-attr]
        await self._writer.drain()  # type: ignore[union-attr]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        if self._writer is not None and hasattr(self._writer, "close"):
            with contextlib.suppress(Exception):
                self._writer.close()  # type: ignore[union-attr]
        if self._kill is not None:
            self._kill()
        if self._proc is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=5)
        self._reader = None
        self._writer = None
        self._proc = None
