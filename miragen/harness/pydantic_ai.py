"""The default harness: miragen's own PydanticAI loop.

Behaviour is exactly what the app tier did inline before the harness seam:
the startup agent is reused unless the turn carries ephemeral credentials or
a memory packet (both are baked into capabilities/instructions at build
time, so those turns get a per-run agent); history is PydanticAI message JSON
persisted by the app tier's history store.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from miragen.harness.base import (
    PYDANTIC_AI, HarnessResult, HarnessTurn, LoadHistory, SaveHistory,
)
from miragen.runs import extract_run_details

logger = logging.getLogger(__name__)

# (secret_env, extra_instructions) -> (agent, limits)
BuildRunAgent = Callable[[dict | None, str | None], tuple[Any, Any]]


class PydanticAIHarness:
    name = PYDANTIC_AI

    def __init__(self, agent: Any, limits: Any, *, build_run_agent: BuildRunAgent,
                 load_history: LoadHistory, save_history: SaveHistory):
        self.agent = agent
        self.limits = limits
        self._build_run_agent = build_run_agent
        self._load_history = load_history
        self._save_history = save_history

    def _agent_for(self, turn: HarnessTurn) -> tuple[Any, Any]:
        if turn.secret_env or turn.extra_instructions is not None:
            return self._build_run_agent(turn.secret_env, turn.extra_instructions)
        return self.agent, self.limits

    def _history(self, turn: HarnessTurn) -> list | None:
        if not turn.use_history:
            return None
        try:
            return self._load_history(turn.instance) or None
        except Exception:
            logger.warning("Failed to load history, starting fresh")
            return None

    def _persist(self, turn: HarnessTurn, messages: list) -> None:
        if not turn.use_history:
            return
        try:
            self._save_history(turn.instance, messages, turn.run_id)
        except Exception:
            logger.warning("Failed to save history")

    async def run(self, turn: HarnessTurn) -> HarnessResult:
        agent, limits = self._agent_for(turn)
        history = self._history(turn)
        result = await agent.run(turn.prompt, usage_limits=limits, message_history=history)
        self._persist(turn, result.all_messages())
        usage, tool_calls = extract_run_details(result)
        return HarnessResult(output=str(result.output), usage=usage, tool_calls=tool_calls)

    @asynccontextmanager
    async def stream(self, turn: HarnessTurn) -> AsyncIterator["_PydanticStream"]:
        agent, limits = self._agent_for(turn)
        history = self._history(turn)
        async with agent.run_stream(turn.prompt, usage_limits=limits,
                                    message_history=history) as stream:
            yield _PydanticStream(self, turn, stream)

    async def aclose(self) -> None:
        return None


class _PydanticStream:
    def __init__(self, harness: PydanticAIHarness, turn: HarnessTurn, stream: Any):
        self._harness = harness
        self._turn = turn
        self._stream = stream
        self._chunks: list[str] = []
        self._result: HarnessResult | None = None

    async def __aiter__(self):
        async for chunk in self._stream.stream_text(delta=True):
            self._chunks.append(chunk)
            yield chunk
        self._harness._persist(self._turn, self._stream.all_messages())
        try:
            usage, tool_calls = extract_run_details(self._stream)
        except Exception:
            usage, tool_calls = None, []
        self._result = HarnessResult(output="".join(self._chunks), usage=usage,
                                     tool_calls=tool_calls)

    @property
    def partial_output(self) -> str:
        return "".join(self._chunks)

    @property
    def result(self) -> HarnessResult:
        if self._result is None:
            raise RuntimeError("stream not finished")
        return self._result
