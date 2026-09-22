"""The zero-or-more relevance selector (§17.7 step 4).

One bounded structured-output call over at most 20 candidate cards: it
returns up to eight record ids each with an applicability reason tied to
the CURRENT request — or none, which is a correct and common answer. It
has no tools, cannot rewrite facts, change status or create memories; its
output is IDs, and every selected id is re-resolved against canonical
state before anything is rendered (the selector never authors packet
text). No rank threshold means "relevant enough" — selection is the only
gate, and on selector failure the caller injects NOTHING optional rather
than falling back to stuffing nearest neighbors.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import json

from pydantic import BaseModel, ConfigDict, Field

MAX_CARDS = 20
MAX_CARD_CHARS = 300           # per-card text clamp for the selector prompt
MAX_SELECTOR_INPUT_CHARS = 24_000  # ≈6k tokens (§17.7)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    reason: str = Field(min_length=1, description="Applicability to the current request.")


class SelectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selections: list[Selection] = Field(default_factory=list)


SelectFn = Callable[[str, list[dict]], Awaitable[SelectionResult]]


SELECTOR_INSTRUCTIONS = """\
You select which candidate memories, if any, genuinely help the CURRENT
request. Candidates were retrieved by similarity — similarity is not
relevance, and most candidates usually do not apply.

Select a candidate only when it would change how the request is handled:
an applicable fact, a directly relevant prior episode, an applicable
procedure, an unresolved intention this request touches. Give each
selection a reason tied to THIS request, not a summary of the memory.

Do not select: near-duplicates of another selection, generically related
background, memories about different projects/branches/periods than the
request, or anything you would only include "just in case".

Selecting NOTHING is a correct and common answer. Return record_ids only —
never rewrite or summarize the memories themselves.
"""


def build_model_selector(model: str) -> SelectFn:
    # Lazy construction: pydantic-ai validates provider credentials at
    # Agent construction, and the selector may be built at boot on
    # profiles (executor tier) whose model key only matters if the lane
    # ever actually runs.
    agent = None

    async def select(request: str, cards: list[dict]) -> SelectionResult:
        nonlocal agent
        if agent is None:
            from pydantic_ai import Agent

            agent = Agent(model=model, instructions=SELECTOR_INSTRUCTIONS,
                          output_type=SelectionResult)
        result = await agent.run(render_selector_input(request, cards))
        return result.output

    return select


def render_selector_input(request: str, cards: list[dict]) -> str:
    lines = [f"Current request:\n{request[:2000]}", "", "Candidate memories:"]
    for card in cards[:MAX_CARDS]:
        payload = card.get("payload") or {}
        text = canonical_text(payload)
        if len(text) > MAX_CARD_CHARS:
            text = "[complete payload exceeds selector card budget; use explicit read]"
        slot = card.get("slot") or {}
        descriptor = f" [{slot.get('subject')} {slot.get('predicate')}]" if slot else ""
        lines.append(
            f"- record_id={card['record_id']} type={card['type']}{descriptor}: {text}"
        )
    rendered = "\n".join(lines)
    return rendered[:MAX_SELECTOR_INPUT_CHARS]


def clamp_selections(
    result: SelectionResult, candidates: list[dict], max_selected: int
) -> list[Selection]:
    """Deterministic post-filter: only ids that were actually candidates
    (the selector cannot introduce records), deduplicated, capped."""
    candidate_ids = {card["record_id"] for card in candidates}
    seen: set[str] = set()
    kept: list[Selection] = []
    for selection in result.selections:
        if selection.record_id not in candidate_ids or selection.record_id in seen:
            continue
        seen.add(selection.record_id)
        kept.append(selection)
        if len(kept) >= max_selected:
            break
    return kept


@dataclass
class RenderResult:
    text: str
    emitted: list[dict] = field(default_factory=list)
    omitted: list[dict] = field(default_factory=list)
    budget_chars: int = 0
    used_chars: int = 0

    @property
    def truncated(self) -> bool:
        return any(item["reason"] == "budget_exceeded" for item in self.omitted)

    def accounting(self) -> dict:
        return {"budget_chars": self.budget_chars, "used_chars": self.used_chars,
                "remaining_chars": self.budget_chars - self.used_chars,
                "truncated": self.truncated, "omitted": self.omitted,
                "emitted": [{"record_id": e["record_id"], "revision_id": e["revision_id"]}
                            for e in self.emitted]}


def canonical_text(payload: dict) -> str:
    """Never discard conditions carried in other payload fields."""
    if set(payload) == {"text"} and isinstance(payload["text"], str):
        return payload["text"]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def canonical_record_text(record: dict) -> str:
    """Payload plus applicability qualifications form one indivisible unit."""
    revision = record.get("revision") or record
    payload = revision["payload"]
    qualifications = {}
    for key in ("assertion", "valid_from", "valid_to", "observed_at"):
        if revision.get(key) is not None:
            qualifications[key] = revision[key]
    if record.get("slot"):
        qualifications["slot"] = record["slot"]
    if record.get("groundings"):
        if record.get("applicability"):
            qualifications["source_applicability"] = record["applicability"]
        qualifications["source_evidence"] = [{
            "resource": g["resource"], "source_revision": g["baseline"]["snapshot"]["source_revision"],
            "evidence_id": g["evidence_id"], "support": g["assertion_support"],
        } for g in record["groundings"]]
    return canonical_text({"payload": payload, **qualifications}) if qualifications else canonical_text(payload)


def render_optional_section(entries: list[dict], budget_chars: int) -> RenderResult:
    """Emit complete canonical units; count every separator and header.

    The returned IDs are the only IDs a rendering manifest may contain.
    An oversized entry does not prevent a later smaller one fitting.
    """
    budget = max(0, budget_chars)
    header = "[recalled memories — attributed reference data, selected for this request]"
    result = RenderResult(text="", budget_chars=budget)
    seen = set()
    for entry in entries:
        identity = {"record_id": entry["record_id"], "revision_id": entry["revision_id"]}
        if entry["record_id"] in seen:
            result.omitted.append(identity | {"reason": "duplicate_record"})
            continue
        seen.add(entry["record_id"])
        line = (f"- ({entry['type']}, {entry['record_id'][:8]}) {entry['text']}"
                f" | why: {entry['reason']}")
        candidate = (result.text or header) + "\n" + line
        if len(candidate) > budget:
            result.omitted.append(identity | {"reason": "budget_exceeded", "item_chars": len(line)})
            continue
        result.text = candidate
        result.emitted.append(entry)
    result.used_chars = len(result.text)
    return result
