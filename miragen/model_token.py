"""The workload's model token: how a spoke agent calls models (MiraRun
ADR-045 §5, decision 3).

The control plane mints a short-lived token for the workload, and the spoke
hands it over on a turn (`credentials.model`) and again on every refresh
(`PUT /credentials/model`). The agent calls models only through the
deployment's provider gateway, with that token. There is no fallback: with no
token, or an expired one, model calls fail.

Where it lives:
- **In memory**, in this process's `ModelTokenHolder`.
- **In one file**, `<MIRAGEN_MODEL_TOKEN_DIR>/model-token`, mode 0600. The
  directory is an in-memory volume the spoke mounts, so the token never
  reaches the image, the workspace or a disk. The file exists only so a
  harness's credential helper can read the current token: Claude Code's
  `apiKeyHelper` runs `helper_command()`, which prints the token, or exits
  non-zero when there is none or it has expired. A failing helper makes
  Claude Code's requests fail rather than fall back
  (code.claude.com/docs/en/authentication, "Credential management").

It is never logged, never in a repr, never written to a run record, and it
is cleared when the workload ends (`clear()`, from the app's shutdown).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

TOKEN_DIR_ENV = "MIRAGEN_MODEL_TOKEN_DIR"
TOKEN_FILE = "model-token"
_MAX_TOKEN = 8192

# Run by the harness's credential helper: no imports beyond the standard
# library, no output but the token, exit 1 when there is none to give.
_HELPER = (
    "import json,sys,datetime as d\n"
    "try:\n"
    "    c=json.load(open(sys.argv[1]))\n"
    "    e=d.datetime.fromisoformat(c['expires_at'])\n"
    "    assert e>d.datetime.now(d.timezone.utc)\n"
    "except Exception:\n"
    "    sys.stderr.write('no current model token\\n'); sys.exit(1)\n"
    "sys.stdout.write(c['token'])\n"
)


class ModelTokenInvalid(ValueError):
    """The handed-over credential is malformed (the message never holds it)."""


@dataclass(frozen=True)
class ModelToken:
    env: str
    token: str = field(repr=False)
    expires_at: datetime

    @classmethod
    def parse(cls, value: object) -> ModelToken:
        if not isinstance(value, dict):
            raise ModelTokenInvalid("credentials.model must be an object")
        env, token, expires = value.get("env"), value.get("token"), value.get("expires_at")
        if not isinstance(env, str) or not env.replace("_", "").isalnum() or not env.isupper():
            raise ModelTokenInvalid("credentials.model.env is not a variable name")
        if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN:
            raise ModelTokenInvalid("credentials.model.token is missing or too long")
        try:
            expires_at = datetime.fromisoformat(str(expires))
        except ValueError as exc:
            raise ModelTokenInvalid("credentials.model.expires_at is not a timestamp") from exc
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return cls(env=env, token=token, expires_at=expires_at)

    def expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or datetime.now(UTC))


_holder: ModelTokenHolder | None = None


def holder() -> ModelTokenHolder:
    """The process's holder (the app hands tokens to it, harnesses read it)."""
    global _holder
    if _holder is None:
        _holder = ModelTokenHolder.from_env()
    return _holder


class ModelTokenHolder:
    """The process's one current model token, and its 0600 file."""

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._current: ModelToken | None = None

    @classmethod
    def from_env(cls) -> ModelTokenHolder:
        value = os.environ.get(TOKEN_DIR_ENV)
        return cls(Path(value) if value else None)

    @property
    def path(self) -> Path | None:
        return self._directory / TOKEN_FILE if self._directory is not None else None

    def set(self, token: ModelToken) -> None:
        """Take a newer token. An older one than held is ignored: a refresh
        and a turn can race."""
        with self._lock:
            if self._current is not None and token.expires_at < self._current.expires_at:
                return
            self._current = token
            self._write(token)

    def current(self) -> ModelToken | None:
        with self._lock:
            if self._current is not None and self._current.expired():
                self._clear_locked()
            return self._current

    def clear(self) -> None:
        with self._lock:
            self._clear_locked()

    def helper_command(self) -> str | None:
        """The command a credential helper runs, or None when this process
        has nowhere to keep the token (then nothing may call models)."""
        if self.path is None:
            return None
        return " ".join(shlex.quote(part) for part in
                        (sys.executable, "-I", "-c", _HELPER, str(self.path)))

    # -- the file ----------------------------------------------------------
    def _write(self, token: ModelToken) -> None:
        path = self.path
        if path is None:
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f".{TOKEN_FILE}.{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps({"token": token.token,
                                     "expires_at": token.expires_at.isoformat()}).encode())
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def _clear_locked(self) -> None:
        self._current = None
        path = self.path
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
