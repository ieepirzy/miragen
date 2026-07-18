"""Executor backend tier — abstract contract + shared machinery.

Contract: workspace-in / diff-and-events-out. The executor owns its own agent
loop; miragen owes the layers above a sufficient event stream (turn events,
tool/item events, exit reason) and a diff harvested EXACTLY ONCE on terminal
success. Tool-level failures inside the loop are the agent's problem and are
not modeled here.

State machine (design record: miradb #293; refinement plan:
docs/design/executor-tier-refinement.md):

    running -> succeeded            (diff harvested)
            -> suspended  (budget)  RESUMABLE
            -> suspended  (timeout) RESUMABLE (wall-clock, enforced by app tier)
            -> failed     (crash)   RESUMABLE
            -> abandoned            (human gave up; only human-terminal state)

Workspace lifetime != container lifetime: the workspace is a persistent
volume keyed to the agent-run, alongside the executor thread_id on the run
record. The container is disposable; resume re-opens the thread bound to the
run and reuses its workspace.

Adapters implement `_stream_turn()`, an async generator of NORMALIZED event
payloads (dicts). The base class owns everything backend-agnostic: workspace
baseline, events.jsonl persistence, payload parsing, budget check, and the
diff harvest. Payload kinds the base parser understands:

    thread.started   {"thread_id": ...}          resume handle
    turn.completed   {"usage": {input_tokens, output_tokens}}
    turn.failed      {"error": ...}              -> failed (resumable crash)
    item.completed   {"item": {"type": "agent_message", "text": ...}} -> output

Cancellation contract: `run_job()` may be cancelled at any await point (the
app tier enforces `turn_timeout_s` via asyncio.wait_for). Adapters must let
CancelledError propagate (never swallow it into a 'failed' result) and must
release their underlying process/session on the way out; the base class
guarantees the events file is flushed and closed.
"""

from __future__ import annotations

import json
import logging
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from miragen.models import AgentProfile, ExecutorSpec, RunUsage, sum_usage

logger = logging.getLogger("miragen.executor")

_DIFF_NAME = "diff.patch"
_EVENTS_SUFFIX = ".events.jsonl"
_BASELINE_TAG = "miragen-baseline"


class ExecutorResult:
    """Terminal outcome of one executor turn bound to an agent-run."""

    def __init__(
        self,
        *,
        status: str,
        exit_reason: str | None = None,
        thread_id: str | None = None,
        output: str | None = None,
        error: str | None = None,
        usage: RunUsage | None = None,
        diff_path: str | None = None,
    ):
        self.status = status
        self.exit_reason = exit_reason
        self.thread_id = thread_id
        self.output = output
        self.error = error
        self.usage = usage
        self.diff_path = diff_path


