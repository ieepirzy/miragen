"""The generic external-session abstraction and the event envelope.

`EventEnvelope` is what an adapter posts: harness, harness session id,
one normalized event, and what the adapter could observe about its
process. `extra="ignore"` throughout — an adapter a version ahead of the
daemon must not be rejected for a field the daemon does not know yet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

HARNESS_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
EVENT_NAMES = (
    "context.started",
    "context.restored",
    "input.received",
    "tool.finished",
    "context.compacting",
    "context.compacted",
    "turn.finished",
    "context.child_started",
    "context.child_finished",
    "context.closed",
)
SessionState = Literal["active", "ended", "stale"]

_PROMPT_KEEP = 20
_PROMPT_CHARS = 300
_TURN_KEEP = 10
_TURN_CHARS = 600


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ClientInfo(_Tolerant):
    pid: Optional[int] = Field(default=None, ge=1)
    cwd: Optional[str] = Field(default=None, max_length=4096)
    user: Optional[str] = Field(default=None, max_length=256)
    # Where the harness runs. A hosted daemon serves sessions from many
    # hosts: pid liveness only means something on its own host, and a
    # working directory can only be inspected there. `remote` is the
    # harness's own declaration (Claude Code sets CLAUDE_CODE_REMOTE in
    # cloud sessions); `project_remote` is the repository's remote URL as
    # the adapter observed it, so the project can be identified without
    # the daemon seeing the filesystem.
    host: Optional[str] = Field(default=None, max_length=256)
    remote: Optional[bool] = None
    project_remote: Optional[str] = Field(default=None, max_length=1024)
    transcript_path: Optional[str] = Field(default=None, max_length=4096)
    project_dir: Optional[str] = Field(default=None, max_length=4096)
    parent_session: Optional[str] = Field(default=None, max_length=256)
    agent: Optional[str] = Field(default=None, max_length=256)
    adapter: Optional[str] = Field(default=None, max_length=64)
    # "deferred": the harness discards start/prompt hook output, so context
    # answered now reaches the model only with a later tool result (Grok
    # Build). Free-form on purpose — a newer adapter's value must not 422.
    context_delivery: Optional[str] = None

    @field_validator("context_delivery", mode="before")
    @classmethod
    def _clip_delivery(cls, value: Any) -> Any:
        return value[:32] if isinstance(value, str) else None


class EventBody(_Tolerant):
    name: Literal[EVENT_NAMES]  # type: ignore[valid-type]
    original_event: str = Field(min_length=1, max_length=64)
    ids: dict[str, str] = Field(default_factory=dict)
    content: Optional[str] = Field(default=None, max_length=20_000)
    attributes: dict[str, Any] = Field(default_factory=dict)


class EventEnvelope(_Tolerant):
    harness: str = Field(pattern=HARNESS_PATTERN)
    session_id: str = Field(min_length=1, max_length=256)
    event: EventBody
    client: ClientInfo = Field(default_factory=ClientInfo)
    sent_at: Optional[str] = None

    @property
    def key(self) -> str:
        return session_key(self.harness, self.session_id)


def session_key(harness: str, session_id: str) -> str:
    return f"{harness}:{session_id}"


class ProjectIdentity(BaseModel):
    """What a working directory resolves to. `id` is stable across
    clones of the same repository when a remote exists; `slug` is the
    scope-id-safe form."""

    model_config = ConfigDict(extra="ignore")

    id: str
    slug: str
    name: str
    root: str
    remote: Optional[str] = None


class ChildAgent(_Tolerant):
    agent_id: str
    agent_type: Optional[str] = None
    started_at: str = Field(default_factory=now_iso)
    finished_at: Optional[str] = None


class SessionCounters(_Tolerant):
    events: int = 0
    prompts: int = 0
    turns: int = 0
    tool_failures: int = 0
    compactions: int = 0
    injections: int = 0
    # Of `injections`, how many were queued for a later tool result rather
    # than shown at once (harnesses that discard start/prompt output).
    deferred_injections: int = 0
    captures: int = 0
    capture_failures: int = 0


class ExternalSession(_Tolerant):
    """An externally managed agent session: enough identity to scope
    memory (harness, project, agent, parent) and enough lifecycle to
    finalize it (state, pid, timestamps, what it did)."""

    key: str
    harness: str
    session_id: str
    state: SessionState = "active"
    pid: Optional[int] = None
    cwd: Optional[str] = None
    user: Optional[str] = None
    host: Optional[str] = None
    remote: bool = False
    transcript_path: Optional[str] = None
    parent_session: Optional[str] = None
    agent: Optional[str] = None
    adapter: Optional[str] = None
    project: Optional[ProjectIdentity] = None
    scope: Optional[str] = None
    # Loimi artifact store participation: the run this session's
    # artifacts belong to, the namespace it was opened in, and which
    # episode occurrences already produced an artifact (the store has
    # no idempotency keys; this is ours).
    run_id: Optional[str] = None
    namespace: Optional[str] = None
    run_status: Optional[str] = None
    artifacts_written: list[str] = Field(default_factory=list)
    # How many times an ended/stale session came back under the same id
    # (a resume, or a >stale_after gap). Each life gets its own run and
    # its own episode keys.
    lives: int = 0
    children: dict[str, ChildAgent] = Field(default_factory=dict)
    counters: SessionCounters = Field(default_factory=SessionCounters)
    created_at: str = Field(default_factory=now_iso)
    last_seen_at: str = Field(default_factory=now_iso)
    ended_at: Optional[str] = None
    end_reason: Optional[str] = None
    # Bounded observation used for the deterministic episode digest.
    prompts: list[str] = Field(default_factory=list)
    turns: list[str] = Field(default_factory=list)
    episodes_written: list[str] = Field(default_factory=list)

    def touch(self, envelope: EventEnvelope) -> None:
        self.last_seen_at = now_iso()
        self.counters.events += 1
        client = envelope.client
        if client.pid and self.pid is None:
            self.pid = client.pid
        if client.cwd and not self.cwd:
            self.cwd = client.cwd
        if client.user and not self.user:
            self.user = client.user
        if client.host and not self.host:
            self.host = client.host
        if client.remote:
            self.remote = True
        if client.transcript_path and not self.transcript_path:
            self.transcript_path = client.transcript_path
        if client.parent_session and not self.parent_session:
            self.parent_session = client.parent_session
        if client.agent and not self.agent:
            self.agent = client.agent
        if client.adapter:
            self.adapter = client.adapter

    def note_prompt(self, text: str | None) -> None:
        self.counters.prompts += 1
        if text:
            self.prompts.append(text[:_PROMPT_CHARS])
            del self.prompts[:-_PROMPT_KEEP]

    def note_turn(self, text: str | None) -> None:
        self.counters.turns += 1
        if text:
            self.turns.append(text[:_TURN_CHARS])
            del self.turns[:-_TURN_KEEP]

    def new_life(self) -> None:
        """An ended/stale session speaks again under the same id: it is
        active, and its closed store run is history — the next context
        opens a fresh run with fresh dedupe state."""
        self.state = "active"
        self.ended_at = None
        self.end_reason = None
        self.lives += 1
        if self.run_status is not None and self.run_status != "running":
            self.run_id = None
            self.namespace = None
            self.run_status = None
            self.artifacts_written = []

    def is_empty(self) -> bool:
        """Nothing happened: no prompt, no turn, no tool failure, no child.
        Cloud harnesses touch every cloned repository at start with a
        session that opens and closes in seconds — those leave no trail."""
        c = self.counters
        return not (c.prompts or c.turns or c.tool_failures or self.children)

    def end(self, reason: str | None) -> None:
        if self.state != "ended":
            self.state = "ended"
            self.ended_at = now_iso()
            self.end_reason = reason or "unknown"

    def summary(self) -> dict[str, Any]:
        """The listing shape: identity + counters, no prompt content."""
        data = self.model_dump(exclude={"prompts", "turns"})
        data["first_prompt"] = self.prompts[0] if self.prompts else None
        return data
