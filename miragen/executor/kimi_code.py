"""Kimi Code adapter — runs jobs through the kimi-agent-sdk.

Same workspace-in / diff-and-events-out contract as Codex / Claude Code.
The SDK session id plays the thread_id role on the run record: resume re-opens
the session bound to the run via Session.resume(work_dir, session_id=...).

Auth (subscription-first, see docs/design/subscription-homes.md):
  - Primary: Kimi Code OAuth under KIMI_CODE_HOME (profile kimi_home),
    populated once by `miragen kimi-login` or the product login flow.
  - Fallback: KIMI_API_KEY / MOONSHOT_API_KEY (metered).
Agent containers never log in; they mount the shared home.

Leash: when enabled, yolo is off and each ApprovalRequest is mapped to a
GateOperation and answered approve/reject before the tool runs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from miragen.executor.base import ExecutorBackend
from miragen.executor.leash import GateOperation
from miragen.models import AgentProfile

logger = logging.getLogger("miragen.executor")

# Env vars the product / SDK treat as metered credentials.
_API_KEY_ENVS = ("KIMI_API_KEY", "MOONSHOT_API_KEY", "KIMI_CODE_API_KEY")


class KimiCodeExecutor(ExecutorBackend):
    """Runs jobs through kimi-agent-sdk (`pip install miragen[kimi-code]`).

    `session_factory` is the test seam: it receives
    `(prompt, *, thread_id, first_turn, options)` and returns an async
    iterator of Wire messages — or of already-normalized payload dicts, which
    pass through `_normalize` untouched.
    """

    def __init__(
        self,
        profile: AgentProfile,
        *,
        runs_root: Path = Path("/agent/runs"),
        session_factory: Callable[..., AsyncIterator[Any]] | None = None,
    ):
        super().__init__(profile, runs_root=runs_root)
        self._session_factory = session_factory or self._default_session

    # ── Startup ────────────────────────────────────────────────────────────

    def prepare(self) -> None:
        home = Path(self.spec.kimi_home)
        home.mkdir(parents=True, exist_ok=True)
        # Profile home wins over any inherited KIMI_CODE_HOME — setdefault would
        # leave a host/developer value in place and the SDK would read the wrong
        # store while we warn against the mounted volume (Codex review on #40).
        os.environ["KIMI_CODE_HOME"] = str(home)

        has_oauth = (home / "config.toml").exists() or any(home.iterdir()) if home.exists() else False
        # OAuth store layout can vary; presence of any home content after login
        # is a weak signal. Prefer explicit auth.json-style paths when known,
        # but always accept a configured API key as sufficient.
        has_key = any(os.environ.get(k) for k in _API_KEY_ENVS)
        if not has_key and not has_oauth:
            logger.warning(
                f"[{self.profile.name}] no credentials in {home} and no "
                f"{'/'.join(_API_KEY_ENVS)} — executor turns will fail auth. "
                "Run `miragen kimi-login --kimi-home <shared volume>` once "
                "(subscription) or set an API key. Agent containers never log "
                "in — they mount the shared store. See docs/design/subscription-homes.md."
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
        options = self._options(workspace)
        session = self._session_factory(
            prompt,
            thread_id=thread_id,
            first_turn=first_turn,
            options=options,
        )
        async for message in session:
            for payload in _normalize(message):
                yield payload

    def _options(self, workspace: Path) -> dict[str, Any]:
        # Leash on: must see ApprovalRequests → yolo off. Unattended default
        # (approval_policy never, no leash) uses yolo so the turn never stalls.
        yolo = (not self.leash_enabled) and self.spec.approval_policy == "never"
        options: dict[str, Any] = {
            "work_dir": str(workspace),
            "yolo": yolo,
            "model": self.spec.model,
        }
        if self.spec.mcp_servers:
            configs: list[dict[str, Any]] = []
            for server in self.spec.mcp_servers:
                # OpenAI-compatible / Kimi MCP config: transport url + optional headers.
                cfg: dict[str, Any] = {
                    "name": server.name,
                    "type": "http",
                    "url": server.url,
                }
                if server.bearer_token_env:
                    token = os.environ.get(server.bearer_token_env)
                    if token:
                        cfg["headers"] = {"Authorization": f"Bearer {token}"}
                    else:
                        logger.warning(
                            f"[{self.profile.name}] mcp server '{server.name}': "
                            f"env var {server.bearer_token_env} unset — injecting without auth"
                        )
                configs.append(cfg)
            options["mcp_configs"] = configs
        return options

    async def _default_session(
        self,
        prompt: str,
        *,
        thread_id: str | None,
        first_turn: bool,
        options: dict[str, Any],
    ) -> AsyncIterator[Any]:
        import asyncio

        from kaos.path import KaosPath
        from kimi_agent_sdk import ApprovalRequest, RunCancelled, Session

        work_dir = KaosPath(options["work_dir"])
        kwargs: dict[str, Any] = {
            "yolo": options["yolo"],
        }
        if options.get("model"):
            kwargs["model"] = options["model"]
        if options.get("mcp_configs"):
            kwargs["mcp_configs"] = options["mcp_configs"]

        session: Session | None
        if thread_id:
            session = await Session.resume(work_dir, thread_id, **kwargs)
            if session is None:
                yield {
                    "type": "turn.failed",
                    "error": {"message": f"kimi session {thread_id!r} not found for resume"},
                }
                return
        else:
            session = await Session.create(work_dir=work_dir, **kwargs)

        assert session is not None
        # Emit resume handle before streaming so a cancelled turn still recovers it.
        yield {"type": "thread.started", "thread_id": session.id}

        last_usage: dict[str, Any] = {}
        try:
            async for msg in session.prompt(prompt):
                if type(msg).__name__ == "ApprovalRequest" or isinstance(msg, ApprovalRequest):
                    self._handle_approval(msg)
                    continue
                if type(msg).__name__ == "StatusUpdate":
                    # Fold interim token counters into the single terminal
                    # turn.completed — base overwrites usage on every such
                    # event, so mid-stream empties would wipe good numbers.
                    folded = _usage_from_status(msg)
                    if folded:
                        last_usage = folded
                    continue
                yield msg
            yield {"type": "turn.completed", "usage": last_usage}
        except asyncio.CancelledError:
            # Real task cancellation (app-tier turn_timeout_s / client cancel).
            # Must propagate as CancelledError so timeout → suspended and the run
            # record is finished by the app tier — never swallow into 'failed'.
            session.cancel()
            raise
        except RunCancelled as e:
            # Product-side cancel independent of the outer task (agent hit a
            # product cancel path). Mapping this to CancelledError would skip
            # every Exception handler and leave the run record stuck at
            # 'running' — emit a normalized failure instead (Codex review #40).
            yield {
                "type": "turn.failed",
                "error": {"message": f"kimi run cancelled: {e}"},
            }
        except Exception as e:
            yield {"type": "turn.failed", "error": {"message": str(e)}}
        finally:
            await session.close()

    def _handle_approval(self, req: Any) -> None:
        """Answer one ApprovalRequest via the host leash (or auto-approve)."""
        if not self.leash_enabled:
            # yolo should have swallowed these; fail-open if one still arrives.
            req.resolve("approve")
            return
        op = _gate_operation(req)
        if op is None:
            req.resolve("approve")
            return
        decision = self.gate_decide(op)
        req.resolve("approve" if decision.allow else "reject")


# Shell / write / network heuristics for Kimi tool names and action strings.
_COMMAND_ACTIONS = {"run shell command", "shell", "bash", "execute"}
_WRITE_ACTIONS = {"write", "edit", "create file", "modify file", "file edit"}
_NETWORK_ACTIONS = {"web fetch", "web search", "fetch", "http"}


def _gate_operation(req: Any) -> GateOperation | None:
    action = (getattr(req, "action", None) or "").strip().lower()
    description = getattr(req, "description", None) or ""
    sender = (getattr(req, "sender", None) or "").lower()
    summary = (description or action or sender)[:120]

    if action in _COMMAND_ACTIONS or "shell" in sender or "bash" in sender:
        return GateOperation(op_class="command", command=description, summary=summary)
    if action in _WRITE_ACTIONS or "write" in sender or "edit" in sender:
        return GateOperation(op_class="write", summary=summary)
    if action in _NETWORK_ACTIONS or "fetch" in sender or "web" in sender:
        return GateOperation(op_class="network", summary=summary)
    # Unknown tool — fail-open (never stall unattended runs on unclassifiable).
    return None


def _normalize(message: Any) -> list[dict[str, Any]]:
    """Map one Wire message / stand-in onto zero or more normalized payloads.

    Matches on class name, not isinstance — the SDK is an optional extra and
    tests drive this with plain stand-in classes. Plain dicts pass through.
    """
    if isinstance(message, dict):
        return [message]

    name = type(message).__name__

    if name == "TextPart":
        text = getattr(message, "text", "") or ""
        if not text:
            return []
        return [{
            "type": "item.completed",
            "item": {"type": "agent_message", "text": text},
        }]

    if name == "ToolCall":
        fn = getattr(message, "function", None)
        tool_name = getattr(fn, "name", None) if fn is not None else getattr(message, "name", None)
        return [{
            "type": "item.completed",
            "item": {"type": "tool_use", "name": tool_name},
        }]

    if name == "StatusUpdate":
        # Production path folds StatusUpdate in _default_session. For the
        # test seam (and any caller that streams raw Wire through _normalize),
        # emit a turn.completed only when token numbers are present.
        usage = _usage_from_status(message)
        if usage is None:
            return []
        return [{"type": "turn.completed", "usage": usage}]

    if name == "TurnEnd":
        # Terminal boundary without usage — production emits turn.completed
        # after the stream with the last folded StatusUpdate usage.
        return []

    # Control / thinking / tool results — no base-tier payload.
    return []


def _usage_from_status(message: Any) -> dict[str, Any] | None:
    raw = getattr(message, "token_usage", None)
    if raw is None:
        return None
    # TokenUsage: input_other / output / input_cache_read
    input_tokens = getattr(raw, "input_other", None)
    if input_tokens is None and isinstance(raw, dict):
        input_tokens = raw.get("input_other") or raw.get("input_tokens")
    output_tokens = getattr(raw, "output", None)
    if output_tokens is None and isinstance(raw, dict):
        output_tokens = raw.get("output") or raw.get("output_tokens")
    cached = getattr(raw, "input_cache_read", None)
    if cached is None and isinstance(raw, dict):
        cached = raw.get("input_cache_read") or raw.get("cached_input_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cached is not None:
        usage["cached_input_tokens"] = cached
    return usage
