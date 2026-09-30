"""Keep routine polling out of the access log.

The agent container answers a Docker health check every ~20 s, Mira's
approval long-poll every ~30 s, and run polling every ~2 s during a turn.
Logged one line each, they buried everything else (Mira, 2026-09-26). This
filter on ``uvicorn.access`` lets through only every Nth successful request
on those paths; any non-2xx answer is always logged.

``MIRAGEN_ACCESS_LOG_SAMPLE`` sets N (default 100; 1 logs everything).
"""

from __future__ import annotations

import logging
import os
from collections import Counter

ROUTINE_PREFIXES = ("/health", "/approvals", "/runs/")


class RoutinePollingFilter(logging.Filter):
    def __init__(self, every: int | None = None, prefixes: tuple[str, ...] = ROUTINE_PREFIXES):
        super().__init__()
        if every is None:
            try:
                every = int(os.getenv("MIRAGEN_ACCESS_LOG_SAMPLE", "100"))
            except ValueError:
                every = 100
        self.every = max(1, every)
        self.prefixes = prefixes
        self.seen: Counter[str] = Counter()

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access: (client_addr, method, full_path, http_version, status_code)
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path, status = str(args[2]), args[4]
        prefix = next((p for p in self.prefixes if path.startswith(p)), None)
        if prefix is None:
            return True
        try:
            if not 200 <= int(status) < 300:
                return True
        except (TypeError, ValueError):
            return True
        self.seen[prefix] += 1
        return self.seen[prefix] % self.every == 1 or self.every == 1


def install() -> None:
    """Idempotent: attach the filter to uvicorn's access logger once."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RoutinePollingFilter) for f in logger.filters):
        logger.addFilter(RoutinePollingFilter())
