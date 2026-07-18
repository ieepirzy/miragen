"""Spawn adapter — the no-SDK fallback: run a CLI, keep its stdout, harvest.

Deliberately minimal (refinement plan §4): an argv template is spawned inside
the workspace, stdout/stderr lines become raw `item.completed` events, exit 0
means success (shared baseline-tag harvest applies), non-zero is a resumable
'failed'. No thread handle exists, so runs are never resumable in practice —
resume returns the existing "no executor thread handle" 409 — and no usage is
reported, so only wall-clock timeout and daily budgets guard spend.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path
from typing import Any, AsyncIterator

from miragen.executor.base import ExecutorBackend

_ERROR_TAIL_LINES = 20


class SpawnExecutor(ExecutorBackend):
    async def _stream_turn(
        self,
        prompt: str,
        *,
        run_id: str,
        thread_id: str | None,
        workspace: Path,
        first_turn: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        assert self.spec.command, "spawn executor requires command (enforced by ExecutorSpec)"
        argv = [
            arg.replace("{workspace}", str(workspace)).replace("{prompt}", prompt)
            for arg in self.spec.command
        ]
        prompt_on_stdin = not any("{prompt}" in arg for arg in self.spec.command)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace),
            stdin=asyncio.subprocess.PIPE if prompt_on_stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Own process group: cancellation must take down the whole tree,
            # not just the wrapper — a surviving grandchild would keep mutating
            # a workspace that resume/abandon assumes is quiescent.
            start_new_session=True,
        )

        lines: list[str] = []
        try:
            if prompt_on_stdin:
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                text = raw.decode(errors="replace").rstrip("\n")
                lines.append(text)
                yield {"type": "item.completed", "item": {"type": "stdout", "text": text}}
            returncode = await proc.wait()
        except asyncio.CancelledError:
            # Timeout/cancellation contract: the process TREE must not outlive
            # the turn — SIGKILL the group (pgid == proc.pid thanks to
            # start_new_session), then reap briefly before propagating.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
            raise

        if returncode == 0:
            if lines:
                # The whole stdout doubles as the run output (there's no
                # structured agent_message in a bare CLI).
                yield {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "\n".join(lines)},
                }
            yield {"type": "turn.completed"}
        else:
            yield {
                "type": "turn.failed",
                "error": {
                    "message": f"command exited with code {returncode}",
                    "tail": lines[-_ERROR_TAIL_LINES:],
                },
            }
