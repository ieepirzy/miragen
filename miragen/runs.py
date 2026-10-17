from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from miragen.models import (
    CallUsage,
    InterventionRequest,
    RepositoryRevision,
    RunProvenance,
    RunRecord,
    RunSummary,
    RunUsage,
    ToolCallRecord,
    budget_tokens,
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
        instance: str | None = None,
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
            instance=instance,
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
            # Only launch runs own the idempotency namespace. Managed-schedule
            # (and other) provenance is stored verbatim and may echo a key it
            # doesn't own; matching those would let an unrelated run shadow a
            # real launch and make it return duplicate:true without dispatching.
            if record.trigger != "launch":
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
        affected_repositories: list[str] | None = None,
        change_categories: list[str] | None = None,
        setup_s: float | None = None,
        tool_call_count: int | None = None,
        tool_call_failures: int | None = None,
        pending_intervention: InterventionRequest | None = None,
        timings: dict | None = None,
    ) -> RunRecord:
        """Telemetry params (`setup_s`, tool-call counts) are the run's
        ACCUMULATED values — callers sum across turns before passing; None
        keeps whatever the record already carries (an executor that can't
        report a metric never zeroes it)."""
        finished_at = datetime.now(timezone.utc)
        duration_s = (finished_at - record.started_at).total_seconds()
        # A resumed run keeps its first started_at (reopen): the gap to this
        # turn would include the earlier turns and the blocked time.
        if not record.resume_count:
            timings = _with_dispatch(timings, record.started_at)
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
            # Outcome classification is only written when the caller supplies
            # it (successful harvest). Keep prior values on non-harvest finishes.
            "affected_repositories": (
                affected_repositories
                if affected_repositories is not None
                else record.affected_repositories
            ),
            "change_categories": (
                change_categories
                if change_categories is not None
                else record.change_categories
            ),
            "setup_s": setup_s if setup_s is not None else record.setup_s,
            "tool_call_count": tool_call_count if tool_call_count is not None else record.tool_call_count,
            "tool_call_failures": tool_call_failures if tool_call_failures is not None else record.tool_call_failures,
            # set on suspension (exit_reason 'intervention'); cleared by reopen()
            "pending_intervention": pending_intervention
            if pending_intervention is not None else record.pending_intervention,
            # The finishing turn's own (None clears an earlier turn's).
            "timings": timings,
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

    def list(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        trigger: str | None = None,
    ) -> list[RunSummary]:
        """Newest-first run summaries, optionally filtered by status/trigger."""
        summaries = []
        for path in sorted(self._existing_files(), reverse=True):
            record = _read_record(path)
            if record is None:
                continue
            if status is not None and record.status != status:
                continue
            if trigger is not None and record.trigger != trigger:
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


def _budget_model(record: RunRecord, model: str | None) -> str | None:
    # The model the run recorded, so a later profile change doesn't re-weight
    # earlier runs; the profile's model (`model`) for records without one.
    # The rates themselves are always the source's current table.
    return record.model or model


def tokens_used_since(store: RunStore, since: datetime, model: str | None = None) -> int:
    """
    Sum budget tokens (models.budget_tokens: uncached input + output, cache
    reads and writes weighted by the model's rates) across `store`'s run
    records with started_at >= since. Usage without a per-call breakdown is
    weighted by the record's own model, else `model` (the profile's;
    models.budget_model), at the source's CURRENT rates: today's spend
    follows the rates table as it is now, not as it was at each run.

    Records with no usage (e.g. a run that failed before the model responded,
    or a fresh `running` record that hasn't finished a turn yet) contribute 0
    — unknown usage counts as 0, not as unbounded. A *resumed* run
    (`RunStore.reopen()`) is `running` again but keeps the `usage` it
    accumulated before suspension, so it is NOT unknown here — this already
    counts it. Callers enforcing a daily budget need `reserved_tokens_in_flight`
    alongside this to also account for runs that haven't finished a turn yet.
    """
    total = 0
    for record in store._iter_records():
        if record.started_at < since or record.usage is None:
            continue
        total += budget_tokens(record.usage, _budget_model(record, model))
    return total


def reserved_tokens_in_flight(
    store: RunStore, since: datetime, per_run_reserve: int | None, remaining_budget: int,
    model: str | None = None,
) -> int:
    """
    Estimated additional token cost of runs started >= since that are still
    `running` — on top of whatever `tokens_used_since` already counted for them.

    `tokens_used_since` only sees usage recorded so far, so a burst of
    concurrent run requests issued before any of them record usage would each
    read the same not-yet-exceeded total and all pass a daily-budget check —
    see miragen#58. Each in-flight run reserves up to `per_run_reserve` tokens
    (a profile's `limits.tokens_per_run`, its own worst-case-per-run bound)
    when one is configured, MINUS whatever usage it has already recorded — a
    resumed run (`RunStore.reopen()`) keeps its prior accumulated usage across
    the status flip back to `running`, and `tokens_used_since` already counts
    that; reserving the full `per_run_reserve` again on top of it would double
    -count and falsely exceed the budget for unrelated runs. Without
    `per_run_reserve` there is no bound to size a reservation on at all, so a
    single in-flight run reserves the entire `remaining_budget` instead — the
    conservative choice, since its real cost is unknown and could be anything
    up to the whole budget.
    """
    total = 0
    for record in store._iter_records():
        if record.started_at < since or record.status != "running":
            continue
        if per_run_reserve is None:
            return max(remaining_budget, 0)
        # The same weighting as tokens_used_since, or the two won't add up.
        already_used = budget_tokens(record.usage, _budget_model(record, model))
        total += max(per_run_reserve - already_used, 0)
    return total


