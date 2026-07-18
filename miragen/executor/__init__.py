"""Executor backend tier — abstract contract + concrete adapters.

See miragen.executor.base for the contract; docs/design/executor-tier-refinement.md
for the design record.
"""

from __future__ import annotations

from pathlib import Path

from miragen.executor.base import ExecutorBackend, ExecutorResult
from miragen.executor.codex import CodexExecutor
from miragen.models import AgentProfile

__all__ = [
    "ExecutorBackend",
    "ExecutorResult",
    "CodexExecutor",
    "build_executor",
]


def build_executor(profile: AgentProfile, *, runs_root: Path) -> ExecutorBackend:
    """Dispatch on ExecutorSpec.executor. The non-codex adapters import lazily
    so their SDKs stay optional extras."""
    assert profile.executor is not None
    kind = profile.executor.executor
    if kind == "codex":
        return CodexExecutor(profile, runs_root=runs_root)
    if kind == "claude-code":
        from miragen.executor.claude_code import ClaudeCodeExecutor

        return ClaudeCodeExecutor(profile, runs_root=runs_root)
    if kind == "spawn":
        from miragen.executor.spawn import SpawnExecutor

        return SpawnExecutor(profile, runs_root=runs_root)
    raise ValueError(f"unknown executor backend: {kind}")  # unreachable via the Literal
