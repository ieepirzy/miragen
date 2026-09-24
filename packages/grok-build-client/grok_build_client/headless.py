"""Headless transport: one `grok -p` process per turn, streaming-json."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from grok_build_client.normalize import normalize_headless_event
from grok_build_client.util import resolve_grok_bin

_DEFAULT_STREAM_LIMIT = 16 * 1024 * 1024


def build_headless_argv(
    grok: str,
    prompt: str,
    *,
    cwd: str,
    session_id: str | None = None,
    resume: bool = False,
    always_approve: bool = True,
    model: str | None = None,
    reasoning_effort: str | None = None,
    web_search: bool = False,
    tools: Sequence[str] | None = None,
    disallowed_tools: Sequence[str] | None = None,
    permission_mode: str | None = None,
    allow: Sequence[str] | None = None,
    deny: Sequence[str] | None = None,
    max_turns: int | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build argv for a headless turn (documented CLI flags).

    Tool surface (grok 1.0.41, all headless-capable):

    - ``tools`` -> ``--tools a,b``: built-in allowlist. MCP meta-tools
      (``search_tool`` / ``use_tool``) stay unless listed out or denied.
    - ``disallowed_tools`` -> ``--disallowed-tools a,b``: removed tools;
      supports ``Agent`` / ``Agent(<type>)`` for subagents. Wins over
      ``tools``.
    - ``permission_mode`` -> ``--permission-mode``. When set it replaces the
      ``--always-approve`` shorthand (``always_approve`` is ignored), so a
      caller asking for ``dontAsk`` never also gets always-approve.
    - ``allow`` / ``deny`` -> repeated ``--allow`` / ``--deny`` rules
      (``Bash(...)``, ``Read``, ``MCPTool(server__tool)`` ...). Deny wins.
    - ``max_turns`` -> ``--max-turns``.
    """
    argv = [
        grok,
        "--no-auto-update",
        "-p", prompt,
        "--cwd", cwd,
        "--output-format", "streaming-json",
    ]
    if permission_mode:
        argv.extend(["--permission-mode", permission_mode])
    elif always_approve:
        argv.append("--always-approve")
    if session_id:
        if resume:
            argv.extend(["-r", session_id])
        else:
            argv.extend(["-s", session_id])
    if model:
        argv.extend(["-m", model])
    if reasoning_effort:
        argv.extend(["--reasoning-effort", reasoning_effort])
    if not web_search:
        argv.append("--disable-web-search")
    if tools is not None:
        argv.extend(["--tools", _csv("tools", tools)])
    if disallowed_tools is not None:
        argv.extend(["--disallowed-tools", _csv("disallowed_tools", disallowed_tools)])
    for rule in allow or ():
        argv.extend(["--allow", rule])
    for rule in deny or ():
        argv.extend(["--deny", rule])
    if max_turns is not None:
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        argv.extend(["--max-turns", str(max_turns)])
    if extra_args:
        argv.extend(extra_args)
    return argv


def _csv(label: str, names: Sequence[str]) -> str:
    """Join tool names for a comma-separated flag, refusing values that would
    silently change meaning (empty lists, blanks, embedded commas)."""
    if isinstance(names, str):
        raise TypeError(f"{label} must be a sequence of tool names, not a string")
    cleaned = [n.strip() for n in names]
    if not cleaned or any(not n or "," in n for n in cleaned):
        raise ValueError(f"{label} needs non-empty tool names without commas: {list(names)!r}")
    return ",".join(cleaned)


@dataclass
class HeadlessSession:
    """Run one headless turn and yield normalized events."""

    prompt: str
    cwd: str = "."
    session_id: str | None = None
    resume: bool = False
    always_approve: bool = True
    model: str | None = None
    reasoning_effort: str | None = None
    web_search: bool = False
    grok_home: str | None = None
    grok_bin: str | None = None
    env: Mapping[str, str] | None = None
    stream_limit: int = _DEFAULT_STREAM_LIMIT
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    permission_mode: str | None = None
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    max_turns: int | None = None
    extra_args: list[str] = field(default_factory=list)

    async def run(self) -> AsyncIterator[dict[str, Any]]:
        grok = resolve_grok_bin(self.grok_bin)
        if not grok:
            yield {
                "type": "error",
                "message": "grok CLI not found on PATH (set GROK_BIN or install Grok Build)",
            }
            return

        argv = build_headless_argv(
            grok,
            self.prompt,
            cwd=self.cwd,
            session_id=self.session_id,
            resume=self.resume,
            always_approve=self.always_approve,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            web_search=self.web_search,
            tools=self.tools,
            disallowed_tools=self.disallowed_tools,
            permission_mode=self.permission_mode,
            allow=self.allow,
            deny=self.deny,
            max_turns=self.max_turns,
            extra_args=self.extra_args,
        )
        env = dict(self.env) if self.env is not None else os.environ.copy()
        if self.grok_home:
            env["GROK_HOME"] = self.grok_home

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
            limit=self.stream_limit,
        )

        def _kill_tree() -> None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

        try:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                text = raw.decode(errors="replace").rstrip("\n")
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    for norm in normalize_headless_event(event):
                        yield norm
            returncode = await proc.wait()
        except asyncio.CancelledError:
            _kill_tree()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
            raise
        except Exception:
            _kill_tree()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
            raise

        if returncode != 0:
            yield {
                "type": "error",
                "message": f"grok exited with code {returncode}",
            }