def _new_run_id() -> str:
    return uuid.uuid4().hex


def _read_record(path: Path) -> RunRecord | None:
    try:
        return RunRecord.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return None


# Provider usage keys pydantic-ai copies into `details` that prove the
# provider reported the cache bucket at all (so a 0 is a real zero).
_READ_KEYS = ("cache_read_input_tokens", "prompt_cache_hit_tokens")
_WRITE_KEYS = ("cache_creation_input_tokens",)


def _pai_cache_reads(pai_usage: Any) -> int | None:
    details = getattr(pai_usage, "details", None) or {}
    reads = getattr(pai_usage, "cache_read_tokens", 0) or 0
    if not reads:
        # DeepSeek's own name for cache hits, which pydantic-ai's usage
        # extractor only maps when the response also carries cached_tokens.
        reads = details.get("prompt_cache_hit_tokens") or 0
    # pydantic-ai defaults the counter to 0: a 0 with no provider key behind
    # it is "not reported", not "no cache reads".
    return reads if reads or any(k in details for k in _READ_KEYS) else None


def _pai_cache_writes(pai_usage: Any) -> int | None:
    details = getattr(pai_usage, "details", None) or {}
    writes = getattr(pai_usage, "cache_write_tokens", 0) or 0
    return writes if writes or any(k in details for k in _WRITE_KEYS) else None


def _pai_cache_write_1h(pai_usage: Any) -> int | None:
    """The 1-hour part of the cache writes, only when pydantic-ai passed it
    through in `details` (it does not for Anthropic as of 1.106: its usage
    details keep only the flat counters, so the split is unknown → None)."""
    details = getattr(pai_usage, "details", None) or {}
    for key in ("cache_write_1h_tokens", "ephemeral_1h_input_tokens"):
        if isinstance(details.get(key), int):
            return details[key]
    return None


def _pai_token_fields(pai_usage: Any) -> dict[str, int | None]:
    """RunUsage/CallUsage token fields from a pydantic-ai usage object.

    pydantic-ai (via its usage extractor) already follows our convention: its
    input_tokens is the whole prompt including cache reads and writes, with
    cache_read_tokens/cache_write_tokens as subsets. All-zero usage means the
    provider's usage could not be extracted, so nothing is reported."""
    input_tokens = getattr(pai_usage, "input_tokens", 0) or 0
    output_tokens = getattr(pai_usage, "output_tokens", 0) or 0
    if not input_tokens and not output_tokens:
        return {}
    writes = _pai_cache_writes(pai_usage)
    return {
        "input_tokens": input_tokens or None,
        "output_tokens": output_tokens or None,
        "cached_input_tokens": _pai_cache_reads(pai_usage),
        "cache_write_tokens": writes,
        "cache_write_1h_tokens": _pai_cache_write_1h(pai_usage) if writes else None,
    }


def _pai_calls(result: Any, requests: int) -> list[CallUsage] | None:
    """One CallUsage per model response of THIS run (new_messages(): the
    message history passed in carries earlier turns' responses). None when
    the responses don't account for every request — a partial breakdown
    would misstate the run."""
    new_messages = getattr(result, "new_messages", None)
    if new_messages is None:
        return None
    calls: list[CallUsage] = []
    for message in new_messages():
        if getattr(message, "kind", None) != "response":
            continue
        timestamp = getattr(message, "timestamp", None)
        if isinstance(timestamp, datetime) and timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc)
        calls.append(CallUsage(
            at=timestamp if isinstance(timestamp, datetime) else None,
            model=getattr(message, "model_name", None),
            **_pai_token_fields(getattr(message, "usage", None)),
        ))
    return calls if len(calls) == requests else None


def extract_run_details(result: Any) -> tuple[RunUsage, list[ToolCallRecord]]:
    """
    Extract usage and a tool-call trace from a PydanticAI AgentRunResult.

    Usage comes from result.usage (a property since pydantic-ai's AgentRunResult
    stopped exposing it as a callable), plus a per-request breakdown from each
    ModelResponse's own usage; tool calls are recovered by walking
    result.all_messages() for tool-call parts and pairing them with their
    return/retry parts via tool_call_id.
    """
    pai_usage = result.usage
    usage = RunUsage(
        requests=pai_usage.requests,
        **_pai_token_fields(pai_usage),
        calls=_pai_calls(result, pai_usage.requests),
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


def _with_dispatch(timings: dict | None, run_started: datetime) -> dict | None:
    """The gap between the run's acceptance and its harness turn starting
    (admission, the background task, history and memory setup), in the
    summary as dispatch_ms."""
    if not isinstance(timings, dict) or not timings.get("started_at"):
        return timings
    try:
        turn_started = datetime.fromisoformat(str(timings["started_at"]))
        gap_ms = round((turn_started - run_started).total_seconds() * 1000, 1)
    except (ValueError, TypeError):   # unparseable, or naive vs aware
        return timings
    summary = dict(timings.get("summary") or {})
    summary["dispatch_ms"] = gap_ms
    return {**timings, "summary": summary}
