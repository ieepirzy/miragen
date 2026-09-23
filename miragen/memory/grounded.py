"""Supported orchestration over disposable source inspection and Loimi APIs."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from miragen.memory.lifecycle import MemoryPacket
from miragen.memory.resources import (
    inspect_resources,
    resource_key,
    _read,
    _symbols,
    digest,
)
from miragen.memory.selection import (
    canonical_text,
    canonical_record_text,
    render_optional_section,
)


async def recall(
    lifecycle,
    checkout: str,
    locators: list[dict],
    *,
    query: str = "",
    inspect: bool = False,
):
    resources = await asyncio.to_thread(inspect_resources, checkout, locators)
    if inspect:
        found = await lifecycle.client.lookup_resources(
            {
                "scope_ids": lifecycle.spec.scopes.read,
                "resources": resources,
                "mode": "inspect",
                "limit": lifecycle.spec.recall.max_candidates,
            }
        )
        entries = [
            {
                "record_id": c["record_id"],
                "revision_id": c["revision_id"],
                "type": c["type"],
                "text": canonical_record_text(c),
                "reason": "INSPECTION ONLY; " + canonical_text(c["applicability"]),
            }
            for c in found["items"]
        ]
        rendered = render_optional_section(
            entries, lifecycle.spec.recall.max_optional_chars
        )
        accounting = rendered.accounting()
        accounting["omitted"] = found.get("omitted", []) + accounting["omitted"]
        accounting["retrieval_truncated"] = found.get("truncated", False)
        accounting["truncated"] = accounting["truncated"] or found.get(
            "truncated", False
        )
        return {
            "text": rendered.text,
            "rendering": accounting,
            "inspection_only": True,
            "permission_to_act": False,
            "resources": resources,
        }
    packet = MemoryPacket(text="")

    async def refresh():
        resources[:] = await asyncio.to_thread(inspect_resources, checkout, locators)
        return resources

    status = await lifecycle._optional_lane(
        packet,
        "resource-tool",
        {"state": {}, "state_revision": 0},
        query,
        resources,
        resource_reader=refresh,
    )
    return {
        "text": packet.text,
        "items": packet.items,
        "rendering": packet.rendering,
        "status": status,
        "delivery_status": "unconfirmed",
        "resources": resources,
        "permission_to_act": False,
    }


async def check(lifecycle, checkout: str, locators: list[dict], *, limit: int = 20):
    """Explicit maintenance write. Bounded exact lookup includes stale evidence."""
    if not 1 <= limit <= 20:
        raise ValueError("check limit must be 1..20")
    resources = await asyncio.to_thread(inspect_resources, checkout, locators)
    found = await lifecycle.client.lookup_resources(
        {
            "scope_ids": lifecycle.spec.scopes.read,
            "resources": resources,
            "mode": "inspect",
            "limit": 50,
        }
    )
    by_key = {resource_key(o["resource"]): o for o in resources}
    receipts, omitted, seen = [], list(found.get("omitted", [])), set()
    for item in found["items"]:
        if item.get("current_revision_id", item["revision_id"]) != item["revision_id"]:
            omitted.append(
                {"revision_id": item["revision_id"], "reason": "historical_revision"}
            )
            continue
        for grounding in item["groundings"]:
            if grounding["id"] in seen or grounding["resource_key"] not in by_key:
                continue
            seen.add(grounding["id"])
            if len(receipts) >= limit:
                omitted.append(
                    {"grounding_id": grounding["id"], "reason": "check_limit"}
                )
                continue
            receipts.append(
                await lifecycle.client.check_grounding(
                    grounding["id"],
                    {
                        "expected_revision_id": item["revision_id"],
                        "observation": by_key[grounding["resource_key"]],
                    },
                )
            )
    return {
        "receipts": receipts,
        "omitted": omitted,
        "truncated": bool(omitted) or found["truncated"],
    }


async def remember(
    lifecycle,
    checkout: str,
    locator: dict,
    *,
    payload: dict,
    support: dict,
    record_type: str = "observation",
):
    """An explicit source-grounded write; support is an attributed verifier receipt.

    A hash does not author this receipt. The caller must name what actually
    checked the assertion and its limitations (or report inconclusive).
    """
    if lifecycle.spec.backend != "loimi":
        return {
            "status": "unsupported_backend",
            "detail": "source grounding requires Loimi",
        }
    if record_type not in ("observation", "procedure", "intention"):
        raise ValueError("structured claims use the existing predicate/record API")
    if support.get("result") not in (
        "supports",
        "contradicts",
        "inconclusive",
    ) or not support.get("method"):
        raise ValueError(
            "provide a verifier method and explicit assertion support outcome"
        )
    observation = (await asyncio.to_thread(inspect_resources, checkout, [locator]))[0]
    if observation["outcome"] != "present":
        return {"status": "unverified", "observation": observation}
    data = await asyncio.to_thread(_read, Path(checkout).resolve(), locator["path"])
    source = _symbols(data)[locator["symbol"]][0] if locator.get("symbol") else data
    if digest(source) != observation["content_digest"]:
        return {"status": "unverified", "detail": "source changed before capture"}
    client = lifecycle.client
    event = await client.append_event(
        scope_id=lifecycle.spec.scopes.default_write,
        idempotency_key=f"source:{uuid.uuid4()}",
        source={
            "kind": "code_observation",
            "ref": resource_key(observation["resource"]),
            "revision": observation["snapshot"]["source_revision"],
        },
        content=source.decode("utf-8", errors="replace"),
        attributes={"resource_observation": observation},
    )
    record = await client.propose_record(
        {
            "type": record_type,
            "scope_id": lifecycle.spec.scopes.default_write,
            "payload": payload,
            "source_event_ids": [event["id"]],
            "assertion": "reported",
            "requires_grounding": True,
        }
    )
    evidence = await client.add_evidence(
        record["revision"]["id"],
        {
            "property": "source_supports_assertion",
            "subject_ref": resource_key(observation["resource"]),
            "subject_revision": observation["content_digest"],
            "observed_at": observation["observed_at"],
            "method": support["method"],
            "result": support["result"],
            "limitations": support.get("limitations", ""),
        },
    )
    grounding = await client.create_grounding(
        record["record_id"],
        {
            "expected_revision_id": record["revision"]["id"],
            "source_event_id": event["id"],
            "evidence_id": evidence["id"],
            "baseline": observation,
        },
    )
    return {
        "status": "accepted",
        "record": record,
        "grounding": grounding,
        "assertion_support": support["result"],
    }
