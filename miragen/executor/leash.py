"""Host-imposed action leash (issue #38): a deterministic, per-operation gate
miragen applies to executor actions through the backend's native approval hook.

Backend-agnostic. An adapter translates its native approval request into a
`GateOperation` and asks the `LeashPolicy` whether to allow it. A blocked
operation never runs; the base tier escalates the block into a Phase-G
intervention for human review. The whole thing is in-process — no files, no
serialization — and costs the agent no context: the gate lives entirely in
miragen and the agent never learns it exists.

Deterministic and ~free (operation-class check + regex backstop). An optional
LLM classifier for the ambiguous shell residue is a later phase, layered on
top of — not instead of — these rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from miragen.models import LeashSpec


@dataclass(frozen=True)
class GateOperation:
    """One executor action presented for approval, normalized across backends."""

    op_class: str  # "write" | "network" | "destructive" | "command" | …
    command: Optional[str] = None  # the shell command, when the action is one
    summary: str = ""  # short human-readable description for the intervention


@dataclass(frozen=True)
class GateDecision:
    allow: bool
    reason: str = ""
    matched: Optional[str] = None  # the rule that blocked (class name or pattern)


class LeashPolicy:
    """Compiled, immutable evaluation of one `LeashSpec` for one agent mode."""

    def __init__(self, spec: LeashSpec, *, mode: str):
        self.gated_classes = spec.gated_classes(mode)
        self._deny = [re.compile(p) for p in spec.deny_commands]

    def evaluate(self, op: GateOperation) -> GateDecision:
        # Free-form command backstop first: a deny-pattern hit is gated
        # regardless of the operation's class (this is the open-ended
        # shell-danger residue rules alone can't classify by type).
        if op.command:
            for pattern in self._deny:
                if pattern.search(op.command):
                    return GateDecision(
                        allow=False,
                        reason=f"command matches deny pattern /{pattern.pattern}/",
                        matched=pattern.pattern,
                    )
        if op.op_class in self.gated_classes:
            return GateDecision(
                allow=False,
                reason=f"operation class '{op.op_class}' is gated for this run",
                matched=op.op_class,
            )
        return GateDecision(allow=True)
