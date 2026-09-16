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

def default_state_dir() -> Path:
    """The context-map home: /agent/memory inside an agent container, or
    MIRAGEN_MEMORY_STATE_DIR for the hook bridge running in an external
    harness session (where /agent does not exist)."""
    import os

    return Path(os.environ.get("MIRAGEN_MEMORY_STATE_DIR", "/agent/memory"))


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
        state_dir: Path | None = None,
        tools_available: bool = True,
        selector=None,
    ) -> None:
        self.spec = spec
        self.profile_name = profile_name
        self.client = client
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.tools_available = tools_available
        # The zero-or-more relevance selector (§17.7); None = the optional
        # recall lane reports itself unconfigured rather than degrading.
        self.selector = selector
        # Selector-ID cache (§17.7: permitted, canonical state re-checked
        # every packet): instance -> (state_revision, query_digest, ids+reasons).
        self._selection_cache: dict[str, tuple[int, str, list[tuple[str, str]]]] = {}
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
        self, *, instance: str | None, run_id: str | None, trigger: str,
        prompt_hint: str | None = None,
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

        optional_status = await self._optional_lane(
            packet, effective_instance, context, prompt_hint
        )
        # The manifest records what was ACTUALLY injected (§8.6); its write
        # is best-effort — a manifest failure must not fail the turn.
        try:
            manifest = await self.client.create_manifest({
                "scope_id": self.spec.scopes.default_write,
                "context_id": context["id"],
                "run_ref": run_id,
                "items": [
                    {"revision_id": item["revision_id"], "reason": item["reason"]}
                    for item in packet.items if item.get("revision_id")
                ],
                "policy": {
                    "guidance_version": GUIDANCE_VERSION,
                    "trigger": trigger,
                    "lane": "required+optional",
                    "state_revision": context["state_revision"],
                    "optional_status": optional_status,
                },
                "degraded": optional_status
                if optional_status.startswith("degraded") else None,
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

    async def _optional_lane(
        self, packet: MemoryPacket, instance: str, context: dict,
        prompt_hint: str | None,
    ) -> str:
        """The optional recall lane (§17.7): bounded hybrid search, the
        zero-or-more selector, canonical re-render, budgeted injection.
        Failure NEVER falls back to stuffing neighbors — required state
        stands alone and the degradation is explicit in the manifest."""
        from miragen.memory.selection import clamp_selections, render_optional_section

        if not self.spec.recall.enabled:
            return "disabled"
        if self.selector is None:
            return "unconfigured"
        state = context.get("state") or {}
        goal = state.get("goal") if isinstance(state.get("goal"), str) else None
        query = " ".join(part for part in (goal, prompt_hint) if part)[:2000].strip()
        if not query:
            return "no_query"

        query_digest = _stable_digest(query)
        try:
            cached = self._selection_cache.get(instance)
            if cached and cached[0] == context["state_revision"] and cached[1] == query_digest:
                selected = cached[2]
            else:
                found = await self.client.search_memory({
                    "scope_ids": self.spec.scopes.read,
                    "query_text": query,
                    "limit": self.spec.recall.max_candidates,
                })
                cards = found["items"]
                if not cards:
                    self._selection_cache[instance] = (
                        context["state_revision"], query_digest, []
                    )
                    return "empty"
                result = await self.selector(query, cards)
                kept = clamp_selections(result, cards, self.spec.recall.max_selected)
                selected = [(sel.record_id, sel.reason) for sel in kept]
                self._selection_cache[instance] = (
                    context["state_revision"], query_digest, selected
                )

            if not selected:
                return "none_selected"

            # Canonical re-render (§17.7 step 5): fetch each selected id
            # fresh — the selector chose, canonical state speaks.
            entries = []
            for record_id, reason in selected:
                record = await self.client.get_record(record_id)
                if (
                    record.get("admission") != "accepted"
                    or not record.get("roots_valid", False)
                    or record.get("revision") is None
                ):
                    continue
                payload = record["revision"]["payload"]
                text = str(payload.get("text") or payload.get("value") or payload)[:500]
                entries.append({
                    "record_id": record_id,
                    "revision_id": record["revision"]["id"],
                    "type": record["type"],
                    "text": text,
                    "reason": reason,
                })
            section = render_optional_section(
                entries, self.spec.recall.max_optional_chars
            )
            if not section:
                return "none_selected"
            packet.text = f"{packet.text}\n{section}"
            packet.items.extend({
                "kind": "recalled",
                "record_id": entry["record_id"],
                "revision_id": entry["revision_id"],
                "reason": entry["reason"],
            } for entry in entries)
            return "ok"
        except (MemoryUnavailable, MemoryAPIError) as exc:
            self._degrade(f"optional recall: {exc}")
            packet.text += (
                "\n[optional recall degraded — memories may exist that could "
                "not be searched; do not conclude the store is empty]"
            )
            return f"degraded: {exc}"
        except Exception as exc:  # selector failure: inject nothing extra
            self._degrade(f"relevance selection: {exc}")
            packet.text += (
                "\n[optional recall degraded — relevance selection failed; "
                "memories were not injected]"
            )
            return f"degraded: selector: {exc}"

    async def recall_section(
        self, *, instance: str | None, prompt_hint: str, run_id: str | None = None,
        trigger: str = "prompt",
    ) -> tuple[str | None, str]:
        """Prompt-time recall for an already-opened context: ONLY the
        optional lane (§17.7), rendered without re-injecting guidance or
        working state. Returns (section text or None, lane status). The
        manifest records what was actually injected, as at the boundary."""
        effective_instance = instance or DEFAULT_INSTANCE
        if not self.spec.recall.enabled:
            return None, "disabled"
        if self.selector is None:
            return None, "unconfigured"
        try:
            context = await self._ensure_context(effective_instance)
        except MemoryUnavailable as exc:
            return None, f"degraded: {self._degrade(f'recall: {exc}')}"
        except MemoryAPIError as exc:
            return None, f"degraded: {self._degrade(f'recall refused: {exc}')}"
        packet = MemoryPacket(text="", context_id=context["id"],
                              state_revision=context["state_revision"])
        status = await self._optional_lane(packet, effective_instance, context, prompt_hint)
        section = packet.text.strip() or None
        if packet.items:
            try:
                await self.client.create_manifest({
                    "scope_id": self.spec.scopes.default_write,
                    "context_id": context["id"],
                    "run_ref": run_id,
                    "items": [
                        {"revision_id": item["revision_id"], "reason": item["reason"]}
                        for item in packet.items if item.get("revision_id")
                    ],
                    "policy": {
                        "guidance_version": GUIDANCE_VERSION,
                        "trigger": trigger,
                        "lane": "optional",
                        "state_revision": context["state_revision"],
                        "optional_status": status,
                    },
                    "degraded": None,
                })
            except (MemoryUnavailable, MemoryAPIError) as exc:
                logger.warning(f"[{self.profile_name}] manifest write failed: {exc}")
        return section, status

    async def capture_episode(
        self, *, instance: str | None, idempotency_key: str, content: str,
        source_ref: str | None, attributes: dict[str, Any] | None = None,
    ) -> dict:
        """Durable, idempotent capture of a session EPISODE — a digest an
        external session's daemon assembled from what it observed. Unlike
        `capture_harness_event` (operational trail, kind `harness:*`, which
        the extraction worker skips) this lands as kind `session_episode`,
        so the bounded extractor may later propose memories from it."""
        effective_instance = instance or DEFAULT_INSTANCE
        try:
            mapping = self._read_map()
            context_id = mapping.get(effective_instance)
            result = await self.client.append_event(
                scope_id=self.spec.scopes.default_write,
                idempotency_key=idempotency_key,
                source={"kind": "session_episode", "ref": source_ref},
                content=content[:20_000],
                attributes={
                    "instance": effective_instance,
                    "profile": self.profile_name,
                    **(attributes or {}),
                },
                occurred_at=datetime.now(timezone.utc).isoformat(),
                context_ids=[context_id] if context_id else None,
            )
            return {"status": "captured", "event_id": result["id"],
                    "created": result.get("created", True)}
        except (MemoryUnavailable, MemoryAPIError) as exc:
            return {"status": "persistence_unavailable",
                    "detail": self._degrade(f"episode capture: {exc}")}

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

    async def capture_harness_event(self, *, instance: str | None, event) -> dict:
        """Durable capture of one normalized harness hook event (§18.7).
        Idempotent per occurrence; fail-open for the harness but counted
        as degradation, never silent."""
        from miragen.memory.harness_hooks import event_idempotency_key
        from miragen_hook.normalize import captured_content

        try:
            result = await self.client.append_event(
                scope_id=self.spec.scopes.default_write,
                idempotency_key=event_idempotency_key(event),
                source={
                    "kind": f"harness:{event.name}",
                    "ref": f"session:{event.session_id}" if event.session_id else None,
                },
                content=captured_content(event),
                attributes={
                    "harness": event.harness,
                    "original_event": event.original_event,
                    "instance": instance or DEFAULT_INSTANCE,
                    "profile": self.profile_name,
                    **event.ids,
                    **event.attributes,
                },
            )
            return {"status": "captured", "event_id": result["id"],
                    "created": result.get("created", True)}
        except (MemoryUnavailable, MemoryAPIError) as exc:
            return {"status": "persistence_unavailable",
                    "detail": self._degrade(f"hook capture: {exc}")}

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

    async def correct(
        self,
        *,
        instance: str | None,
        run_id: str | None,
        record_id: str,
        corrected_payload: dict[str, Any],
        reason: str = "",
    ) -> dict:
        """Immediate active correction (§17.5): the correction is captured
        as its own evidence event — kind `agent_relayed_correction`, never
        a self-asserted 'human' label (§17.6) — and applied through the
        CAS-guarded correct route with one concurrent-writer retry. It
        takes effect for the next read the moment the transaction commits;
        no vector backfill is waited on."""
        try:
            record = await self.client.get_record(record_id)
            event = await self.client.append_event(
                scope_id=record["scope_id"],
                idempotency_key=(
                    f"correct:{run_id or 'manual'}:{record_id}:"
                    f"{_stable_digest(json.dumps(corrected_payload, sort_keys=True))}"
                ),
                source={"kind": "agent_relayed_correction",
                        "ref": f"run:{run_id}" if run_id else None},
                content=reason or f"correction of record {record_id}",
                attributes={"record_id": record_id, "run_id": run_id,
                            "instance": instance or DEFAULT_INSTANCE,
                            "profile": self.profile_name},
            )
            body = {
                "payload": corrected_payload,
                "source_event_ids": [event["id"]],
                "expected_seq": record["revision"]["seq"],
                "reason": reason,
            }
            try:
                result = await self.client.correct_record(record_id, body)
            except MemoryAPIError as exc:
                if exc.status_code != 409:
                    raise
                fresh = await self.client.get_record(record_id)
                body["expected_seq"] = fresh["revision"]["seq"]
                result = await self.client.correct_record(record_id, body)
            return {
                "status": "accepted",
                "record_id": record_id,
                "new_seq": result["revision"]["seq"],
                "resolution": result.get("resolution"),
            }
        except MemoryUnavailable as exc:
            return {"status": "persistence_unavailable",
                    "detail": self._degrade(f"correct: {exc}")}
        except MemoryAPIError as exc:
            if exc.status_code == 409:
                return {"status": "conflict", "detail": str(exc)}
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
