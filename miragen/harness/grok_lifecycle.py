"""Session lifecycle for long-lived Grok instances: compact, rotate, hand off.

A conversation instance (a Telegram chat, say) is endless; a Grok session
behind it shouldn't be, because every model call re-sends the whole context.
After each turn the harness asks a **policy** what to do with the session:

  - nothing: let it grow
  - compact: a memory-save turn first, then grok's ``/compact``
  - rotate:  one turn that saves memories *and* writes a handoff note, then
             a fresh session whose first turn carries the note

The instance name never changes, so a client (Mira) never sees sessions.

The default policy (``ThresholdPolicy``) is a first guess, not a finding:
compact at ~150k context tokens, rotate instead of the 3rd compaction, and
rotate unconditionally at 400k (grok-4.7's window is 500k). A **ledger**
(``<grok_home>/lifecycle/<instance>.jsonl``) records context size per turn,
every compaction's before/after, memory writes and each rotation, so a better
policy can be derived from real data and swapped in via
``MIRAGEN_GROK_LIFECYCLE_POLICY=module:attr``.
"""

from __future__ import annotations

import importlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

Action = Literal["none", "compact", "rotate"]

# Gateway tools whose accepted result means a durable memory write.
MEMORY_WRITE_TOOLS = ("memory_remember", "memory_checkpoint", "memory_correct")


@dataclass(frozen=True)
class SessionStats:
    context_tokens: int      # the latest model call's whole input
    compactions: int         # in this session (harness-triggered + grok's own)
    turns: int               # user turns in this session
    age_s: float             # since the session started
    seq: int                 # 1 = the instance's first session


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""


class LifecyclePolicy(Protocol):
    def decide(self, stats: SessionStats) -> Decision: ...


@dataclass(frozen=True)
class ThresholdPolicy:
    compact_at_tokens: int = 150_000
    rotate_at_tokens: int = 400_000
    rotate_after_compactions: int = 3

    def decide(self, stats: SessionStats) -> Decision:
        if stats.context_tokens >= self.rotate_at_tokens:
            return Decision("rotate", "context_cap")
        if stats.context_tokens >= self.compact_at_tokens:
            if stats.compactions + 1 >= self.rotate_after_compactions:
                return Decision("rotate", "compaction_count")
            return Decision("compact", "context_threshold")
        return Decision("none")


def load_policy(env: dict[str, str]) -> LifecyclePolicy:
    """``MIRAGEN_GROK_LIFECYCLE_POLICY=module:attr`` (a policy object or a
    zero-argument factory), else ThresholdPolicy from the env numbers."""
    ref = env.get("MIRAGEN_GROK_LIFECYCLE_POLICY")
    if ref:
        module, _, attr = ref.partition(":")
        obj = getattr(importlib.import_module(module), attr)
        # A class or factory is called; a ready policy object is used as is.
        return obj() if isinstance(obj, type) or not hasattr(obj, "decide") else obj
    return ThresholdPolicy(
        compact_at_tokens=int(env.get("MIRAGEN_GROK_COMPACT_AT_TOKENS", "150000")),
        rotate_at_tokens=int(env.get("MIRAGEN_GROK_ROTATE_AT_TOKENS", "400000")),
        rotate_after_compactions=int(env.get("MIRAGEN_GROK_ROTATE_AFTER_COMPACTIONS", "3")),
    )


class Ledger:
    """Append-only lifecycle telemetry, one JSON line per event."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, instance: str) -> Path:
        return self.root / f"{instance}.jsonl"

    def write(self, instance: str, event: str, **fields: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path(instance).open("a") as f:
            f.write(json.dumps({"at": time.time(), "event": event, **fields}, default=str) + "\n")

    def read(self, instance: str) -> list[dict]:
        p = self.path(instance)
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def memory_save_prompt(reason: str) -> str:
    return (
        "<session-maintenance source=\"miragen\">\n"
        f"This conversation's context is about to be compacted ({reason}): older detail will "
        "be summarised. Before that happens, save to long-term memory anything durable from "
        "this session that isn't saved yet — facts, decisions, commitments, preferences — "
        "with your memory tools. Don't re-save what is already stored. Reply with one short "
        "line; nobody reads it.\n</session-maintenance>")


def rotation_prompt(reason: str, max_chars: int) -> str:
    return (
        "<session-maintenance source=\"miragen\">\n"
        f"This conversation continues in a fresh session after this ({reason}). The person "
        "you're talking with won't notice the switch, so the next session has to pick up "
        "exactly where this one is.\n"
        "1. First, save to long-term memory anything durable from this session that isn't "
        "saved yet, with your memory tools.\n"
        "2. Then reply with a handoff note to your next self: what is in progress, open "
        "threads and promises, what was just being discussed, and anything about tone or "
        "mood worth keeping. Write it for yourself, not for the user.\n"
        f"The note is cut off at {max_chars} characters, so put the most important things "
        "first.\n</session-maintenance>")


@dataclass(frozen=True)
class Handoff:
    text: str
    truncated: bool
    prev_seq: int
    reason: str
    started_at: float
    rotated_at: float
    turns: int
    compactions: int
    context_tokens: int
    memory_writes: int
    last_memory_write_at: float | None
    handoff_error: str | None = None

    def render(self) -> str:
        def ts(t: float | None) -> str:
            return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t else "never"
        facts = [
            f"previous session: #{self.prev_seq}, {ts(self.started_at)} → {ts(self.rotated_at)}, "
            f"{self.turns} turns, {self.compactions} compactions, "
            f"{self.context_tokens} context tokens at the end",
            f"rotated because: {self.reason}",
            f"memories written in it: {self.memory_writes} (last: {ts(self.last_memory_write_at)})",
        ]
        if self.handoff_error:
            facts.append(f"handoff note: missing ({self.handoff_error})")
        elif self.truncated:
            facts.append("handoff note: cut at the size limit")
        body = "\n".join(f"- {f}" for f in facts)
        note = f"\n\nYour handoff note:\n{self.text}" if self.text else ""
        return ("<handoff source=\"miragen\">\nThis conversation continues from an earlier "
                "session of yours; the person you're talking with sees one continuous chat.\n"
                f"{body}{note}\n</handoff>")

    def to_json(self) -> dict:
        return asdict(self)


def accepted_memory_writes(calls: list) -> int:
    """Gateway calls that durably wrote memory: the right tool, ok, and a
    result the plane marked accepted (pending / unavailable are not saved)."""
    n = 0
    for c in calls:
        name = getattr(c, "tool_name", "")
        if not any(name == t or name.endswith("_" + t) for t in MEMORY_WRITE_TOOLS):
            continue
        if getattr(c, "ok", False) and getattr(c, "result_status", None) == "accepted":
            n += 1
    return n
