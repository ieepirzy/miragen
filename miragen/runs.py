from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from miragen.models import RunRecord, RunSummary, RunUsage, ToolCallRecord

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

    def start(self, *, agent_name: str, trigger: str, prompt: str, use_history: bool = False) -> RunRecord:
        record = RunRecord(
            run_id=_new_run_id(),
            agent_name=agent_name,
            trigger=trigger,
            status="running",
            prompt=prompt[:_PROMPT_MAX],
            started_at=datetime.now(timezone.utc),
            use_history=use_history,
        )
        self._write(record)
        return record

    def finish(
        self,
        record: RunRecord,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        usage: RunUsage | None = None,
        tool_calls: Sequence[ToolCallRecord] = (),
    ) -> RunRecord:
        finished_at = datetime.now(timezone.utc)
        updated = record.model_copy(update={
            "status": status,
            "output": output[:_OUTPUT_MAX] if output is not None else None,
            "error": error,
            "finished_at": finished_at,
            "duration_s": (finished_at - record.started_at).total_seconds(),
            "usage": usage,
            "tool_calls": list(tool_calls),
        })
        self._write(updated)
        self._prune()
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

    Usage comes from result.usage(); tool calls are recovered by walking
    result.all_messages() for tool-call parts and pairing them with their
    return/retry parts via tool_call_id.
    """
    pai_usage = result.usage()
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