class ExecutorBackend(ABC):
    """Backend-agnostic half of the executor contract.

    Owns workspace prep, the events.jsonl sink, payload parsing, the per-run
    budget check, and the baseline-tag diff harvest. Concrete adapters own
    only how to start/resume their agent and how to map its native event
    stream into normalized payloads.
    """

    def __init__(self, profile: AgentProfile, *, runs_root: Path = Path("/agent/runs")):
        assert profile.executor is not None, "executor backends require an executor-tier profile"
        self.profile = profile
        self.spec: ExecutorSpec = profile.executor
        self.runs_root = Path(runs_root)

    # ── Startup ────────────────────────────────────────────────────────────

    def prepare(self) -> None:
        """Startup-time config (idempotent). Default: nothing to write."""

    # ── Adapter seam ───────────────────────────────────────────────────────

    @abstractmethod
    def _stream_turn(
        self,
        prompt: str,
        *,
        run_id: str,
        thread_id: str | None,
        workspace: Path,
        first_turn: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one turn and yield normalized event payloads (see module doc)."""

    # ── Job execution (template) ───────────────────────────────────────────

    async def run_job(
        self,
        prompt: str,
        run_id: str,
        *,
        thread_id: str | None = None,
        workspace: str | None = None,
        first_turn: bool = True,
        prior_usage: RunUsage | None = None,
    ) -> ExecutorResult:
        """Execute one turn of a job bound to an agent-run.

        First turn: fresh workspace + fresh thread; `instructions` are
        prepended. Resume: existing workspace + adapter-side thread resume.
        `prior_usage` is the run's accumulated usage from earlier turns —
        needed so a per-run budget check on resume sees the true total, not
        just this turn's usage.
        """
        ws = Path(workspace) if workspace else Path(self.spec.workspace_root) / run_id

        if first_turn and self.spec.instructions:
            prompt = f"{self.spec.instructions}\n\n{prompt}"

        events_path = self._events_path(run_id)
        events_path.parent.mkdir(parents=True, exist_ok=True)

        usage: RunUsage | None = None
        output: str | None = None
        error: str | None = None
        observed_thread_id: str | None = thread_id
        turn_failed = False

        try:
            # Inside the try: a bad workspace_root, missing git, or a
            # permission error here must still produce a 'failed' result
            # (and thus a RunStore.finish() call) rather than an unhandled
            # exception that leaves the run record stuck at 'running'.
            self._prepare_workspace(ws)
            with events_path.open("a") as sink:
                async for payload in self._stream_turn(
                    prompt,
                    run_id=run_id,
                    thread_id=thread_id,
                    workspace=ws,
                    first_turn=first_turn,
                ):
                    payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
                    sink.write(json.dumps(payload, default=str) + "\n")

                    kind = payload.get("type")
                    if kind == "thread.started":
                        observed_thread_id = payload.get("thread_id") or observed_thread_id
                    elif kind == "turn.completed":
                        usage = _usage_from_payload(payload.get("usage"))
                    elif kind == "turn.failed":
                        turn_failed = True
                        error = json.dumps(payload.get("error")) if payload.get("error") else "turn failed"
                    elif kind == "item.completed":
                        item = payload.get("item") or {}
                        if item.get("type") in ("agent_message", "agent-message"):
                            output = item.get("text") or output
        except Exception as e:
            # Crash / API error: resumable. Partial workspace changes are
            # neither harvested nor discarded — they are resume state.
            # (CancelledError is a BaseException and deliberately NOT caught:
            # the app-tier timeout owns that path.)
            logger.error(f"[{self.profile.name}] executor crashed: {e}", exc_info=True)
            return ExecutorResult(
                status="failed",
                exit_reason="crash",
                thread_id=observed_thread_id,
                error=str(e),
                usage=usage,
            )

        if turn_failed:
            return ExecutorResult(
                status="failed",
                exit_reason="crash",
                thread_id=observed_thread_id,
                error=error,
                usage=usage,
            )

        if self._budget_exhausted(sum_usage(prior_usage, usage)):
            # Turn completed but blew the per-run budget: suspend, keep the
            # partial diff as resume state, do not harvest. Checked against
            # the run's cumulative usage (prior turns + this one) — a resumed
            # run must not be able to sneak back under a per-run cap just
            # because a single resumed turn's own usage looks small.
            return ExecutorResult(
                status="suspended",
                exit_reason="budget",
                thread_id=observed_thread_id,
                output=output,
                usage=usage,
            )

        diff_path = await self._harvest_diff(ws)
        return ExecutorResult(
            status="succeeded",
            thread_id=observed_thread_id,
            output=output,
            usage=usage,
            diff_path=diff_path,
        )

    # ── Events ─────────────────────────────────────────────────────────────

    def _events_path(self, run_id: str) -> Path:
        return self.runs_root / f"{run_id}{_EVENTS_SUFFIX}"

    def read_events(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        path = self._events_path(run_id)
        if not path.exists():
            return []
        events = []
        with path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        return events[-limit:]

    def latest_thread_id(self, run_id: str) -> str | None:
        """Recover the resume handle from the persisted event stream — used by
        the app tier when a turn is cancelled (timeout) before run_job could
        return a result carrying the thread_id."""
        for event in reversed(self.read_events(run_id, limit=1000)):
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return event["thread_id"]
        return None

    # ── Workspace + harvest ────────────────────────────────────────────────

    def _prepare_workspace(self, ws: Path) -> None:
        ws.mkdir(parents=True, exist_ok=True)
        if not (ws / ".git").exists():
            # The diff IS the deliverable; a git baseline makes harvest exact.
            # Tagged (not just left as a loose commit) so harvest can diff
            # against it even after the executor makes its own commits —
            # the tag lives in .git, so the executor's own `git add -A` /
            # commits never touch or shadow it.
            _git(ws, "init", "-q")
            _git(ws, "add", "-A")
            _git(ws, "-c", "user.email=miragen@local", "-c", "user.name=miragen",
                 "commit", "-q", "--allow-empty", "-m", "miragen: workspace baseline")
            _git(ws, "tag", "-f", _BASELINE_TAG)

    async def _harvest_diff(self, ws: Path) -> str:
        """git-diff harvest, exactly once, on terminal success (§293 decision 3).

        Diffs against the baseline tag, not HEAD: if the executor committed
        mid-turn, HEAD has moved past the baseline, and `git diff --cached`
        with no revision arg would only show the *uncommitted* remainder,
        silently dropping everything the executor already committed.
        """
        _git(ws, "add", "-A")  # stage everything so new files appear in the diff
        diff = _git(ws, "diff", "--cached", "--binary", _BASELINE_TAG)
        marker_dir = ws / ".miragen"
        marker_dir.mkdir(exist_ok=True)
        diff_path = marker_dir / _DIFF_NAME
        diff_path.write_text(diff)
        return str(diff_path)

    # ── Budget ─────────────────────────────────────────────────────────────

    def _budget_exhausted(self, usage: RunUsage | None) -> bool:
        limits = self.profile.limits
        if limits is None or limits.tokens_per_run is None or usage is None:
            return False
        total = (usage.input_tokens or 0) + (usage.output_tokens or 0)
        return total > limits.tokens_per_run


def event_payload(event: Any) -> dict[str, Any]:
    """Normalize an SDK event (pydantic model, dict, or opaque object) into a
    JSON-able payload dict, stamping a ts if the event carries none."""
    if isinstance(event, dict):
        payload = dict(event)
    elif hasattr(event, "model_dump"):
        payload = event.model_dump(by_alias=False)
    else:
        payload = {"type": "unknown", "repr": repr(event)}
    payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
    return payload


def _usage_from_payload(raw: Any) -> RunUsage | None:
    if not isinstance(raw, dict):
        return None
    return RunUsage(
        requests=1,
        input_tokens=raw.get("input_tokens"),
        output_tokens=raw.get("output_tokens"),
    )


def _git(ws: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ws), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout
