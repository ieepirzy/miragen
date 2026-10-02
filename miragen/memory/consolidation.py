"""Conservative overlapping-memory proposals for the existing bounded worker."""

from __future__ import annotations


class ConsolidationConflict(Exception):
    """Expected revisions changed; a new bounded candidate set is required."""


async def process_overlap(client, job, proposer=None):
    candidates = job["payload"]["candidates"]
    if not 1 <= len(candidates) <= 20:
        raise ValueError("overlap candidate limit is 20")
    records = []
    for candidate in candidates:
        record = await client.get_record(candidate["record_id"])
        if record["revision"]["id"] != candidate["revision_id"]:
            # Loimi rechecks under row locks, too. The worker never renews the
            # job's expected revisions silently following a concurrent edit.
            raise ConsolidationConflict(
                "candidate revision changed; create a fresh bounded proposal"
            )
        records.append(record)
    if proposer is not None:
        proposals = await proposer(records)
    else:
        proposals = []
        used = set()
        for i, target in enumerate(records):
            if target["record_id"] in used:
                continue
            for source in records[i + 1 :]:
                if source["record_id"] in used:
                    continue
                a, b = source["revision"], target["revision"]
                # No semantic equivalence inference: exact complete payloads,
                # same source roots, temporal/assertion metadata and no claims.
                same = (
                    source["type"] == target["type"]
                    and not source.get("slot_id")
                    and not target.get("slot_id")
                    and source["scope_id"] == target["scope_id"]
                    and not source.get("groundings")
                    and not target.get("groundings")
                    and source.get("roots") == target.get("roots")
                    and all(
                        a.get(k) == b.get(k)
                        for k in (
                            "payload",
                            "assertion",
                            "observed_at",
                            "valid_from",
                            "valid_to",
                        )
                    )
                )
                if same:
                    proposals.append(
                        {
                            "action": "merge",
                            "source_record_id": source["record_id"],
                            "target_record_id": target["record_id"],
                            "evidence_ids": [],
                            "reason": "identical complete representation of the same source roots",
                        }
                    )
                    used.add(source["record_id"])
    return await client.consolidate({"candidates": candidates, "proposals": proposals})
