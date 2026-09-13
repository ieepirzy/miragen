"""The memory lifecycle: what happens at the run boundary (§17.3).

`prepare_context` restores the instance's working state by IDENTITY (no
search — M1) and renders the injected packet: the versioned guidance block
plus the working-state data, wrapped as attributed reference data.
`finish_turn` captures the run outcome as an idempotent durable event.
`checkpoint`/`remember`/`read` back the agent-facing tools.

Degradation contract (§8.6/§18.8): a reachability failure is EXPLICIT —
the packet says memory is degraded, tool results say
`persistence_unavailable`, and nothing is ever misreported as "no relevant
memories" or "saved". Memory failures never fail the run itself.

Work-context identity: one durable context per (profile, instance), with
the id → instance map kept on the agent volume (/agent/memory/contexts.json)
— /memory/v1 addresses contexts by id, and the agent volume is exactly as
durable as the instance histories living beside it. A lost map entry (or a
404 on a mapped id) recreates the context: state is then genuinely gone,
and the packet says so rather than inventing a recollection.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from miragen.memory.client import MemoryAPIError, MemoryClient, MemoryUnavailable
from miragen.memory.guidance import GUIDANCE_VERSION, build_guidance
from miragen.models import DEFAULT_INSTANCE, MemorySpec

logger = logging.getLogger(__name__)

STATE_DIR = Path("/agent/memory")


@dataclass
class MemoryPacket:
    """What the run boundary injects. `text` is the rendered block;
    `degraded` is None only when preparation fully succeeded."""

    text: str
    context_id: str | None = None
    state_revision: int | None = None
    degraded: str | None = None
    manifest_id: str | None = None
    guidance_version: str = GUIDANCE_VERSION
    items: list[dict[str, Any]] = field(default_factory=list)


class MemoryLifecycle:
    def __init__(
        self,
        spec: MemorySpec,
        profile_name: str,
        client: MemoryClient,
        *,
        state_dir: Path = STATE_DIR,
        tools_available: bool = True,
    ) -> None:
        self.spec = spec
        self.profile_name = profile_name
        self.client = client
        self.state_dir = Path(state_dir)
        self.tools_available = tools_available
        # Honest health surface: how often memory degraded, and why last.
        self.degraded_count = 0
        self.last_degraded: str | None = None

    # -- context identity --------------------------------------------------

    def _map_path(self) -> Path:
        return self.state_dir / "contexts.json"

    def _read_map(self) -> dict[str, str]:
        try:
            data = json.loads(self._map_path().read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_map(self, mapping: dict[str, str]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._map_path().write_text(json.dumps(mapping))

    async def _ensure_context(self, instance: str) -> dict:
        mapping = self._read_map()
        context_id = mapping.get(instance)
        if context_id is not None:
            try:
                return await self.client.get_context(context_id)
            except MemoryAPIError as exc:
                if exc.status_code != 404:
                    raise
                logger.warning(
                    f"[{self.profile_name}] mapped work context {context_id} for "
                    f"instance '{instance}' no longer exists — recreating (state lost)"
                )
        created = await self.client.create_context(
            scope_id=self.spec.scopes.default_write,
            kind="task",
            title=f"{self.profile_name}/{instance}",
        )
        mapping[instance] = created["id"]
        self._write_map(mapping)
        return created

    # -- degradation bookkeeping ------------------------------------------

    def _degrade(self, reason: str) -> str:
        self.degraded_count += 1
        self.last_degraded = reason
        logger.warning(f"[{self.profile_name}] memory degraded: {reason}")
        return reason

    # -- the boundary ------------------------------------------------------

    async def prepare_context(
        self, *, instance: str | None, run_id: str | None, trigger: str
    ) -> MemoryPacket:
        effective_instance = instance or DEFAULT_INSTANCE
        try:
            context = await self._ensure_context(effective_instance)
        except MemoryUnavailable as exc:
            reason = self._degrade(f"prepare: {exc}")
            return MemoryPacket(
                text=self._render(None, degraded=reason), degraded=reason
            )
        except MemoryAPIError as exc:
            reason = self._degrade(f"prepare refused: {exc}")
            return MemoryPacket(
                text=self._render(None, degraded=reason), degraded=reason
            )

        packet = MemoryPacket(
            text=self._render(context, degraded=None),
            context_id=context["id"],
            state_revision=context["state_revision"],
            items=[{
                "kind": "working_state",
                "context_id": context["id"],
                "state_revision": context["state_revision"],
                "reason": "required lane: instance working state by identity",
            }],
        )
        # The manifest records what was ACTUALLY injected (§8.6); its write
        # is best-effort — a manifest failure must not fail the turn.
        try:
            manifest = await self.client.create_manifest({
                "scope_id": self.spec.scopes.default_write,
                "context_id": context["id"],
                "run_ref": run_id,
                "items": [],
                "policy": {
                    "guidance_version": GUIDANCE_VERSION,
                    "trigger": trigger,
                    "lane": "required",
                    "state_revision": context["state_revision"],
                },
                "degraded": None,
            })
            packet.manifest_id = manifest["id"]
        except (MemoryUnavailable, MemoryAPIError) as exc:
            logger.warning(f"[{self.profile_name}] manifest write failed: {exc}")
        return packet

    def _render(self, context: dict | None, *, degraded: str | None) -> str:
        guidance = build_guidance(
            scopes_read=self.spec.scopes.read,
            default_write=self.spec.scopes.default_write,
            tools_available=self.tools_available,
            degraded=degraded,
        )
        if context is None:
            return guidance
        state = context.get("state") or {}
        body = json.dumps(state, indent=1, ensure_ascii=False) if state else "(empty)"
        return (
            f"{guidance}\n"
            f"[working state — context {context['id']} rev {context['state_revision']}; "
            "reference data]\n"
            f"{body}"
        )

    async def finish_turn(
        self,
        *,
        instance: str | None,
        run_id: str,
        trigger: str,
        status: str,
        summary: str | None = None,
        error: str | None = None,
        turn: int = 0,
    ) -> dict:
        """Durable, idempotent run-outcome capture. The idempotency key is
        (principal, profile:run:turn), so a retried finish never writes a
        second episode while each executor resume turn still gets its own
        (§8.3)."""
        effective_instance = instance or DEFAULT_INSTANCE
        content = summary or error or f"run {status}"
        try:
            mapping = self._read_map()
            context_id = mapping.get(effective_instance)
            event = await self.client.append_event(
                scope_id=self.spec.scopes.default_write,
                idempotency_key=f"{self.profile_name}:{run_id}:t{turn}:finish",
                source={"kind": "agent_run", "ref": f"run:{run_id}"},
                content=content[:20_000],
                attributes={
                    "run_id": run_id,
                    "trigger": trigger,
                    "status": status,
                    "instance": effective_instance,
                    "profile": self.profile_name,
                },
                occurred_at=datetime.now(timezone.utc).isoformat(),
                context_ids=[context_id] if context_id else None,
            )
            return {"status": "captured", "event_id": event["id"]}
        except (MemoryUnavailable, MemoryAPIError) as exc:
            reason = self._degrade(f"finish: {exc}")
            return {"status": "persistence_unavailable", "detail": reason}

    # -- tool backends -----------------------------------------------------

    async def checkpoint(self, *, instance: str | None, patch: dict[str, Any]) -> dict:
        effective_instance = instance or DEFAULT_INSTANCE
        try:
            context = await self._ensure_context(effective_instance)
            try:
                updated = await self.client.patch_context(
                    context["id"],
                    expected_revision=context["state_revision"],
                    patch=patch,
                )
            except MemoryAPIError as exc:
                if exc.status_code != 409:
                    raise
                # One concurrent-writer retry against fresh state; a second
                # conflict is reported, not silently overwritten (§8.4).
                context = await self.client.get_context(context["id"])
                updated = await self.client.patch_context(
                    context["id"],
                    expected_revision=context["state_revision"],
                    patch=patch,
                )
            return {
                "status": "accepted",
                "context_id": updated["id"],
                "state_revision": updated["state_revision"],
            }
        except MemoryUnavailable as exc:
            return {"status": "persistence_unavailable",
                    "detail": self._degrade(f"checkpoint: {exc}")}
        except MemoryAPIError as exc:
            if exc.status_code == 409:
                return {"status": "conflict", "detail": str(exc)}
            return {"status": "rejected", "detail": str(exc)}

    async def remember(
        self,
        *,
        instance: str | None,
        run_id: str | None,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> dict:
        """Agent-proposed durable observation: the statement lands as a
        source event, and the record is rooted in it — so even an agent
        note carries honest provenance ('the agent said this, then')."""
        try:
            event = await self.client.append_event(
                scope_id=self.spec.scopes.default_write,
                idempotency_key=f"note:{run_id or 'manual'}:{_stable_digest(content)}",
                source={"kind": "agent_note",
                        "ref": f"run:{run_id}" if run_id else None},
                content=content[:20_000],
                attributes={"run_id": run_id, "profile": self.profile_name,
                            "instance": instance or DEFAULT_INSTANCE},
            )
            record = await self.client.propose_record({
                "type": "observation",
                "scope_id": self.spec.scopes.default_write,
                "payload": payload or {"text": content[:2_000]},
                "source_event_ids": [event["id"]],
            })
            return {
                "status": record["admission"],  # 'accepted' (rooted)
                "record_id": record["record_id"],
                "event_id": event["id"],
            }
        except MemoryUnavailable as exc:
            return {"status": "persistence_unavailable",
                    "detail": self._degrade(f"remember: {exc}")}
        except MemoryAPIError as exc:
            return {"status": "rejected", "detail": str(exc)}

    async def read(self, record_id: str) -> dict:
        try:
            return await self.client.get_record(record_id)
        except MemoryUnavailable as exc:
            return {"status": "persistence_unavailable",
                    "detail": self._degrade(f"read: {exc}")}
        except MemoryAPIError as exc:
            return {"status": "rejected", "detail": str(exc)}


def _stable_digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]
