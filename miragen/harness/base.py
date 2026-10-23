"""The base tier's harness seam (docs/design/harnesses.md).

A *harness* is whatever runs the model loop for a base-tier (``spec:``)
profile. PydanticAI is the default one; a self-contained agent CLI such as
Grok Build is another, even when it only drives one model family. The app
tier — instances, admission, run records, memory boundary, telemetry run
spans, triggers — stays the same regardless of the harness; only the turn
itself goes through this interface.

Selection is by model prefix, mirroring the ``claude-code:<model>`` selector
convention: ``spec.model: grok-build:grok-4.6`` picks the Grok Build
harness, ``claude-code:sonnet`` the Claude Code harness, ``codex:gpt-5.5`` the
Codex harness; any other string is a pydantic-ai model string.

(The executor tier is a different thing: one-off worker jobs with a
workspace, diff harvest and resumable threads. It does not go through here.)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from miragen.models import RunUsage, ToolCallRecord, mirarun_env

PYDANTIC_AI = "pydantic-ai"
# Harness model prefixes. The part after the colon is the harness's own model
# id (may be empty: "use the harness default").
HARNESS_PREFIXES: dict[str, str] = {
    "grok-build:": "grok-build",
    "claude-code:": "claude-code",
    "codex:": "codex",
}


def parse_harness_model(model: str) -> tuple[str, str]:
    """``"grok-build:grok-4.6"`` → ``("grok-build", "grok-4.6")``; any other
    string → ``("pydantic-ai", model)`` unchanged."""
    for prefix, name in HARNESS_PREFIXES.items():
        if model.startswith(prefix):
            return name, model[len(prefix):]
    return PYDANTIC_AI, model


@dataclass(frozen=True)
class HarnessTurn:
    """One turn, as the app tier hands it to a harness."""

    prompt: str
    # The state scope the turn converses with; None = ephemeral run.
    instance: str | None
    # Continue the instance's conversation (instances/v1 use_history).
    use_history: bool = False
    run_id: str | None = None
    # This launch's ephemeral MCP credential values (never persisted, never
    # in repr, so never in a log line that prints a turn).
    secret_env: dict[str, str] | None = field(default=None, repr=False)
    # Per-run system context (the memory packet): transient by contract.
    extra_instructions: str | None = None
    # MiraRun's ids for the run or session (execution-refs/v1). A harness
    # sets them as MIRARUN_* in the agent process's environment; for a
    # named instance it keeps them in the instance's state so a restarted
    # process gets them too.
    mirarun: dict[str, str] | None = None


@dataclass
class HarnessResult:
    output: str
    usage: RunUsage | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    # Where the turn's time went (miragen/harness/timing.py), when the harness
    # measures it; lands on RunRecord.timings.
    timings: dict | None = None


class HarnessStream(Protocol):
    """Text deltas while the turn runs; ``result`` once iteration ends."""

    def __aiter__(self) -> AsyncIterator[str]: ...

    @property
    def result(self) -> HarnessResult: ...


class TaskStream:
    """A HarnessStream over a turn task that pushes text as it arrives."""

    def __init__(self):
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._result: HarnessResult | None = None

    def attach(self, task: asyncio.Task) -> None:
        self._task = task
        task.add_done_callback(lambda _t: self._queue.put_nowait(None))

    def push(self, text: str) -> None:
        self._queue.put_nowait(text)

    async def __aiter__(self):
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item
        assert self._task is not None
        self._result = await self._task  # re-raises a failed turn

    @property
    def result(self) -> HarnessResult:
        if self._result is None:
            raise RuntimeError("stream not finished")
        return self._result


class InstanceBusyError(RuntimeError):
    """The instance has a turn in flight; its state can't be discarded now."""


@runtime_checkable
class Harness(Protocol):
    name: str

    async def run(self, turn: HarnessTurn) -> HarnessResult: ...

    def stream(self, turn: HarnessTurn) -> AbstractAsyncContextManager[HarnessStream]:
        """An async context manager yielding a HarnessStream, so a harness can
        hold its process or connection open for exactly the stream's life."""
        ...

    async def aclose(self) -> None: ...


# History persistence hooks for harnesses whose conversation state the app
# tier stores (pydantic-ai). A harness that owns its conversation natively
# (Grok Build sessions) doesn't use them.
LoadHistory = Callable[[str], list]
SaveHistory = Callable[[str, list, "str | None"], None]


class MiraRunEnv:
    """MIRARUN_* in a harness's agent processes (execution-refs/v1).

    For the CLI harnesses, which start one process per instance (or per
    one-off run) and keep it across turns. A named instance's refs live in
    its state entry (``mirarun``), so a process started after a restart — or
    after a crash — gets the same ones; a one-off run's are held in memory
    only for as long as its process is being started. A live process keeps
    the environment it started with: refs that change apply from the next
    process (rotate the instance to apply them at once).

    The harness supplies ``_load_state`` and ``_update_instance``."""

    async def _remember_mirarun(self, instance: str, ephemeral: bool,
                                refs: dict[str, str] | None) -> None:
        if not refs:
            return
        if ephemeral:
            self.__dict__.setdefault("_mirarun_ephemeral", {})[instance] = dict(refs)
            return
        if (self._load_state().get(instance) or {}).get("mirarun") != refs:
            await self._update_instance(instance, mirarun=dict(refs))

    def _forget_mirarun(self, instance: str) -> None:
        self.__dict__.get("_mirarun_ephemeral", {}).pop(instance, None)

    def _mirarun_env(self, instance: str) -> dict[str, str]:
        refs = self.__dict__.get("_mirarun_ephemeral", {}).get(instance)
        if refs is None:
            refs = (self._load_state().get(instance) or {}).get("mirarun")
        return mirarun_env(refs)
