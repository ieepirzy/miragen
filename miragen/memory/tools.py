"""The model-tier memory tools (§18.8): the SMALL role-appropriate surface
— remember, read, checkpoint. Administrative erasure, promotion and
maintenance are capabilities of other principals, never tools shown here.

Results speak the guidance vocabulary: `accepted`, `pending`, `conflict`,
`rejected`, `persistence_unavailable` — an agent must not say "saved" for
an unacknowledged write, and the tool answers make that checkable.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from miragen.memory.lifecycle import MemoryLifecycle


def build_memory_tools(
    lifecycle: MemoryLifecycle,
    current_run_id: Callable[[], str | None],
    current_instance: Callable[[], str | None],
) -> list[Callable]:
    """Closures over the lifecycle plus the app's run/instance context
    accessors (contextvars) — the same pattern as the voice speak tool."""

    async def memory_checkpoint(state: dict[str, Any]) -> str:
        """Persist durable working state for this conversation instance.

        Shallow-merged into the stored working state (a key set to null
        removes it). Checkpoint when the goal, constraints, decisions or
        pending actions materially change — this is what survives restarts
        and context loss.

        Args:
            state: Fields to merge, e.g. {"goal": ..., "pending_actions": [...]}.
        """
        result = await lifecycle.checkpoint(
            instance=current_instance(), patch=state
        )
        return json.dumps(result)

    async def memory_remember(content: str) -> str:
        """Propose a durable memory: one focused, factual observation worth
        recalling in future runs (a decision made, a fact learned, an
        outcome). Not for transcripts, boilerplate or guesses.

        Args:
            content: The observation, self-contained and specific.
        """
        result = await lifecycle.remember(
            instance=current_instance(), run_id=current_run_id(), content=content
        )
        return json.dumps(result)

    async def memory_correct(record_id: str, correction: dict, reason: str = "") -> str:
        """Correct an erroneous stored memory record. Use when the user
        corrects something you previously recorded, or you discover a
        stored record is wrong. The correction replaces the old value in
        its own validity period — history stays queryable.

        Args:
            record_id: The record to correct.
            correction: The corrected payload, e.g. {"text": ...} or {"value": ...}.
            reason: Why — quote the user's correction when relaying one.
        """
        result = await lifecycle.correct(
            instance=current_instance(), run_id=current_run_id(),
            record_id=record_id, corrected_payload=correction, reason=reason,
        )
        return json.dumps(result)

    async def memory_read(record_id: str) -> str:
        """Read one memory record by id (its current revision, with root
        validity status).

        Args:
            record_id: The record id, e.g. from a memory packet or a
                remember result.
        """
        result = await lifecycle.read(record_id)
        return json.dumps(result)

    return [memory_checkpoint, memory_remember, memory_read, memory_correct]
