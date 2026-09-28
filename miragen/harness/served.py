"""Which harness last served each instance (harness-neutral).

A profile can swap harnesses (``spec.model: grok-build:…`` ↔
``claude-code:…``), for example while one subscription is rate limited.
Each harness keeps its own conversation state, so after a swap the next
harness resumes a conversation that knows nothing about the turns the other
one served. Both harnesses record themselves here after every turn and
report ``fresh`` from ``session_info`` when the last turn was someone
else's, so a client adds its own recent transcript (as after a rotation).

No entry means "unknown" (state from before this ledger existed), which is
not a switch.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class ServedLedger:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def get(self, instance: str) -> dict | None:
        return self._load().get(instance)

    def switched(self, instance: str, harness: str) -> bool:
        """Was this instance's last turn served by a different harness?"""
        entry = self.get(instance)
        return entry is not None and entry.get("harness") != harness

    def mark(self, instance: str, harness: str, seq: int) -> None:
        with self._lock:
            state = self._load()
            state[instance] = {"harness": harness, "seq": seq, "at": time.time()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
            os.replace(tmp, self.path)

    def forget(self, instance: str) -> None:
        with self._lock:
            state = self._load()
            if state.pop(instance, None) is not None:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
                os.replace(tmp, self.path)
