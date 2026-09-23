"""Disposable, bounded Python source inspection. Never writes memory or a baseline.

Exact qualified names are identities. Source bytes (including decorators and
comments within a symbol) are content evidence. No call-graph or rename inference.
"""

from __future__ import annotations

import ast
import hashlib
import json
import socket
import os
import stat
import subprocess
import selectors
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_RESOURCES = 20
MAX_FILE_BYTES = 512_000
MAX_CHECKOUT_BYTES = 8_000_000
VERIFIER = "miragen-python-source/1"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resource_key(resource: dict) -> str:
    return digest(json.dumps(resource, sort_keys=True, separators=(",", ":")).encode())


def _git(root: Path, *args: str) -> bytes:
    command = ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args]
    # Cap output while receiving it, not after allocating an arbitrarily large
    # dirty diff. No inherited stdin, pager, text converter or fsmonitor helper.
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    result = bytearray()
    deadline = time.monotonic() + 5
    try:
        with selectors.DefaultSelector() as reader:
            reader.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not reader.select(remaining):
                    raise subprocess.TimeoutExpired(command, 5)
                chunk = os.read(process.stdout.fileno(), 65_536)
                if not chunk:
                    break
                result.extend(chunk)
                if len(result) > MAX_CHECKOUT_BYTES:
                    raise ValueError("checkout inspection byte limit exceeded")
        status = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if status:
            raise subprocess.CalledProcessError(status, command)
        return bytes(result)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def _read(root: Path, path: str) -> bytes:
    relative = Path(path)
    if (
        relative.is_absolute()
        or any(p in ("", ".", "..") for p in path.split("/"))
        or "\\" in path
    ):
        raise ValueError("expected a canonical relative path")
    # Walk through anchored directory descriptors. O_NOFOLLOW on each step
    # also rejects a symlink swapped in between validation and opening.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("source must be a regular file")
            data = source.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(directory)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("source file byte limit exceeded")
    return data


def snapshot(root: Path) -> dict:
    from miragen.daemon.sessions.projects import normalize_remote

    actual = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if actual != root:
        raise ValueError("checkout must be its repository root")
    try:
        repository = normalize_remote(
            _git(root, "remote", "get-url", "origin").decode().strip()
        )
    except subprocess.CalledProcessError:
        repository = f"path:{root}"
    revision = _git(root, "rev-parse", "HEAD").decode().strip()
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    gitdir = _git(root, "rev-parse", "--absolute-git-dir").decode().strip()
    delta = _git(root, "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(
        b"\0"
    )
    total = len(delta)
    hasher = hashlib.sha256(delta)
    for name in sorted(n for n in untracked if n):
        data = _read(root, name.decode())
        total += len(data)
        if total > MAX_CHECKOUT_BYTES:
            raise ValueError("dirty checkout byte limit exceeded")
        hasher.update(name + b"\0" + bytes.fromhex(digest(data)))
    return {
        "repository": repository,
        "checkout_id": digest(f"{socket.gethostname()}\0{root}\0{gitdir}".encode()),
        "branch": branch,
        "source_revision": revision,
        "dirty_digest": hasher.hexdigest(),
    }


def _symbols(data: bytes) -> dict[str, list[bytes]]:
    tree = ast.parse(data)
    lines = data.splitlines(keepends=True)
    result: dict[str, list[bytes]] = {}

    class Visitor(ast.NodeVisitor):
        stack: list[str] = []

        def declaration(self, node):
            self.stack.append(node.name)
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            source = b"".join(lines[start - 1 : node.end_lineno])
            result.setdefault(".".join(self.stack), []).append(source)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = declaration
        visit_AsyncFunctionDef = declaration
        visit_ClassDef = declaration

    Visitor().visit(tree)
    return result


def inspect_resources(checkout: str | Path, locators: list[dict]) -> list[dict]:
    """One consistent bounded inspection; failure is unknown, never verified.

    locators are explicit path/symbol pairs, optionally carrying repository and
    kind from a stored grounding. Repeated declarations remain ambiguous.
    """
    if not 1 <= len(locators) <= MAX_RESOURCES:
        raise ValueError("inspect between 1 and 20 resources")
    root = Path(checkout).resolve()
    before, failure = None, None
    try:
        before = snapshot(root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        failure = type(exc).__name__ + ": source snapshot unavailable"
    observations, files = [], {}
    for locator in locators:
        resource = {
            "kind": locator.get("kind")
            or ("python_symbol" if locator.get("symbol") else "python_file"),
            "repository": locator.get("repository")
            or (before or {}).get("repository", f"path:{root}"),
            "path": locator["path"],
            "symbol": locator.get("symbol"),
        }
        row = {
            "resource": resource,
            "snapshot": before,
            "outcome": "unverified",
            "content_digest": None,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "verifier": VERIFIER,
            "limitations": "Exact source bytes only; no runtime or semantic assertion verification.",
        }
        if failure or resource["repository"] != (before or {}).get("repository"):
            row["snapshot"] = None
            row["limitations"] += " " + (failure or "Repository identity mismatch.")
            observations.append(row)
            continue
        try:
            data = _read(root, resource["path"])
            files[resource["path"]] = data
            if not resource["path"].endswith(".py"):
                raise ValueError("Python adapter requires a .py source")
            symbols = _symbols(data)
            matches = (
                symbols.get(resource["symbol"], []) if resource["symbol"] else [data]
            )
            row["outcome"] = (
                "present"
                if len(matches) == 1
                else "ambiguous"
                if matches
                else "missing"
            )
            if len(matches) == 1:
                row["content_digest"] = digest(matches[0])
        except FileNotFoundError:
            row["outcome"] = "missing"
        except (OSError, ValueError, SyntaxError, RecursionError):
            row["limitations"] += " Source cannot be safely parsed or inspected."
        observations.append(row)
    if before:
        try:
            stable = before == snapshot(root) and all(
                _read(root, p) == data for p, data in files.items()
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            stable = False
        if not stable:
            for row in observations:
                row.update(outcome="unverified", content_digest=None)
                row["limitations"] += " Checkout changed during inspection."
    return observations
