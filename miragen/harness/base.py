"""The base tier's harness seam (docs/design/harnesses.md).

A *harness* is whatever runs the model loop for a base-tier (``spec:``)
profile. PydanticAI is the default one; a self-contained agent CLI such as
Grok Build is another, even when it only drives one model family. The app
tier — instances, admission, run records, memory boundary, telemetry run
spans, triggers — stays the same regardless of the harness; only the turn
itself goes through this interface.

Selection is by model prefix, mirroring the ``claude-code:<model>`` selector
convention: ``spec.model: grok-build:grok-4.6`` picks the Grok Build
harness; any other string is a pydantic-ai model string.

(The executor tier is a different thing: one-off worker jobs with a
workspace, diff harvest and resumable threads. It does not go through here.)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from miragen.models import RunUsage, ToolCallRecord

PYDANTIC_AI = "pydantic-ai"
# Harness model prefixes. The part after the colon is the harness's own model
# id (may be empty: "use the harness default").
HARNESS_PREFIXES: dict[str, str] = {
    "grok-build:": "grok-build",
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
    # This launch's ephemeral MCP credential values (never persisted).
    secret_env: dict[str, str] | None = None
    # Per-run system context (the memory packet): transient by contract.
    extra_instructions: str | None = None


@dataclass
class HarnessResult:
    output: str
    usage: RunUsage | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)


class HarnessStream(Protocol):
    """Text deltas while the turn runs; ``result`` once iteration ends."""

    def __aiter__(self) -> AsyncIterator[str]: ...

    @property
    def result(self) -> HarnessResult: ...


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
