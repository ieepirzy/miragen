"""Memory through a hosted miragend session plane (``memory.backend: bridge``).

A miragen agent whose harness miragen drives (e.g. Grok Build) takes part in
the same memory plane as external Claude Code / Codex / Grok sessions: it
reports its session's life — opened, prompt received, turn finished — to the
plane's ``/sessions/v1/events`` exactly as the miragen-hook adapter does for
those harnesses, and puts what the plane answers (working state, guidance,
and the plane's selector-picked recall) in front of the turn's prompt. The
plane owns the Loimi principal, the scopes and the recall selector model;
this agent holds only the plane's bearer.

Recall is synchronous here: the client doesn't advertise ``async-recall``,
so the plane selects within its own timeout and returns the section in the
``input.received`` answer. A plane that can't be reached never fails a turn;
the packet says memory was unavailable instead.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from miragen.models import MemorySpec

logger = logging.getLogger("miragen.memory.bridge")

HARNESS = "miragen"
UNAVAILABLE_NOTE = (
    "[memory] the memory service could not be reached for this turn: stored "
    "memories and working state were not checked. Don't claim to remember "
    "what you can't see here.")


@dataclass
class BridgeMemory:
    spec: MemorySpec
    agent_name: str
    base_url: str
    token: str | None
    transport: httpx.AsyncBaseTransport | None = None
    timeout_s: float = 20.0
    _opened: set[str] = field(default_factory=set)
    # The harness's session sequence for an instance (Grok lifecycle): each
    # rotated session is its own plane session, so the plane closes one and
    # writes its episode before the next opens.
    session_seq: Callable[[str], int] | None = None

    @classmethod
    def from_env(cls, spec: MemorySpec, agent_name: str, env: dict[str, str]) -> "BridgeMemory":
        url = env.get(spec.endpoint_env)
        if not url:
            raise ValueError(f"memory backend 'bridge' needs {spec.endpoint_env} (the plane's URL)")
        return cls(spec=spec, agent_name=agent_name, base_url=url.rstrip("/"),
                   token=env.get(spec.credential_env))

    def session_id(self, instance: str | None) -> str:
        base = f"{self.agent_name}-{instance or 'ephemeral'}"
        seq = self.session_seq(instance) if (self.session_seq and instance) else 1
        return base if seq <= 1 else f"{base}-s{seq}"

    def session_key(self, instance: str | None) -> str:
        """What the plane calls this session (the X-Harness-Session value)."""
        return f"{HARNESS}:{self.session_id(instance)}"

    def _envelope(self, instance: str | None, name: str, original: str, *,
                  content: str | None = None, ids: dict | None = None, **attributes) -> dict:
        return {
            "harness": HARNESS, "session_id": self.session_id(instance),
            "event": {"name": name, "original_event": original, "ids": ids or {},
                      "content": content[:20_000] if content else content,
                      "attributes": attributes},
            "client": {"host": socket.gethostname(), "remote": True,
                       "project_remote": self.spec.project, "capabilities": []},
        }

    async def _post(self, envelope: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as http:
            resp = await http.post(f"{self.base_url}/sessions/v1/events", json=envelope,
                                   headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"memory plane answered http {resp.status_code}")
        return resp.json()

    async def prepare(self, *, instance: str | None, run_id: str | None, prompt: str) -> str:
        """The memory packet for this turn: working state + guidance on the
        instance's first turn in this process, and the plane's recall."""
        parts: list[str] = []
        try:
            if self.session_id(instance) not in self._opened:
                answer = await self._post(self._envelope(
                    instance, "context.started", "SessionStart", source="startup"))
                self._opened.add(self.session_id(instance))
                if answer.get("context"):
                    parts.append(answer["context"])
            answer = await self._post(self._envelope(
                instance, "input.received", "UserPromptSubmit", content=prompt,
                ids={"prompt_id": run_id} if run_id else None))
            if answer.get("context"):
                parts.append(answer["context"])
        except Exception as exc:  # the plane is optional to a turn, never fatal
            logger.warning("memory bridge unavailable: %s", exc)
            self._opened.discard(self.session_id(instance))
            return UNAVAILABLE_NOTE
        return "\n\n".join(parts)

    async def finish(self, *, instance: str | None, run_id: str | None, output: str | None,
                     status: str) -> None:
        try:
            await self._post(self._envelope(
                instance, "turn.finished", "Stop", content=output,
                ids={"turn_id": run_id} if run_id else None, stop_reason=status))
        except Exception as exc:
            logger.warning("memory bridge capture failed: %s", exc)

    async def lifecycle(self, instance: str, event: str, info: dict) -> None:
        """The harness compacted or closed a session: the plane writes its
        episode + working-state checkpoint (compact-N / end)."""
        name, original = {"compacting": ("context.compacting", "PreCompact"),
                          "closed": ("context.closed", "SessionEnd")}[event]
        try:
            await self._post(self._envelope(instance, name, original, **info))
            if event == "closed":
                self._opened.discard(self.session_id(instance))
        except Exception as exc:  # noqa: BLE001 — the plane is optional to a session, never fatal
            logger.warning("memory bridge %s event failed: %s", event, exc)
