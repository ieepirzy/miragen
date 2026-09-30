"""The stdlib adapter runs on the oldest Python a harness machine may have:
3.10 for the hooks, the MCP proxy and the Grok setup (the Codex setup needs
tomllib, 3.11+, and degrades to a reported SetupError). A 3.11-only import
at module level would kill EVERY Claude/Codex/Grok hook with ImportError.

Always: an AST check of the vendored sources against Python 3.10 grammar and
3.11-only module-level imports. When an old interpreter is available
(MIRAGEN_OLDEST_PYTHON, or python3.10/python3.11 on PATH): the real thing."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "miragen-memory"
SOURCES = sorted((PLUGIN / "miragen_hook").glob("*.py"))
# Module-level imports that do not exist on 3.10.
_NEW_IN_311 = {("tomllib", None), ("datetime", "UTC"), ("typing", "Self"), ("enum", "StrEnum"),
               ("typing", "LiteralString"), ("typing", "Never"), ("typing", "assert_never")}


@pytest.mark.parametrize("path", SOURCES, ids=[p.name for p in SOURCES])
def test_sources_parse_as_python_310_and_import_nothing_newer(path):
    tree = ast.parse(path.read_text(), feature_version=(3, 10))
    for node in tree.body:  # module level only: lazy imports inside functions may degrade
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert (alias.name, None) not in _NEW_IN_311, f"{path.name}: import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert (node.module, alias.name) not in _NEW_IN_311, \
                    f"{path.name}: from {node.module} import {alias.name}"
    assert not any(isinstance(n, ast.TryStar) for n in ast.walk(tree))


def _oldest_python() -> str | None:
    explicit = os.environ.get("MIRAGEN_OLDEST_PYTHON")
    if explicit:
        return explicit
    for name in ("python3.10", "python3.11"):
        found = shutil.which(name)
        if found:
            return found
    return None


OLD = _oldest_python()
needs_old = pytest.mark.skipif(OLD is None, reason="no python3.10/3.11 (set MIRAGEN_OLDEST_PYTHON)")


class _Bridge(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/sessions/v1/events"):
            answer = {"context": "FLOOR-CANARY"}
        else:
            answer = {"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}}
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def bridge():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@needs_old
def test_the_vendored_adapter_runs_on_the_oldest_python(bridge, tmp_path):
    version = subprocess.run([OLD, "-c", "import sys; print(sys.version_info[:2])"],
                             capture_output=True, text=True, check=True).stdout.strip()
    env = {"PATH": f"{Path(OLD).parent}:/usr/bin:/bin", "HOME": str(tmp_path)}
    # the Claude Code hook command, verbatim
    hook = subprocess.run(
        [OLD, "-m", "miragen_hook", "claude-code", "--daemon", bridge],
        input=json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "source": "startup"}),
        capture_output=True, text=True, timeout=30, check=False, cwd=tmp_path,
        env={**env, "PYTHONPATH": str(PLUGIN)},
    )
    assert "FLOOR-CANARY" in hook.stdout, (version, hook.stderr)
    # the MCP proxy, by file path as the manifests run it
    proxy = subprocess.run(
        [OLD, str(PLUGIN / "miragen_hook" / "__main__.py"), "mcp-proxy", "--daemon", bridge, "--no-setup"],
        input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n', capture_output=True, text=True,
        timeout=30, check=False, cwd=tmp_path, env=env,
    )
    assert json.loads(proxy.stdout)["result"] == {"tools": []}, (version, proxy.stderr)
    # the Grok setup works; the Codex setup degrades to a reported error
    (tmp_path / ".grok").mkdir()
    (tmp_path / ".codex").mkdir()
    grok = subprocess.run([OLD, str(PLUGIN / "miragen_hook" / "__main__.py"), "setup", "grok-build",
                           "--daemon", bridge], capture_output=True, text=True, timeout=30,
                          check=False, cwd=tmp_path, env=env)
    assert grok.returncode == 0 and (tmp_path / ".grok" / "hooks" / "miragen.json").exists(), grok.stderr
    codex = subprocess.run([OLD, str(PLUGIN / "miragen_hook" / "__main__.py"), "setup", "codex",
                            "--daemon", bridge], capture_output=True, text=True, timeout=30,
                           check=False, cwd=tmp_path, env=env)
    if version.startswith("(3, 10"):
        assert codex.returncode == 2 and "3.11" in codex.stderr, codex.stderr
        assert not (tmp_path / ".codex" / "hooks.json").exists()  # nothing half-written
    else:
        assert codex.returncode == 0, codex.stderr
