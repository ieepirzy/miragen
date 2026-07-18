"""Claude Code adapter — runs jobs through the claude-agent-sdk.

Same workspace-in / diff-and-events-out contract as Codex. The SDK session id
plays the thread_id role on the run record: resume re-opens the session bound
to the run. Jobs are atomic — nothing persists between distinct runs; the
per-run session is the resume state, exactly as a Codex thread is.

Auth: `ANTHROPIC_API_KEY` in the container environment, or Claude Code OAuth
credentials volume-mounted at ~/.claude (the Codex `auth.json` story, one door
over). `prepare()` warns at startup when neither is visible.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from miragen.executor.base import ExecutorBackend
from miragen.models import AgentProfile

logger = logging.getLogger("miragen.executor")

# approval_policy is a codex-shaped enum; map it onto Claude Code permission
# modes rather than inventing a parallel knob. 'never' (the unattended
# default) must not stall on interactive approval — same gotcha as Codex.
_PERMISSION_MODES = {
    "never": "bypassPermissions",
    "on-request": "acceptEdits",
    "on-failure": "acceptEdits",
    "untrusted": "default",
}


class ClaudeCodeExecutor(ExecutorBackend):
    """Runs jobs through the claude-agent-sdk (`pip install miragen[claude-code]`).

    `query_factory` exists for tests: it receives (prompt, options: dict) and
    returns an async iterator of SDK messages — or of already-normalized
    payload dicts, which pass through untouched.
    """

    def __init__(
        self,
        profile: AgentProfile,
        *,
        runs_root: Path = Path("/agent/runs"),
        query_factory: Callable[..., Any] | None = None,
    ):
        super().__init__(profile, runs_root=runs_root)
        self._query_factory = query_factory or self._sdk_query

    # ── Startup ────────────────────────────────────────────────────────────

    def prepare(self) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY") and not (Path.home() / ".claude").exists():
            logger.warning(
                f"[{self.profile.name}] no ANTHROPIC_API_KEY and no ~/.claude credentials — "
                "executor spawns will fail auth. Set the key or mount Claude Code credentials."
            )

    # ── Turn streaming ─────────────────────────────────────────────────────

    async def _stream_turn(
        self,
        prompt: str,
        *,
        run_id: str,
        thread_id: str | None,
        workspace: Path,
        first_turn: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        options = self._options(workspace, thread_id)
        async for message in self._query_factory(prompt, options):
            for payload in _normalize(message):
                yield payload

    def _options(self, workspace: Path, thread_id: str | None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "cwd": str(workspace),
            "permission_mode": _PERMISSION_MODES[self.spec.approval_policy],
            "resume": thread_id,
        }
        if self.spec.model:
            options["model"] = self.spec.model
        if self.spec.mcp_servers:
            servers: dict[str, dict[str, Any]] = {}
            for server in self.spec.mcp_servers:
                cfg: dict[str, Any] = {"type": "http", "url": server.url}
                if server.bearer_token_env:
                    token = os.environ.get(server.bearer_token_env)
                    if token:
                        cfg["headers"] = {"Authorization": f"Bearer {token}"}
                    else:
                        logger.warning(
                            f"[{self.profile.name}] mcp server '{server.name}': "
                            f"env var {server.bearer_token_env} unset — injecting without auth"
                        )
                servers[server.name] = cfg
            options["mcp_servers"] = servers
        return options

    def _sdk_query(self, prompt: str, options: dict[str, Any]) -> AsyncIterator[Any]:
        from claude_agent_sdk import ClaudeAgentOptions, query

        return query(prompt=prompt, options=ClaudeAgentOptions(**options))


def _normalize(message: Any) -> list[dict[str, Any]]:
    """Map one SDK message onto zero or more normalized event payloads.

    Matches on class name, not isinstance — the SDK is an optional extra and
    tests drive this with plain stand-in classes.
    """
    if isinstance(message, dict):  # test seam: already-normalized payloads
        return [message]

    name = type(message).__name__

    if name == "SystemMessage":
        if getattr(message, "subtype", None) == "init":
            data = getattr(message, "data", None) or {}
            return [{"type": "thread.started", "thread_id": data.get("session_id")}]
        return []

    if name == "AssistantMessage":
        payloads = []
        for block in getattr(message, "content", None) or []:
            block_name = type(block).__name__
            if block_name == "TextBlock":
                payloads.append({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": getattr(block, "text", "")},
                })
            elif block_name == "ToolUseBlock":
                payloads.append({
                    "type": "item.completed",
                    "item": {"type": "tool_use", "name": getattr(block, "name", None)},
                })
        return payloads

    if name == "ResultMessage":
        session_id = getattr(message, "session_id", None)
        prefix = [{"type": "thread.started", "thread_id": session_id}] if session_id else []
        if getattr(message, "is_error", False):
            return prefix + [{
                "type": "turn.failed",
                "error": {"message": getattr(message, "result", None) or "executor reported error"},
            }]
        raw_usage = getattr(message, "usage", None) or {}
        return prefix + [{
            "type": "turn.completed",
            "usage": {
                "input_tokens": raw_usage.get("input_tokens"),
                "output_tokens": raw_usage.get("output_tokens"),
            },
        }]

    return []  # UserMessage (tool results echoed back) and anything unknown
