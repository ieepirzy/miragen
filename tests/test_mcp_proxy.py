"""`miragen-hook mcp-proxy`: stdio JSON-RPC ↔ the bridge's Streamable HTTP
/mcp, resolving URL + token with the hooks' chain — plus the Codex context
cap and the token-file fallthrough the proxy shares with the hooks."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from miragen_hook import harness_setup as hs
from miragen_hook import mcp_proxy
from miragen_hook.client import (
    CODEX_CONTEXT_CAP_BYTES,
    cap_context_bytes,
    read_token,
    run,
)

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "miragen-memory"


class _Bridge(BaseHTTPRequestHandler):
    """A bridge double: answers initialize as JSON with a session id, tools/list
    as SSE, notifications with 202, `fail` with 500; records headers."""

    seen: list = []  # noqa: RUF012 — reset per test by the fixture

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        method = body.get("method")
        if "id" not in body:
            self.send_response(202)
            self.end_headers()
            return
        if method == "fail":
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"boom")
            return
        if method == "tools/list":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            msg = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "memory_read"}]}}
            self.wfile.write(b"event: message\ndata: " + json.dumps(msg).encode() + b"\n\n")
            return
        data = json.dumps({"jsonrpc": "2.0", "id": body["id"],
                           "result": {"protocolVersion": "2025-06-18", "echo": method}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("mcp-session-id", "sess-1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def bridge():
    _Bridge.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _Bridge.seen
    server.shutdown()


def _lines(*messages) -> bytes:
    return b"".join(json.dumps(m).encode() + b"\n" for m in messages)


@pytest.mark.parametrize("root", [ROOT, PLUGIN], ids=["package", "vendored"])
def test_the_proxy_speaks_both_answer_shapes(bridge, tmp_path, root):
    """Run exactly as the harness configs do: by file path (the daemon's
    adapter copy is the package; the plugin manifests run the vendored one)."""
    url, seen = bridge
    token = tmp_path / "tok"
    token.write_text("s3cret\n")
    stdin = _lines(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "fail"},
    )
    ran = subprocess.run(
        [sys.executable, str(root / "miragen_hook" / "__main__.py"), "mcp-proxy", "--harness",
         "grok-build", "--daemon", url, "--token-file", str(token), "--no-setup"],
        input=stdin, capture_output=True, timeout=30, check=False, cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GROK_SESSION_ID": "g-42"},
    )
    answers = {m["id"]: m for m in map(json.loads, ran.stdout.decode().splitlines())}
    assert set(answers) == {0, 1, 2}, ran.stderr  # nothing for the notification, and stdout is JSON only
    assert answers[0]["result"]["echo"] == "initialize"
    assert answers[1]["result"]["tools"] == [{"name": "memory_read"}]  # relayed from SSE
    assert answers[2]["error"]["code"] == -32000 and "500" in answers[2]["error"]["message"]
    assert {s["path"] for s in seen} == {"/mcp"}
    for request in seen:
        headers = {k.lower(): v for k, v in request["headers"].items()}
        assert headers["authorization"] == "Bearer s3cret"
        assert headers["x-harness-session"] == "grok-build:g-42"
        assert "text/event-stream" in headers["accept"] and "application/json" in headers["accept"]
    later = [{k.lower(): v for k, v in s["headers"].items()} for s in seen[1:]]
    assert all(h.get("mcp-session-id") == "sess-1" for h in later)
    assert all(h.get("mcp-protocol-version") == "2025-06-18" for h in later)


def test_an_unreachable_bridge_answers_every_request():
    out = io.StringIO()

    def refused(request, timeout):
        raise urllib.error.URLError("refused")

    proxy = mcp_proxy.Proxy("http://127.0.0.1:1", None, out=out, opener=refused)
    proxy.run(io.BytesIO(_lines({"jsonrpc": "2.0", "id": 7, "method": "tools/call"},
                                {"jsonrpc": "2.0", "method": "notifications/x"})))
    [answer] = [json.loads(line) for line in out.getvalue().splitlines()]
    assert answer["id"] == 7 and "unreachable" in answer["error"]["message"]


def test_codex_gets_no_session_header():
    assert mcp_proxy.harness_session("codex", {"GROK_SESSION_ID": "x"}) is None
    assert mcp_proxy.harness_session("grok-build", {"GROK_SESSION_ID": "x"}) == "grok-build:x"


class TestResolution:
    def test_a_daemon_record_stands_in_for_the_baked_arguments(self, tmp_path):
        home = tmp_path / ".grok"
        home.mkdir()
        (tmp_path / "tok").write_text("from-file")
        hs.ensure_grok(home, url="https://daemon.example", token_file=str(tmp_path / "tok"))
        env = {"GROK_HOME": str(home), "MIRAGEND_URL": "https://env.example", "MIRAGEND_TOKEN": "env"}
        assert mcp_proxy.resolve("grok-build", None, None, env)[:2] == ("https://daemon.example", "from-file")
        # an explicit argument still wins
        assert mcp_proxy.resolve("grok-build", "https://arg.example", None, env)[0] == "https://arg.example"

    def test_a_plugin_record_does_not_pin_the_url(self, tmp_path):
        home = tmp_path / ".grok"
        home.mkdir()
        hs.ensure_grok(home, url="https://old.example", managed_by=hs.MANAGED_BY_PLUGIN)
        env = {"GROK_HOME": str(home), "MIRAGEND_URL": "https://env.example"}
        assert mcp_proxy.resolve("grok-build", None, None, env)[0] == "https://env.example"

    def test_codex_record_is_read_from_codex_home(self, tmp_path):
        home = tmp_path / ".codex"
        home.mkdir()
        hs.ensure_codex(home, url="https://daemon.example")
        assert mcp_proxy.resolve("codex", None, None, {"HOME": str(tmp_path)})[0] == "https://daemon.example"

    def test_secondary_grok_setup_never_overwrites_a_daemons(self, tmp_path):
        home = tmp_path / ".grok"
        home.mkdir()
        hs.ensure_grok(home, url="https://daemon.example")
        mcp_proxy._secondary_grok_setup("https://plugin.example", None, {"GROK_HOME": str(home)})
        assert hs.read_setup_record(home)["url"] == "https://daemon.example"
        other = tmp_path / "g2"
        other.mkdir()
        mcp_proxy._secondary_grok_setup("https://plugin.example", None, {"GROK_HOME": str(other)})
        assert hs.read_setup_record(other) == {"url": "https://plugin.example", "token_file": None,
                                               "managed_by": "plugin"}


class TestTokenAndCap:
    def test_a_missing_token_file_falls_through_to_the_environment(self, tmp_path):
        assert read_token(str(tmp_path / "missing"), {"MIRAGEND_TOKEN": "env"}) == "env"
        (tmp_path / "empty").write_text("\n")
        assert read_token(str(tmp_path / "empty"), {"MIRAGEND_TOKEN": "env"}) == "env"
        (tmp_path / "t").write_text("file\n")
        assert read_token(str(tmp_path / "t"), {"MIRAGEND_TOKEN": "env"}) == "file"

    def test_cap_counts_bytes_and_keeps_the_head(self):
        text = "[miragen session] store_run=r-1\n" + "ä" * 20_000
        capped = cap_context_bytes(text, CODEX_CONTEXT_CAP_BYTES)
        assert len(capped.encode()) <= CODEX_CONTEXT_CAP_BYTES
        assert capped.startswith("[miragen session] store_run=r-1") and "truncated" in capped
        assert cap_context_bytes("short", CODEX_CONTEXT_CAP_BYTES) == "short"
        # below Codex's spill threshold for our entries (bytes/4 ≤ limit)
        assert CODEX_CONTEXT_CAP_BYTES / 4 <= hs.CODEX_CONTEXT_TOKEN_LIMIT

    @pytest.mark.parametrize("harness,capped", [("codex", True), ("claude-code", False)])
    def test_only_codex_context_is_capped(self, harness, capped):
        big = "x" * (CODEX_CONTEXT_CAP_BYTES * 2)

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        out = run(harness, {"hook_event_name": "SessionStart", "session_id": "s", "source": "startup"},
                  daemon_url="http://d", token=None, pid=1,
                  opener=lambda req, timeout: R(json.dumps({"context": big}).encode()))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert (len(context.encode()) <= CODEX_CONTEXT_CAP_BYTES) is capped
