"""The pod's own ServiceAccount token, kept current (miragen#224).

A projected ServiceAccount token rotates: the kubelet rewrites the file
well before expiry. Reading it once at start-up and baking it into a
header works for exactly the token's lifetime, then every API call is a
401. So, as client-go does, the token is re-read from its file: at most a
minute after the last read, at once when the file changes, and once more
on a 401 before the request is retried.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path

import httpx

INCLUSTER_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

# client-go's cached file token source re-reads every minute.
REREAD_SECONDS = 60.0


class ServiceAccountTokenAuth(httpx.Auth):
    # A 401 is retried once, so the body must be replayable.
    requires_request_body = True

    def __init__(self, path: Path, *, reread_seconds: float = REREAD_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._path = path
        self._reread_seconds = reread_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._token = ""
        self._mtime_ns: int | None = None
        self._read_at = 0.0
        self._load()

    def _load(self) -> None:
        # The kubelet swaps the file atomically (a `..data` symlink), so a
        # read never sees half a token.
        mtime_ns = self._path.stat().st_mtime_ns
        token = self._path.read_text().strip()
        if not token:
            raise OSError(f"{self._path} is empty")
        self._token, self._mtime_ns, self._read_at = token, mtime_ns, self._monotonic()

    def token(self, *, force: bool = False) -> str:
        with self._lock:
            try:
                stale = force or self._monotonic() - self._read_at >= self._reread_seconds
                if stale or self._path.stat().st_mtime_ns != self._mtime_ns:
                    self._load()
            except OSError:
                # A transient read failure keeps the last good token; the API
                # server's answer says whether it still works.
                pass
            return self._token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        sent = self.token()
        request.headers["Authorization"] = f"Bearer {sent}"
        response = yield request
        if response.status_code == 401:
            fresh = self.token(force=True)
            if fresh != sent:
                request.headers["Authorization"] = f"Bearer {fresh}"
                yield request
