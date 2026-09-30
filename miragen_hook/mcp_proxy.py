"""`miragen-hook mcp-proxy`: the bridge MCP server as a stdio process.

Harness MCP configs cannot all say where the bridge is: Codex plugin MCP
has no variable expansion and starts stdio servers with a cleared
environment, Grok cannot expand Claude's plugin options. A stdio proxy
resolves the daemon URL and token with the SAME chain the hooks use, so
tools and hooks always reach the same daemon:

  --daemon / --token-file  (daemon-written Codex config bakes them)
  → the harness setup record the daemon wrote (<home>/miragen-adapter/setup.json)
  → MIRAGEND_URL / MIRAGEND_TOKEN → the option saved in Claude Code
  → the plugin manifest default.

Wire: newline-delimited JSON-RPC on stdin/stdout ↔ Streamable HTTP POSTs to
`<url>/mcp` (JSON or SSE answers; `mcp-session-id` kept when the server
issues one). Every request with an id gets an answer — a JSON-RPC error when
the bridge cannot be reached — so the harness never waits on a lost call.
stdout carries JSON-RPC only; diagnostics go to stderr.

Under Grok the proxy also names the session on the connection
(`X-Harness-Session: grok-build:<GROK_SESSION_ID>`) and, on a machine where
no daemon manages the Grok hooks, writes them (`harness_setup.ensure_grok`,
the secondary path: effective from the next session).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from miragen_hook import ADAPTER_VERSION

MCP_PATH = "/mcp"
REQUEST_TIMEOUT_S = 300.0  # tool calls (recall, store writes) — far above any hook bound
SESSION_HEADER = "X-Harness-Session"
_ACCEPT = "application/json, text/event-stream"


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, (parts.hostname or "").lower(), port


def _log(message: str) -> None:
    print(f"miragen-hook mcp-proxy: {message}", file=sys.stderr, flush=True)


def harness_session(harness: str | None, environ: dict | None = None) -> str | None:
    env = os.environ if environ is None else environ
    if harness == "grok-build":
        if env.get("GROK_SESSION_ID"):
            return f"grok-build:{env['GROK_SESSION_ID']}"
        # Grok < 1.0.41 (0.2.114, verified) passes its stdio MCP servers no
        # GROK_SESSION_ID: the tools still work, unbound to the session.
        _log("no GROK_SESSION_ID in the environment (Grok Build < 1.0.41?): tool calls are "
             "not bound to this session — omitted project/session mean the default project")
    return None


def managed_record(harness: str | None, environ: dict | None = None) -> dict:
    """The setup record for this harness, if the daemon or `setup` CLI wrote it (a
    record the plugin path wrote only mirrors this chain — honouring it
    would pin a URL the environment has since changed)."""
    from miragen_hook.harness_setup import (
        AUTHORITATIVE,
        codex_home,
        grok_home,
        read_setup_record,
    )

    if harness == "grok-build":
        record = read_setup_record(grok_home(environ))
    elif harness == "codex":
        record = read_setup_record(codex_home(environ))
    else:
        return {}
    return record if record.get("managed_by") in AUTHORITATIVE and record.get("url") else {}


def resolve(harness: str | None, daemon: str | None, token_file: str | None,
            environ: dict | None = None) -> tuple[str, str | None, str | None]:
    """(url, token, token_file) by the hooks' chain, with the daemon's setup
    record standing in for the arguments the daemon baked into the hooks."""
    from miragen_hook.client import read_token, resolve_daemon_url

    if not daemon:
        record = managed_record(harness, environ)
        if record:
            daemon = record["url"]
            token_file = token_file or record.get("token_file")
    url = resolve_daemon_url(daemon, environ)
    return url, read_token(token_file, environ), token_file


class Proxy:
    def __init__(self, url: str, token: str | None, *, session: str | None = None,
                 out=None, opener: Callable | None = None, timeout: float = REQUEST_TIMEOUT_S):
        self.endpoint = url.rstrip("/") + MCP_PATH
        self.token = token
        self.session = session
        self.out = out or sys.stdout
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.mcp_session_id: str | None = None
        self.protocol_version: str | None = None
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    # -- output ---------------------------------------------------------------

    def emit(self, message: Any) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self.out.write(line)
            self.out.flush()

    def _errors_for(self, message: Any, text: str) -> None:
        items = message if isinstance(message, list) else [message]
        for item in items:
            if isinstance(item, dict) and "id" in item and "method" in item:
                self.emit({"jsonrpc": "2.0", "id": item["id"],
                           "error": {"code": -32000, "message": text}})

    # -- transport ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": _ACCEPT,
                   "User-Agent": ADAPTER_VERSION}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session:
            headers[SESSION_HEADER] = self.session
        if self.mcp_session_id:
            headers["mcp-session-id"] = self.mcp_session_id
        if self.protocol_version:
            headers["mcp-protocol-version"] = self.protocol_version
        return headers

    def _post(self, data: bytes, url: str | None = None, hops: int = 0):
        request = urllib.request.Request(url or self.endpoint, data=data,
                                         headers=self._headers(), method="POST")
        try:
            return self.opener(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in (307, 308) and location and hops < 2:
                target = urllib.request.urljoin(request.full_url, location)
                # Same origin only: the bearer must never follow a redirect
                # to another host (or a scheme/port downgrade).
                if _origin(target) == _origin(self.endpoint):
                    return self._post(data, target, hops + 1)
                _log(f"refusing a redirect to another origin ({_origin(target)})")
            raise

    def forward(self, message: Any) -> None:
        data = json.dumps(message).encode()
        try:
            response = self._post(data)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read()[:300].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001, S110 — the status line is enough
                pass
            if exc.code == 404 and self.mcp_session_id:
                self.mcp_session_id = None  # the server forgot us; next initialize starts over
            _log(f"bridge answered HTTP {exc.code}: {detail}")
            self._errors_for(message, f"miragen bridge answered HTTP {exc.code}: {detail}".strip())
            return
        except Exception as exc:  # noqa: BLE001 — URLError, timeouts, refused
            _log(f"bridge unreachable at {self.endpoint}: {exc}")
            self._errors_for(message, f"miragen bridge unreachable at {self.endpoint}: {exc}")
            return
        with response:
            session_id = response.headers.get("mcp-session-id")
            if session_id:
                self.mcp_session_id = session_id
            ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype == "text/event-stream":
                self._relay_sse(response, message)
                return
            body = response.read()
        self._note_protocol(message, body)
        if body.strip():
            try:
                self.emit(json.loads(body))
            except ValueError:
                self._errors_for(message, "miragen bridge answered non-JSON")

    def _note_protocol(self, message: Any, body: bytes) -> None:
        if isinstance(message, dict) and message.get("method") == "initialize":
            try:
                version = json.loads(body).get("result", {}).get("protocolVersion")
            except (ValueError, AttributeError):
                version = None
            if isinstance(version, str):
                self.protocol_version = version

    def _relay_sse(self, response, message: Any) -> None:
        answered = False
        data_lines: list[str] = []

        def dispatch() -> None:
            nonlocal answered
            if not data_lines:
                return
            payload = "\n".join(data_lines)
            data_lines.clear()
            try:
                parsed = json.loads(payload)
            except ValueError:
                return
            self._note_protocol(message, payload.encode())
            self.emit(parsed)
            answered = True

        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                dispatch()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" ") if line[5:6] == " " else line[5:])
        dispatch()
        if not answered:
            self._errors_for(message, "miragen bridge closed the stream without an answer")

    # -- loop -----------------------------------------------------------------

    def handle_line(self, line: bytes | str) -> None:
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
        text = text.strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except ValueError:
            self.emit({"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32700, "message": "parse error"}})
            return
        if isinstance(message, dict) and message.get("method") == "initialize":
            # Everything after initialize needs its answer (and session id):
            # the client waits for it anyway, so run it inline.
            self.forward(message)
            return
        thread = threading.Thread(target=self.forward, args=(message,), daemon=True)
        thread.start()
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]

    def run(self, stdin=None) -> None:
        stream = stdin or sys.stdin.buffer
        for line in stream:
            self.handle_line(line)
        for thread in self._threads:
            thread.join(timeout=5)
        self.close()

    def close(self) -> None:
        if not self.mcp_session_id:
            return
        request = urllib.request.Request(self.endpoint, headers=self._headers(), method="DELETE")
        try:
            with self.opener(request, timeout=2):
                pass
        except Exception:  # noqa: BLE001, S110 — best effort
            pass


def _secondary_grok_setup(url: str, token_file: str | None, environ: dict | None = None) -> None:
    """No daemon manages this machine's Grok hooks: write them from here
    (they take effect from the next session). Never over a daemon's setup."""
    from miragen_hook.harness_setup import (
        AUTHORITATIVE,
        MANAGED_BY_PLUGIN,
        ensure_grok,
        grok_home,
        read_setup_record,
    )

    home = grok_home(environ)
    if read_setup_record(home).get("managed_by") in AUTHORITATIVE:
        return
    try:
        status = ensure_grok(home, url=url, token_file=token_file, managed_by=MANAGED_BY_PLUGIN,
                             environ=environ)
    except Exception as exc:  # noqa: BLE001 — the proxy must still serve tools
        _log(f"could not write the Grok hooks ({exc})")
        return
    if status.get("changed"):
        _log(f"wrote Grok hooks: {', '.join(status['changed'])}")


def main(args) -> int:
    url, token, token_file = resolve(args.harness, args.daemon, args.token_file)
    if args.harness == "grok-build" and not args.no_setup:
        _secondary_grok_setup(url, token_file)
    Proxy(url, token, session=harness_session(args.harness)).run()
    return 0


def add_parser(sub) -> None:
    proxy = sub.add_parser("mcp-proxy", help="serve the bridge MCP over stdio (proxy to <url>/mcp)")
    proxy.add_argument("--harness", default=None, choices=("claude-code", "codex", "grok-build"))
    proxy.add_argument("--daemon", default=None, help="miragend base URL")
    proxy.add_argument("--token-file", default=None)
    proxy.add_argument("--no-setup", action="store_true",
                       help="never write harness hooks from here (the Grok secondary path)")


__all__ = ["Proxy", "add_parser", "harness_session", "main", "resolve"]
