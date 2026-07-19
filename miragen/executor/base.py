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
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from miragen.models import AgentProfile, ExecutorSpec, RunUsage, sum_usage

logger = logging.getLogger("miragen.executor")

_DIFF_NAME = "diff.patch"
_EVENTS_SUFFIX = ".events.jsonl"
_BASELINE_TAG = "miragen-baseline"

# Event envelope version (issue #33 Phase C). Every persisted event carries
# `seq` (per-run monotonic, 1-based), `schema`, `ts`, and `type` alongside its
# payload fields. The envelope is flat — new keys on the same JSONL object —
# so existing tail readers keep working unchanged.
EVENT_SCHEMA = "miragen/executor-event/v1"


def _iter_event_lines(path: Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    """Yield (line_no, parsed | None) for every non-blank line. Unparsable
    lines yield None but still occupy a line number, so sequence assignment
    stays stable no matter who reads the file."""
    with path.open() as f:
        line_no = 0
        for line in f:
            line = line.strip()
            if not line:
                continue
            line_no += 1
            try:
                yield line_no, json.loads(line)
            except ValueError:
                yield line_no, None


def _effective_seq(line_no: int, event: dict[str, Any]) -> int:
    """Explicit seq when the writer stamped one; the 1-based line number for
    legacy pre-envelope lines (writers have always been append-only and
    single-writer per run, so line order IS event order)."""
    seq = event.get("seq")
    return seq if isinstance(seq, int) else line_no


class _EventWriter:
    """Append-only events.jsonl sink owning the per-run monotonic sequence.

    On open, scans the existing file (resume case) and continues numbering
    after the last effective sequence — a resumed turn's events extend the
    same ordered stream rather than restarting at 1. Each write is flushed so
    cursor/tail readers and crash forensics see events promptly.
    """

    def __init__(self, path: Path):
        self.path = path
        self._last_seq = 0
        if path.exists():
            for line_no, event in _iter_event_lines(path):
                self._last_seq = _effective_seq(line_no, event) if event is not None else line_no
        self._fh = path.open("a")

    def write(self, payload: dict[str, Any]) -> None:
        self._last_seq += 1
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        payload["seq"] = self._last_seq
        payload["schema"] = EVENT_SCHEMA
        self._fh.write(json.dumps(payload, default=str) + "\n")
        self._fh.flush()

    def __enter__(self) -> "_EventWriter":
        return self

    def __exit__(self, *exc) -> None:
        self._fh.close()


@dataclass
class EventPage:
    """One page of a cursor read: events with seq > `after`, in order.
    Replay contract: (run_id, seq) is the deduplication key; re-reading any
    cursor returns the same events."""

    events: list[dict[str, Any]] = field(default_factory=list)
    next_after: int = 0  # pass as `after` to get the next page
    has_more: bool = False


@dataclass
class RepositoryCheckout:
    """One repository to prepare in the run's workspace (issue #33 Phase D).

    `clone_url` is the EPHEMERAL authorized binding — consumed during
    preparation, redacted from any error, stripped from the clone's git
    config, and never persisted. On resume the workspace already exists, so
    checkouts are rebuilt from the run record with clone_url="" and
    preparation only re-reads the recorded state."""

    name: str
    ref: str
    mount_path: str
    writable: bool = False
    clone_url: str = ""


def _sanitize_url(url: str) -> str:
    """Strip userinfo (user:token@) from a URL so it never rests in a clone's
    .git/config or appears in an error message."""
    if "://" in url and "@" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest.split("/", 1)[0]:
            rest = rest.split("@", 1)[1]
        return f"{scheme}://{rest}"
    return url


def _redact(text: str, checkouts: "list[RepositoryCheckout] | None") -> str:
    """Replace every binding URL (and its credentialed userinfo) in `text`
    with a stable placeholder — git error output embeds remote URLs."""
    for co in checkouts or []:
        if co.clone_url:
            text = text.replace(co.clone_url, f"<clone-url:{co.name}>")
            sanitized = _sanitize_url(co.clone_url)
            if sanitized != co.clone_url:
                text = text.replace(sanitized, f"<clone-url:{co.name}>")
    return text


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
        repositories: list[dict[str, Any]] | None = None,
    ):
        self.status = status
        self.exit_reason = exit_reason
        self.thread_id = thread_id
        self.output = output
        self.error = error
        self.usage = usage
        self.diff_path = diff_path
        # Concrete prepared-repository state (name/ref/mount_path/writable/
        # commit) — filled by workspace preparation, no credentials ever.
        self.repositories = repositories


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
        repositories: list[RepositoryCheckout] | None = None,
    ) -> ExecutorResult:
        """Execute one turn of a job bound to an agent-run.

        First turn: fresh workspace + fresh thread; `instructions` are
        prepended. Resume: existing workspace + adapter-side thread resume.
        `prior_usage` is the run's accumulated usage from earlier turns —
        needed so a per-run budget check on resume sees the true total, not
        just this turn's usage. `repositories` is the run's checkout plan
        (with ephemeral bindings on the first turn, binding-less on resume);
        None keeps the classic single-baseline empty workspace.
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
        prepared: list[dict[str, Any]] | None = None

        try:
            # Inside the try: a bad workspace_root, missing git, a failed
            # clone/fetch, a permission error, or a failed harvest must still
            # produce a 'failed' result (and thus a RunStore.finish() call)
            # rather than an unhandled exception that leaves the run record
            # stuck at 'running'. Setup failures are resumable crashes:
            # preparation is idempotent (already-prepared repositories are
            # skipped), so a resumed turn re-runs it safely.
            with _EventWriter(events_path) as sink:
                setup_t0 = time.monotonic()
                sink.write({"type": "lifecycle.setup.started", "phase": "workspace", "first_turn": first_turn})
                prepared = self._prepare_workspace(ws, repositories, sink=sink)
                sink.write({
                    "type": "lifecycle.setup.completed",
                    "phase": "workspace",
                    "duration_ms": int((time.monotonic() - setup_t0) * 1000),
                })
                async for payload in self._stream_turn(
                    prompt,
                    run_id=run_id,
                    thread_id=thread_id,
                    workspace=ws,
                    first_turn=first_turn,
                ):
                    sink.write(payload)

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

                if turn_failed:
                    return ExecutorResult(
                        status="failed",
                        exit_reason="crash",
                        thread_id=observed_thread_id,
                        error=error,
                        usage=usage,
                        repositories=prepared,
                    )

                if self._budget_exhausted(sum_usage(prior_usage, usage)):
                    # Turn completed but blew the per-run budget: suspend, keep
                    # the partial diff as resume state, do not harvest. Checked
                    # against the run's cumulative usage (prior turns + this
                    # one) — a resumed run must not be able to sneak back under
                    # a per-run cap just because a single resumed turn's own
                    # usage looks small.
                    return ExecutorResult(
                        status="suspended",
                        exit_reason="budget",
                        thread_id=observed_thread_id,
                        output=output,
                        usage=usage,
                        repositories=prepared,
                    )

                harvest_t0 = time.monotonic()
                diff_path = await self._harvest_diff(ws, repositories)
                sink.write({
                    "type": "lifecycle.harvest.completed",
                    "duration_ms": int((time.monotonic() - harvest_t0) * 1000),
                    "diff_bytes": Path(diff_path).stat().st_size,
                })
        except Exception as e:
            # Crash / API error: resumable. Partial workspace changes are
            # neither harvested nor discarded — they are resume state.
            # Binding URLs (which may carry credentials) are redacted from the
            # recorded error — git embeds remote URLs in its stderr.
            # (CancelledError is a BaseException and deliberately NOT caught:
            # the app-tier timeout owns that path.)
            message = _redact(str(e), repositories)
            logger.error(f"[{self.profile.name}] executor crashed: {message}", exc_info=True)
            return ExecutorResult(
                status="failed",
                exit_reason="crash",
                thread_id=observed_thread_id,
                error=message,
                usage=usage,
                repositories=prepared,
            )

        return ExecutorResult(
            status="succeeded",
            thread_id=observed_thread_id,
            output=output,
            usage=usage,
            diff_path=diff_path,
            repositories=prepared,
        )

    # ── Events ─────────────────────────────────────────────────────────────

    def _events_path(self, run_id: str) -> Path:
        return self.runs_root / f"{run_id}{_EVENTS_SUFFIX}"

    def _parsed_events(self, run_id: str) -> list[dict[str, Any]]:
        """All parseable events with an effective `seq` stamped — explicit for
        envelope-era lines, line-number-derived for legacy pre-envelope files,
        so replay/dedup by (run_id, seq) works across both."""
        path = self._events_path(run_id)
        if not path.exists():
            return []
        events = []
        for line_no, event in _iter_event_lines(path):
            if event is None:
                continue
            event.setdefault("seq", _effective_seq(line_no, event))
            events.append(event)
        return events

    def read_events(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Tail read (newest `limit` events, in order) — the original polling
        contract, preserved."""
        return self._parsed_events(run_id)[-limit:]

    def read_events_page(self, run_id: str, *, after: int = 0, limit: int = 200) -> EventPage:
        """Cursor read: up to `limit` events with seq > `after`, oldest first.
        Feed `next_after` back as `after` to continue; a cursor past the end
        returns an empty page with has_more=False. Reads are pure — replaying
        the same cursor yields the same page, and a projector can rebuild its
        projection from after=0 at any time."""
        newer = [e for e in self._parsed_events(run_id) if e["seq"] > after]
        page = newer[:limit]
        return EventPage(
            events=page,
            next_after=page[-1]["seq"] if page else after,
            has_more=len(newer) > limit,
        )

    def latest_thread_id(self, run_id: str) -> str | None:
        """Recover the resume handle from the persisted event stream — used by
        the app tier when a turn is cancelled (timeout) before run_job could
        return a result carrying the thread_id.

        Scans the WHOLE file, not a tail window: thread.started is typically
        the first event, so a long turn would push it out of any tail.
        """
        path = self._events_path(run_id)
        if not path.exists():
            return None
        thread_id: str | None = None
        for _line_no, event in _iter_event_lines(path):
            if event is not None and event.get("type") == "thread.started" and event.get("thread_id"):
                thread_id = event["thread_id"]
        return thread_id

    # ── Workspace + harvest ────────────────────────────────────────────────

    def _prepare_workspace(
        self,
        ws: Path,
        repositories: list[RepositoryCheckout] | None = None,
        *,
        sink: "_EventWriter | None" = None,
    ) -> list[dict[str, Any]] | None:
        """Prepare the run's workspace. Idempotent — safe to re-run on resume.

        Without a repository plan (the classic layout): the workspace root
        itself becomes a git repo with the baseline tag.

        With a plan: each repository is fetched into its mount path as its own
        git repo; writable ones get the baseline tag; the workspace root stays
        a plain directory (a root git repo would record the nested clones as
        gitlinks and silently swallow their diffs). Returns the concrete
        prepared state (name/ref/mount_path/writable/commit) — never bindings.
        """
        ws.mkdir(parents=True, exist_ok=True)
        if not repositories:
            if not (ws / ".git").exists():
                # The diff IS the deliverable; a git baseline makes harvest
                # exact. Tagged (not just left as a loose commit) so harvest
                # can diff against it even after the executor makes its own
                # commits — the tag lives in .git, so the executor's own
                # `git add -A` / commits never touch or shadow it.
                _git(ws, "init", "-q")
                _git(ws, "add", "-A")
                _git(ws, "-c", "user.email=miragen@local", "-c", "user.name=miragen",
                     "commit", "-q", "--allow-empty", "-m", "miragen: workspace baseline")
                _git(ws, "tag", "-f", _BASELINE_TAG)
            return None

        prepared: list[dict[str, Any]] = []
        for co in repositories:
            repo_t0 = time.monotonic()
            try:
                commit = self._prepare_repository(ws, co)
            except subprocess.CalledProcessError as e:
                raise RuntimeError(_redact(
                    f"preparing repository '{co.name}' (ref {co.ref}) failed: "
                    f"{(e.stderr or e.stdout or str(e)).strip()}",
                    repositories,
                )) from None
            prepared.append({
                "name": co.name,
                "ref": co.ref,
                "mount_path": co.mount_path,
                "writable": co.writable,
                "commit": commit,
            })
            if sink is not None:
                sink.write({
                    "type": "lifecycle.repo.prepared",
                    "name": co.name,
                    "ref": co.ref,
                    "mount_path": co.mount_path,
                    "writable": co.writable,
                    "commit": commit,
                    "duration_ms": int((time.monotonic() - repo_t0) * 1000),
                })
        return prepared

    def _prepare_repository(self, ws: Path, co: RepositoryCheckout) -> str:
        """Fetch one repository into its mount path and return the concrete
        commit SHA. Credentials stay ephemeral: after checkout the remote URL
        is rewritten without userinfo so nothing credentialed rests in
        .git/config, and callers redact binding URLs from any failure."""
        dest = ws / co.mount_path
        if (dest / ".git").exists():
            # Resume / retried setup: already prepared — report its state.
            return _git(dest, "rev-parse", "HEAD").strip()
        if not co.clone_url:
            raise RuntimeError(
                f"repository '{co.name}' is not prepared at '{co.mount_path}' and no "
                "binding is available — bindings are ephemeral launch-time state and "
                "cannot be re-minted by miragen (was the workspace discarded?)"
            )
        dest.mkdir(parents=True, exist_ok=True)
        _git(dest, "init", "-q")
        _git(dest, "remote", "add", "origin", co.clone_url)
        try:
            # Shallow fetch of exactly the requested ref (branch, tag, or SHA
            # where the server allows it)...
            _git(dest, "fetch", "-q", "--depth", "1", "origin", co.ref)
            _git(dest, "checkout", "-q", "--detach", "FETCH_HEAD")
        except subprocess.CalledProcessError:
            try:
                # ...then full-depth fetch of the same explicit ref...
                _git(dest, "fetch", "-q", "origin", co.ref)
                _git(dest, "checkout", "-q", "--detach", "FETCH_HEAD")
            except subprocess.CalledProcessError:
                # ...then an unqualified full fetch for refs no direct fetch
                # can serve (e.g. an arbitrary reachable SHA the server won't
                # advertise), which the checkout can then resolve locally.
                _git(dest, "fetch", "-q", "origin")
                _git(dest, "checkout", "-q", "--detach", co.ref)
        # Ephemeral credentials: the binding URL must not rest in .git/config.
        _git(dest, "remote", "set-url", "origin", _sanitize_url(co.clone_url))
        commit = _git(dest, "rev-parse", "HEAD").strip()
        if co.writable:
            _git(dest, "tag", "-f", _BASELINE_TAG)
        return commit

    async def _harvest_diff(
        self, ws: Path, repositories: list[RepositoryCheckout] | None = None
    ) -> str:
        """git-diff harvest, exactly once, on terminal success (§293 decision 3).

        Diffs against the baseline tag, not HEAD: if the executor committed
        mid-turn, HEAD has moved past the baseline, and `git diff --cached`
        with no revision arg would only show the *uncommitted* remainder,
        silently dropping everything the executor already committed.

        Multi-repo artifact contract: only WRITABLE repositories are
        harvested (non-writable mounts are reference material — changes there
        are deliberately not part of the deliverable). Each writable repo
        yields `.miragen/diffs/<name>.patch` (apply with `git apply` inside
        that repo); `.miragen/diff.patch` is a human-readable bundle of all
        of them with `# === miragen repository: ...` section markers, and is
        what diff_path / GET /runs/{id}/diff serve.
        """
        marker_dir = ws / ".miragen"
        marker_dir.mkdir(exist_ok=True)
        diff_path = marker_dir / _DIFF_NAME

        if not repositories:
            _git(ws, "add", "-A")  # stage everything so new files appear in the diff
            diff_path.write_text(_git(ws, "diff", "--cached", "--binary", _BASELINE_TAG))
            return str(diff_path)

        diffs_dir = marker_dir / "diffs"
        diffs_dir.mkdir(exist_ok=True)
        sections: list[str] = []
        for co in repositories:
            if not co.writable:
                continue
            repo_dir = ws / co.mount_path
            _git(repo_dir, "add", "-A")
            diff = _git(repo_dir, "diff", "--cached", "--binary", _BASELINE_TAG)
            (diffs_dir / f"{co.name}.patch").write_text(diff)
            sections.append(
                f"# === miragen repository: {co.name} (mount: {co.mount_path}) ===\n{diff}"
            )
        diff_path.write_text("\n".join(sections))
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
