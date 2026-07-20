from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from miragen.models import (
    InterventionRequest,
    RepositoryRevision,
    RunProvenance,
    RunRecord,
    RunSummary,
    RunUsage,
    ToolCallRecord,
)

_PROMPT_MAX = 20_000
_OUTPUT_MAX = 100_000
_ARGS_MAX = 2_000


class AmbiguousRunIdError(Exception):
    """Raised by RunStore.get() when a prefix matches more than one run."""

    def __init__(self, candidates: list[str]):
        self.candidates = candidates
        super().__init__(f"ambiguous run_id prefix, {len(candidates)} candidates: {candidates}")


class RunStore:
    """
    Persists one JSON file per agent run under `root`.

    Files are named `<started_at:%Y%m%dT%H%M%S>Z-<run_id[:8]>.json` — lexicographic
    filename order is chronological order, so listing needs no index. Writes are
    atomic (write to a .tmp file, then os.replace).
    """

    def __init__(self, root: Path = Path("/agent/runs"), retention: int = 200):
        self.root = Path(root)
        self.retention = retention

    def start(
        self,
        *,
        agent_name: str,
        trigger: str,
        prompt: str,
        use_history: bool = False,
        executor: str | None = None,
        model: str | None = None,
        snapshot_sha256: str | None = None,
        provenance: RunProvenance | None = None,
        repositories: list[RepositoryRevision] | None = None,
    ) -> RunRecord:
        record = RunRecord(
            run_id=_new_run_id(),
            agent_name=agent_name,
            trigger=trigger,
            status="running",
            prompt=prompt[:_PROMPT_MAX],
            started_at=datetime.now(timezone.utc),
            use_history=use_history,
            executor=executor,
            model=model,
            snapshot_sha256=snapshot_sha256,
            provenance=provenance,
            repositories=repositories,
        )
        self._write(record)
        return record

    def find_by_idempotency_key(self, key: str) -> RunRecord | None:
        """Newest run whose provenance carries `key`. This is the launch
        dedupe lookup: POST /executor-runs persists the key inside provenance
        before acknowledging, so a retried launch (including one retried after
        an ambiguous response or a process crash) recovers the original run
        instead of launching twice."""
        for path in sorted(self._existing_files(), reverse=True):
            record = _read_record(path)
            if record is None or record.provenance is None:
                continue
            if record.provenance.idempotency_key == key:
                return record
        return None

    def finish(
        self,
        record: RunRecord,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        usage: RunUsage | None = None,
        tool_calls: Sequence[ToolCallRecord] = (),
        thread_id: str | None = None,
        workspace: str | None = None,
        exit_reason: str | None = None,
        diff_path: str | None = None,
        repositories: list[RepositoryRevision] | None = None,
        setup_s: float | None = None,
        tool_call_count: int | None = None,
        tool_call_failures: int | None = None,
        pending_intervention: InterventionRequest | None = None,
    ) -> RunRecord:
        """Telemetry params (`setup_s`, tool-call counts) are the run's
        ACCUMULATED values — callers sum across turns before passing; None
        keeps whatever the record already carries (an executor that can't
        report a metric never zeroes it)."""
        finished_at = datetime.now(timezone.utc)
        duration_s = (finished_at - record.started_at).total_seconds()
        updated = record.model_copy(update={
            "status": status,
            "output": output[:_OUTPUT_MAX] if output is not None else None,
            "error": error,
            "finished_at": finished_at,
            "duration_s": duration_s,
            # active = wall clock minus time spent suspended awaiting a human
            # (blocked_s accumulates in reopen()).
            "active_s": duration_s - (record.blocked_s or 0),
            "usage": usage,
            "tool_calls": list(tool_calls),
            # Executor-tier handles: never clear an already-recorded value —
            # thread/workspace bindings survive every state transition.
            "thread_id": thread_id or record.thread_id,
            "workspace": workspace or record.workspace,
            "exit_reason": exit_reason,
            "diff_path": diff_path or record.diff_path,
            "repositories": repositories or record.repositories,
            "setup_s": setup_s if setup_s is not None else record.setup_s,
            "tool_call_count": tool_call_count if tool_call_count is not None else record.tool_call_count,
            "tool_call_failures": tool_call_failures if tool_call_failures is not None else record.tool_call_failures,
            # set on suspension (exit_reason 'intervention'); cleared by reopen()
            "pending_intervention": pending_intervention
            if pending_intervention is not None else record.pending_intervention,
        })
        self._write(updated)
        self._prune()
        return updated

    def annotate(self, record: RunRecord, **updates) -> RunRecord:
        """Write advisory fields onto an already-finished record (e.g. the
        artifact-sink outcome) without touching status or timing."""
        updated = record.model_copy(update=updates)
        self._write(updated)
        return updated

    def reopen(self, record: RunRecord) -> RunRecord:
        """Executor resume: transition a suspended/failed run back to running,
        preserving its thread/workspace bindings and accumulated usage.
        Telemetry: counts the resume and accumulates the blocked interval —
        the time this run sat suspended/failed awaiting a human decision."""
        blocked_s = record.blocked_s
        if record.finished_at is not None:
            blocked_s = (blocked_s or 0) + (
                datetime.now(timezone.utc) - record.finished_at
            ).total_seconds()
        updated = record.model_copy(update={
            "status": "running",
            "finished_at": None,
            "duration_s": None,
            "active_s": None,
            "error": None,
            "exit_reason": None,
            "resume_count": record.resume_count + 1,
            "blocked_s": blocked_s,
            # answered or superseded either way — the event stream keeps history
            "pending_intervention": None,
        })
        self._write(updated)
        return updated

    def get(self, run_id: str) -> RunRecord | None:
        """Look up a run by full id or unique prefix. Raises AmbiguousRunIdError on a
        prefix matching more than one run."""
        matches: list[RunRecord] = []
        for record in self._iter_records():
            if record.run_id == run_id:
                return record
            if record.run_id.startswith(run_id):
                matches.append(record)
        if not matches:
            return None
        if len(matches) > 1:
            raise AmbiguousRunIdError([r.run_id for r in matches])
        return matches[0]

    def list(self, *, limit: int = 20, status: str | None = None) -> list[RunSummary]:
        """Newest-first run summaries, optionally filtered by status."""
        summaries = []
        for path in sorted(self._existing_files(), reverse=True):
            record = _read_record(path)
            if record is None:
                continue
            if status is not None and record.status != status:
                continue
            summaries.append(RunSummary.from_record(record))
            if len(summaries) >= limit:
                break
        return summaries

    def sweep_interrupted(self) -> int:
        """Rewrite any record still `running` (from a killed process) as `interrupted`."""
        count = 0
        for record in self._iter_records():
            if record.status == "running":
                updated = record.model_copy(update={
                    "status": "interrupted",
                    "finished_at": datetime.now(timezone.utc),
                })
                self._write(updated)
                count += 1
        return count

    # ── Run snapshots ─────────────────────────────────────────────────────────
    #
    # The immutable resolved EDF snapshot for a launched run (issue #33 Phase
    # A). Stored under a subdirectory so the `*.json` run-record glob (and its
    # retention prune) never sees snapshot files. Like events.jsonl, snapshots
    # are not pruned with run records — they are cheap and provenance-bearing.

    def write_snapshot(self, run_id: str, snapshot: dict) -> Path:
        snapshot_dir = self.root / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = snapshot_dir / f"{run_id}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(snapshot))
        os.replace(tmp, path)
        return path

    def read_snapshot(self, run_id: str) -> dict | None:
        path = self.root / "snapshots" / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    # ── Internals ─────────────────────────────────────────────────────────────

    def _existing_files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return list(self.root.glob("*.json"))

    def _iter_records(self):
        for path in sorted(self._existing_files()):
            record = _read_record(path)
            if record is not None:
                yield record

    def _path_for(self, record: RunRecord) -> Path:
        ts = record.started_at.strftime("%Y%m%dT%H%M%S")
        return self.root / f"{ts}Z-{record.run_id[:8]}.json"

    def _write(self, record: RunRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path_for(record)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(record.model_dump_json())
        os.replace(tmp, path)

    def _prune(self) -> None:
        files = sorted(self._existing_files())
        excess = len(files) - self.retention
        for path in files[:max(excess, 0)]:
            path.unlink(missing_ok=True)


def tokens_used_since(store: RunStore, since: datetime) -> int:
    """
    Sum input+output tokens across `store`'s run records with started_at >= since.

    Records with no usage (e.g. a run that failed before the model responded)
    contribute 0 — unknown usage counts as 0, not as unbounded.
    """
    total = 0
    for record in store._iter_records():
        if record.started_at < since or record.usage is None:
            continue
        total += (record.usage.input_tokens or 0) + (record.usage.output_tokens or 0)
    return total


def _new_run_id() -> str:
    return uuid.uuid4().hex


def _read_record(path: Path) -> RunRecord | None:
    try:
        return RunRecord.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return None


def extract_run_details(result: Any) -> tuple[RunUsage, list[ToolCallRecord]]:
    """
    Extract usage and a tool-call trace from a PydanticAI AgentRunResult.

    Usage comes from result.usage (a property since pydantic-ai's AgentRunResult
    stopped exposing it as a callable); tool calls are recovered by walking
    result.all_messages() for tool-call parts and pairing them with their
    return/retry parts via tool_call_id.
    """
    pai_usage = result.usage
    usage = RunUsage(
        requests=pai_usage.requests,
        input_tokens=pai_usage.input_tokens or None,
        output_tokens=pai_usage.output_tokens or None,
    )

    calls: dict[str, dict[str, str]] = {}
    outcomes: dict[str, bool] = {}

    for message in result.all_messages():
        for part in getattr(message, "parts", []):
            kind = getattr(part, "part_kind", None)
            if kind == "tool-call":
                args = part.args if isinstance(part.args, str) else json.dumps(part.args or {})
                calls[part.tool_call_id] = {"tool_name": part.tool_name, "args": args}
            elif kind == "tool-return":
                outcomes[part.tool_call_id] = part.outcome == "success"
            elif kind == "retry-prompt" and part.tool_call_id in calls:
                outcomes[part.tool_call_id] = False

    tool_calls = [
        ToolCallRecord(
            tool_name=call["tool_name"],
            args=call["args"][:_ARGS_MAX],
            ok=outcomes.get(call_id, True),
        )
        for call_id, call in calls.items()
    ]

    return usage, tool_calls


def run_retention_from_env() -> int:
    return int(os.environ.get("MIRAGEN_RUN_RETENTION", 200))


def simplify_history_messages(messages: Sequence[Any]) -> list[dict[str, str]]:
    """
    Flatten PydanticAI ModelMessages into plain {"role", "content"} dicts for the
    GET /history API, walking parts the same way extract_run_details does.
    """
    simplified: list[dict[str, str]] = []
    for message in messages:
        if getattr(message, "kind", None) == "response":
            texts = []
            for part in getattr(message, "parts", []):
                kind = getattr(part, "part_kind", None)
                if kind == "text":
                    texts.append(str(part.content))
                elif kind == "tool-call":
                    args = part.args if isinstance(part.args, str) else json.dumps(part.args or {})
                    texts.append(f"[tool_call: {part.tool_name}({args})]")
            simplified.append({"role": "assistant", "content": "\n".join(t for t in texts if t)})
            continue

        role = "user"
        texts = []
        for part in getattr(message, "parts", []):
            kind = getattr(part, "part_kind", None)
            if kind == "system-prompt":
                role = "system"
            elif kind == "tool-return":
                role = "tool"
            if kind in ("system-prompt", "user-prompt", "tool-return", "retry-prompt"):
                texts.append(str(part.content))
        simplified.append({"role": role, "content": "\n".join(t for t in texts if t)})
    return simplified
