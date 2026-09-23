"""miragend's harness setup loop: the daemon (not a person, not a trust
prompt) keeps Grok Build and Codex joined to the memory bridge on this
machine. The file-level work is the stdlib adapter's
(`miragen_hook.harness_setup`); this module decides whether to run it,
runs it at startup and every `interval_s`, logs what it wrote, and reports
per-harness status on /health.

Gate: explicit `enabled` wins; otherwise on only when a URL is configured
(never a guessed loopback — hooks and tools reporting to a daemon nobody
chose is the split-memory failure) and the daemon is not in a container
(the hosted VPS daemon). Each harness is then skipped while its home
(`$GROK_HOME`/`$CODEX_HOME`, default ~/.grok, ~/.codex — from THIS
process's environment: a systemd unit must carry a relocated home) does
not exist.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from miragen.daemon.sessions.config import HarnessSetup as HarnessSetupConfig

logger = logging.getLogger(__name__)

_FALSE = ("off", "0", "false", "no")
_TRUE = ("on", "1", "true", "yes")


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


@dataclass
class ResolvedHarnessSetup:
    enabled: bool
    reason: str
    url: str | None
    token_file: str | None
    interval_s: int


def resolve_harness_setup(
    config: HarnessSetupConfig | None, environ: dict | None = None,
    *, in_container: Callable[[], bool] = _in_container,
) -> ResolvedHarnessSetup:
    env = os.environ if environ is None else environ
    config = config or HarnessSetupConfig()
    url = env.get("MIRAGEND_HARNESS_SETUP_URL") or config.url
    token_file = env.get("MIRAGEND_HARNESS_SETUP_TOKEN_FILE") or (
        str(Path(config.token_file).expanduser()) if config.token_file else None
    )
    interval = int(env.get("MIRAGEND_HARNESS_SETUP_INTERVAL_S") or config.interval_s)
    switch = (env.get("MIRAGEND_HARNESS_SETUP") or "").strip().lower()
    explicit = False if switch in _FALSE else True if switch in _TRUE else config.enabled

    if explicit is False:
        return ResolvedHarnessSetup(False, "disabled by configuration", url, token_file, interval)
    if not url:
        return ResolvedHarnessSetup(
            False, "no harness_setup.url (or MIRAGEND_HARNESS_SETUP_URL): nothing to point "
                   "the harnesses at", None, token_file, interval)
    if "$" in url or (token_file and "$" in token_file):
        return ResolvedHarnessSetup(False, "a '$' in the URL or token file would disable the "
                                           "Grok hooks", url, token_file, interval)
    if explicit is None and in_container():
        return ResolvedHarnessSetup(False, "running in a container (the hosted daemon); set "
                                           "harness_setup.enabled to override", url, token_file,
                                    interval)
    return ResolvedHarnessSetup(True, "enabled", url, token_file, max(30, interval))


@dataclass
class HarnessStatus:
    installed: bool = False
    home: str | None = None
    binary: str | None = None
    current: bool = False
    last_run_at: float | None = None
    last_changed_at: float | None = None
    last_changed: list[str] = field(default_factory=list)
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class HarnessSetupService:
    """Runs `ensure_grok` + `ensure_codex` (in a worker thread: file I/O)
    at startup and every interval."""

    def __init__(self, resolved: ResolvedHarnessSetup, environ: dict | None = None,
                 *, ensure: dict[str, Callable[..., dict]] | None = None):
        from miragen_hook import harness_setup

        self.resolved = resolved
        self.environ = dict(os.environ if environ is None else environ)
        self._ensure = ensure or {
            "grok-build": lambda: harness_setup.ensure_grok(
                harness_setup.grok_home(self.environ), url=self.resolved.url,
                token_file=self.resolved.token_file, environ=self.environ),
            "codex": lambda: harness_setup.ensure_codex(
                harness_setup.codex_home(self.environ), url=self.resolved.url,
                token_file=self.resolved.token_file, environ=self.environ),
        }
        self.status = {name: HarnessStatus() for name in self._ensure}
        self.runs = 0
        self._task: asyncio.Task | None = None

    def run_once(self) -> dict[str, dict]:
        if not self.resolved.enabled:
            return self.describe()["harnesses"]
        for name, ensure in self._ensure.items():
            status = self.status[name]
            status.last_run_at = time.time()
            try:
                result = ensure()
            except Exception as exc:  # noqa: BLE001 — one harness never stops the other
                status.last_error = f"{type(exc).__name__}: {exc}"
                status.current = False
                logger.warning(f"harness setup ({name}): {status.last_error}")
                continue
            status.installed = bool(result.get("installed"))
            status.home = result.get("home")
            status.binary = result.get("binary")
            status.current = bool(result.get("current"))
            status.last_error = None
            changed = list(result.get("changed") or []) + list(result.get("pruned") or [])
            if changed:
                status.last_changed = changed
                status.last_changed_at = status.last_run_at
                logger.info(f"harness setup ({name}): wrote {', '.join(changed)}")
        self.runs += 1  # completed runs
        return self.describe()["harnesses"]

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.run_once)
            except Exception:  # pragma: no cover — run_once already contains failures
                logger.exception("harness setup: unexpected failure")
            await asyncio.sleep(self.resolved.interval_s)

    async def start(self) -> None:
        if not self.resolved.enabled:
            logger.info(f"harness setup off: {self.resolved.reason}")
            return
        logger.info(f"harness setup on: sessions report to {self.resolved.url} "
                    f"(token file: {self.resolved.token_file or 'none'}), every "
                    f"{self.resolved.interval_s}s")
        self._task = asyncio.create_task(self._loop(), name="miragend-harness-setup")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 — shutting down
                pass
            self._task = None

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.resolved.enabled,
            "reason": self.resolved.reason,
            "url": self.resolved.url,
            "token_file": self.resolved.token_file,
            "interval_s": self.resolved.interval_s,
            "runs": self.runs,
            "harnesses": {name: status.to_dict() for name, status in self.status.items()},
        }
