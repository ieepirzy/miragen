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
import tempfile
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
    # Explicitly disabled: undo what THIS daemon wrote (a setup record with
    # managed_by=miragend), so hooks and proxies stop following it.
    remove: bool = False


# Not `…_TOKEN_FILE`: main() resolves every `*_FILE` variable into its
# plain counterpart (the secret-file convention) before this is read.
def resolve_harness_setup(
    config: HarnessSetupConfig | None, environ: dict | None = None,
    *, in_container: Callable[[], bool] = _in_container,
) -> ResolvedHarnessSetup:
    env = os.environ if environ is None else environ
    config = config or HarnessSetupConfig()
    url = env.get("MIRAGEND_HARNESS_SETUP_URL") or config.url
    token_file = env.get("MIRAGEND_HARNESS_SETUP_TOKEN_PATH") or (
        str(Path(config.token_file).expanduser()) if config.token_file else None
    )
    raw_interval = env.get("MIRAGEND_HARNESS_SETUP_INTERVAL_S")
    try:
        interval = int(raw_interval) if raw_interval else config.interval_s
    except ValueError:
        logger.warning(f"MIRAGEND_HARNESS_SETUP_INTERVAL_S={raw_interval!r} is not a number; "
                       f"using {config.interval_s}s")
        interval = config.interval_s
    switch = (env.get("MIRAGEND_HARNESS_SETUP") or "").strip().lower()
    explicit = False if switch in _FALSE else True if switch in _TRUE else config.enabled

    if explicit is False:
        return ResolvedHarnessSetup(False, "disabled by configuration (daemon-written setup removed)",
                                    url, token_file, interval, remove=True)
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
                 *, ensure: dict[str, Callable[..., dict]] | None = None,
                 remove: dict[str, Callable[..., dict]] | None = None):
        from miragen_hook import harness_setup

        self.resolved = resolved
        self.environ = dict(os.environ if environ is None else environ)
        # The adapter is snapshotted ONCE, now: a local miragend often runs
        # editable from a live checkout that other sessions switch branches
        # in — that must not reach the harness homes within an interval.
        self._source = None
        if resolved.enabled and ensure is None:
            self._snapshot_dir = tempfile.TemporaryDirectory(prefix="miragend-adapter-")
            self._source = harness_setup.snapshot_adapter(Path(self._snapshot_dir.name))
        self._ensure = ensure or {
            "grok-build": lambda: harness_setup.ensure_grok(
                harness_setup.grok_home(self.environ), url=self.resolved.url,
                token_file=self.resolved.token_file, environ=self.environ, source=self._source),
            "codex": lambda: harness_setup.ensure_codex(
                harness_setup.codex_home(self.environ), url=self.resolved.url,
                token_file=self.resolved.token_file, environ=self.environ, source=self._source),
        }
        homes = {"grok-build": lambda: harness_setup.grok_home(self.environ),
                 "codex": lambda: harness_setup.codex_home(self.environ)}
        removers = {"grok-build": harness_setup.remove_grok, "codex": harness_setup.remove_codex}

        def _remover(name):
            def run() -> dict:
                home = homes[name]()
                record = harness_setup.read_setup_record(home)
                if record.get("managed_by") != harness_setup.MANAGED_BY_DAEMON:
                    return {"changed": []}  # not ours (absent, or the plugin's fallback)
                return removers[name](home)
            return run

        self._remove = remove or {name: _remover(name) for name in self._ensure}
        self.status = {name: HarnessStatus() for name in self._ensure}
        self.runs = 0
        self._task: asyncio.Task | None = None

    def remove_once(self) -> dict[str, dict]:
        """Undo this daemon's setup (explicitly disabled)."""
        for name, remove in self._remove.items():
            status = self.status[name]
            status.last_run_at = time.time()
            try:
                result = remove()
            except Exception as exc:  # noqa: BLE001
                status.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(f"harness setup removal ({name}): {status.last_error}")
                continue
            status.current = False
            status.last_error = None
            if result.get("changed"):
                status.last_changed = list(result["changed"])
                status.last_changed_at = status.last_run_at
                logger.info(f"harness setup ({name}): removed {', '.join(result['changed'])}")
        return self.describe()["harnesses"]

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
            if self.resolved.remove:
                self._task = asyncio.create_task(asyncio.to_thread(self.remove_once),
                                                 name="miragend-harness-setup-remove")
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
