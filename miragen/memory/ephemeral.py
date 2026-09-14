"""`backend: ephemeral` — an in-process, NON-DURABLE implementation of the
miragen memory backend protocol (docs/memory-backend-protocol.md).

Purpose: `pip install miragen` exercises the full memory lifecycle —
packets, tools, hooks, worker-free — with zero external services, and the
protocol keeps a permanent second implementation, which is what keeps a
protocol honest. It serves the same wire contract through an in-process
httpx transport, so `MemoryClient` and the conformance suite run against
it unchanged.

NOT for production: state lives in this process and dies with it. The
lifespan says so loudly at boot. Deliberately unimplemented: the worker
surface (jobs/projections/embeddings — no background machinery to feed)
and RLS (capability checks are in code; single-process trust domain).
The conformance suite treats those as a declared-optional capability.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

_TOKEN_PREFIX = "lmm_ephemeral_"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _err(status: int, code: str, message: str, details: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json={
        "error": {"code": code, "message": message, "details": details or {}}
    })


class EphemeralMemoryService:
    def __init__(self, *, operator_token: str = "ephemeral-operator") -> None:
        self.operator_token = operator_token
        self.principals: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}          # sha256 -> principal id
        self.scopes: dict[str, dict] = {}
        self.grants: set[tuple[str, str, str]] = set()
        self.predicates: dict[tuple[str, str], dict] = {}
        self.events: dict[str, dict] = {}
        self.idempotency: dict[tuple[str, str], str] = {}
        self.records: dict[str, dict] = {}
        self.revisions: dict[str, dict] = {}
        self.slots: dict[str, dict] = {}          # slot_id -> {..., version}
        self.segments: dict[str, list[dict]] = {} # slot_id -> segments
        self.resolutions: dict[str, list[dict]] = {}
        self.contexts: dict[str, dict] = {}
        self.manifests: list[dict] = []
        self.ledger: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # ── auth ───────────────────────────────────────────────────────────────

    def _bearer(self, request: httpx.Request) -> str | None:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        return token if scheme.lower() == "bearer" and token else None

    def _principal(self, request: httpx.Request) -> str | None:
        token = self._bearer(request)
        if token is None:
            return None
        principal = self.tokens.get(_sha(token))
        if principal is None or self.principals.get(principal, {}).get("disabled"):
            return None
        return principal

    def _can(self, principal: str, scope: str, verb: str) -> bool:
        return (principal, scope, verb) in self.grants

    # ── routing ────────────────────────────────────────────────────────────

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/memory/v1")
        body = json.loads(request.content) if request.content else {}
        params = dict(request.url.params)
        method = request.method

        if path.startswith("/admin/") or path.endswith("/erase"):
            if self._bearer(request) != self.operator_token:
                return _err(401, "unauthenticated",
                            "operator routes require the operator token")
            return self._admin(method, path, body, params)

        principal = self._principal(request)
        if principal is None:
            return _err(401, "unauthenticated",
                        "unknown, revoked or disabled memory principal token")
        return self._data(method, path, body, params, principal)

    # ── operator surface ──────────────────────────────────────────────────

    def _admin(self, method: str, path: str, body: dict, params: dict) -> httpx.Response:
        if method == "POST" and path == "/admin/principals":
            if body["id"] in self.principals:
                return _err(409, "illegal_transition", f"principal '{body['id']}' already exists")
            token = _TOKEN_PREFIX + uuid.uuid4().hex
            self.principals[body["id"]] = {"kind": body["kind"], "disabled": False}
            self.tokens[_sha(token)] = body["id"]
            return httpx.Response(201, json={"id": body["id"], "kind": body["kind"],
                                             "token": token})
        if method == "POST" and re.fullmatch(r"/admin/principals/[^/]+/tokens", path):
            principal_id = path.split("/")[3]
            if principal_id not in self.principals:
                return _err(404, "not_found", f"unknown principal '{principal_id}'")
            token = _TOKEN_PREFIX + uuid.uuid4().hex
            self.tokens[_sha(token)] = principal_id
            return httpx.Response(201, json={"principal_id": principal_id, "token": token})
        if method == "POST" and path == "/admin/scopes":
            if body["id"] in self.scopes:
                return _err(409, "illegal_transition", f"scope '{body['id']}' already exists")
            self.scopes[body["id"]] = {"kind": body["kind"]}
            return httpx.Response(201, json={"id": body["id"], "kind": body["kind"]})
        if method == "POST" and path == "/admin/grants":
            if body["principal_id"] not in self.principals or body["scope_id"] not in self.scopes:
                return _err(404, "not_found", "unknown principal or scope")
            for verb in body["verbs"]:
                self.grants.add((body["principal_id"], body["scope_id"], verb))
            return httpx.Response(201, json=body)
        if method == "POST" and path == "/admin/predicates":
            key = (body["scope_id"], body["id"])
            existing = self.predicates.get(key)
            if existing is not None:
                if (existing["cardinality"] != body["cardinality"]
                        or existing["authority_source_kinds"] != body.get(
                            "authority_source_kinds", [])):
                    return _err(409, "illegal_transition",
                                "predicate already registered with different semantics")
                return httpx.Response(201, json=existing | {"created": False})
            registration = {
                "scope_id": body["scope_id"], "id": body["id"],
                "cardinality": body["cardinality"],
                "authority_source_kinds": body.get("authority_source_kinds", []),
            }
            self.predicates[key] = registration
            return httpx.Response(201, json=registration | {"created": True})
        if method == "POST" and path.endswith("/erase"):
            event_id = path.split("/")[2]
            event = self.events.get(event_id)
            if event is None:
                return _err(404, "not_found", f"unknown source event '{event_id}'")
            event["content"] = None
            event["erased_at"] = _iso(_now())
            event["generation"] += 1
            tombstoned = 0
            for record in self.records.values():
                if event_id in record["root_links"] and not record.get("erased"):
                    revision = self.revisions[record["current_revision_id"]]
                    revision["payload"] = {"erased": True}
                    revision["erased_at"] = _iso(_now())
                    record["admission"] = "archived"
                    tombstoned += 1
            self.ledger.append({"target_kind": "source_event", "target_id": event_id})
            return httpx.Response(200, json=self._event_view(event)
                                  | {"dependent_records_tombstoned": tombstoned})
        return _err(404, "not_found", f"unhandled admin route {path}")

    # ── data surface ──────────────────────────────────────────────────────

    def _data(self, method, path, body, params, principal) -> httpx.Response:
        if method == "POST" and path == "/events":
            return self._append_event(body, principal)
        if method == "GET" and re.fullmatch(r"/events/[^/]+", path):
            return self._get_event(path.split("/")[-1], principal)
        if method == "POST" and path.endswith("/retract") and path.startswith("/events/"):
            return self._retract_event(path.split("/")[2], principal)
        if method == "POST" and path == "/records":
            return self._propose(body, principal)
        if method == "GET" and re.fullmatch(r"/records/[^/]+", path):
            return self._get_record(path.split("/")[-1], principal,
                                    seq=int(params["seq"]) if "seq" in params else None)
        if method == "POST" and path.endswith("/retract") and path.startswith("/records/"):
            return self._retract_record(path.split("/")[2], principal)
        if method == "POST" and path.endswith("/correct"):
            return self._correct(path.split("/")[2], body, principal)
        if method == "GET" and path == "/claims":
            return self._claims(params, principal)
        if method == "POST" and path.startswith("/slots/") and path.endswith("/resolve"):
            return self._resolve(path.split("/")[2], body, principal)
        if method == "POST" and path == "/contexts":
            return self._create_context(body, principal)
        if method == "GET" and path.startswith("/contexts/"):
            context = self.contexts.get(path.split("/")[-1])
            if context is None or not self._can(principal, context["scope_id"], "read"):
                return _err(404, "not_found", "unknown work context")
            return httpx.Response(200, json=context)
        if method == "PATCH" and path.startswith("/contexts/"):
            return self._patch_context(path.split("/")[-1], body, principal)
        if method == "POST" and path == "/search":
            return self._search(body, principal)
        if method == "POST" and path == "/manifests":
            self.manifests.append(body | {"id": str(uuid.uuid4())})
            return httpx.Response(201, json={"id": self.manifests[-1]["id"],
                                             "created_at": _iso(_now())})
        return _err(404, "not_found", f"unhandled route {path}")

    # ── events ────────────────────────────────────────────────────────────

    def _event_view(self, event: dict) -> dict:
        return {k: v for k, v in event.items() if k != "producer_key"}

    def _append_event(self, body: dict, principal: str) -> httpx.Response:
        scope = body["scope_id"]
        if scope not in self.scopes or not self._can(principal, scope, "propose"):
            return _err(404, "not_found",
                        f"scope '{scope}' does not exist or grants you no propose capability")
        digest = _sha(body["content"])
        key = (principal, body["idempotency_key"])
        if key in self.idempotency:
            existing = self.events[self.idempotency[key]]
            if existing["content_digest"] != digest:
                return _err(409, "illegal_transition",
                            "idempotency_key was already used for different content")
            return httpx.Response(201, json=self._event_view(existing) | {"created": False})
        event = {
            "id": str(uuid.uuid4()), "scope_id": scope, "producer_id": principal,
            "idempotency_key": body["idempotency_key"],
            "source": body["source"], "content": body["content"],
            "content_digest": digest, "attributes": body.get("attributes", {}),
            "occurred_at": body.get("occurred_at"), "received_at": _iso(_now()),
            "generation": 1, "retracted_at": None, "erased_at": None,
        }
        self.events[event["id"]] = event
        self.idempotency[key] = event["id"]
        return httpx.Response(201, json=self._event_view(event) | {"created": True})

    def _readable_event(self, event_id: str, principal: str) -> dict | None:
        event = self.events.get(event_id)
        if event is None or not self._can(principal, event["scope_id"], "read"):
            return None
        return event

    def _get_event(self, event_id: str, principal: str) -> httpx.Response:
        event = self._readable_event(event_id, principal)
        if event is None:
            return _err(404, "not_found", f"unknown source event '{event_id}'")
        return httpx.Response(200, json=self._event_view(event))

    def _retract_event(self, event_id: str, principal: str) -> httpx.Response:
        event = self.events.get(event_id)
        if event is None or not self._can(principal, event["scope_id"], "retract"):
            return _err(404, "not_found", f"unknown source event '{event_id}'")
        if event["retracted_at"] is None:
            event["retracted_at"] = _iso(_now())
        event["generation"] += 1
        return httpx.Response(200, json=self._event_view(event))

    # ── records / claims ──────────────────────────────────────────────────

    def _verify_roots(self, ids: list[str], principal: str):
        roots = []
        missing, dead = [], []
        for event_id in ids:
            event = self._readable_event(event_id, principal)
            if event is None:
                missing.append(event_id)
            elif event["retracted_at"] or event["erased_at"]:
                dead.append(event_id)
            else:
                roots.append(event)
        if missing:
            return None, _err(400, "invalid_request",
                              "unknown or inaccessible source event(s)",
                              {"source_event_ids": missing})
        if dead:
            return None, _err(400, "invalid_request",
                              "source event(s) are retracted or erased and cannot "
                              "root new memory", {"source_event_ids": dead})
        return roots, None

    def _slot_key(self, scope: str, claim: dict) -> str:
        return _sha("\x1f".join([
            scope, claim["subject"], claim["predicate"],
            json.dumps(claim.get("qualifiers", {}), sort_keys=True),
        ]))

    def _propose(self, body: dict, principal: str) -> httpx.Response:
        scope = body["scope_id"]
        if scope not in self.scopes or not self._can(principal, scope, "propose"):
            return _err(404, "not_found",
                        f"scope '{scope}' does not exist or grants you no propose capability")
        claim = body.get("claim")
        if (body["type"] == "claim") != (claim is not None):
            return _err(400, "invalid_request",
                        "claim records require `claim`; other types must omit it")
        roots, error = self._verify_roots(body.get("source_event_ids", []), principal)
        if error is not None:
            return error
        if body.get("quarantine"):
            admission = "quarantined"
        else:
            admission = "accepted" if roots else "candidate"

        registration = None
        slot_id = None
        slot_cardinality = None
        if claim is not None:
            registration = self.predicates.get((scope, claim["predicate"]))
            effective = registration["cardinality"] if registration else "observation_only"
            requested = claim.get("cardinality")
            if requested is not None and requested != effective:
                return _err(409, "illegal_transition",
                            f"predicate '{claim['predicate']}' is "
                            + (f"registered as {effective}" if registration
                               else "unregistered (claims are observation_only)"))
            slot_cardinality = effective
            slot_id = self._slot_key(scope, claim)
            slot = self.slots.setdefault(slot_id, {
                "id": slot_id, "scope_id": scope, "subject": claim["subject"],
                "predicate": claim["predicate"],
                "qualifiers": claim.get("qualifiers", {}),
                "cardinality": effective, "version": 0,
            })
            if slot["cardinality"] != effective:
                return _err(409, "illegal_transition",
                            "slot exists with different cardinality")

        if body.get("record_id"):
            return self._revise(body, principal, admission, roots)

        record_id = str(uuid.uuid4())
        revision = self._new_revision(record_id, 1, body, principal)
        record = {
            "record_id": record_id, "type": body["type"], "scope_id": scope,
            "admission": admission, "producer_id": principal, "slot_id": slot_id,
            "slot_cardinality": slot_cardinality,
            "current_revision_id": revision["id"], "created_at": _iso(_now()),
            "root_links": {r["id"]: r["generation"] for r in roots},
        }
        self.records[record_id] = record

        resolution = None
        if slot_id and slot_cardinality == "single_valued" and admission == "accepted":
            allowed = registration["authority_source_kinds"]
            if allowed and not any(r["source"]["kind"] in allowed for r in roots):
                resolution = {"outcome": "withheld",
                              "reason": "no root with head-moving authority for this predicate",
                              "required_source_kinds": allowed}
            else:
                resolution = self._auto_resolve(slot_id, revision, body)
        return httpx.Response(201, json=self._record_view(record)
                              | {"resolution": resolution,
                                 "predicate_registered": registration is not None})

    def _new_revision(self, record_id: str, seq: int, body: dict, principal: str,
                      *, supersedes: str | None = None,
                      correction_of: str | None = None) -> dict:
        revision = {
            "id": str(uuid.uuid4()), "record_id": record_id, "seq": seq,
            "payload": body["payload"],
            "assertion": body.get("assertion", "reported"),
            "lifecycle": "active",
            "valid_from": body.get("valid_from"), "valid_to": body.get("valid_to"),
            "recorded_at": _iso(_now()), "extractor": body.get("extractor"),
            "supersedes_revision_id": supersedes, "correction_of": correction_of,
            "producer_id": principal, "erased_at": None,
        }
        self.revisions[revision["id"]] = revision
        return revision

    def _revise(self, body: dict, principal: str, admission: str, roots) -> httpx.Response:
        record = self.records.get(str(body["record_id"]))
        if record is None or not self._can(principal, record["scope_id"], "propose"):
            return _err(404, "not_found", f"unknown record '{body['record_id']}'")
        current = self.revisions[record["current_revision_id"]]
        if current["seq"] != body.get("expected_seq"):
            return _err(409, "illegal_transition",
                        "expected_seq does not match the record's current revision",
                        {"current_seq": current["seq"]})
        revision = self._new_revision(record["record_id"], current["seq"] + 1, body,
                                      principal, supersedes=current["id"])
        current["lifecycle"] = "superseded"
        record["current_revision_id"] = revision["id"]
        record["admission"] = admission
        for root in roots:
            record["root_links"][root["id"]] = root["generation"]
        return httpx.Response(201, json=self._record_view(record)
                              | {"resolution": None, "predicate_registered": None})

    def _auto_resolve(self, slot_id: str, revision: dict, body: dict) -> dict:
        valid_from = _parse(body.get("valid_from")) or _now()
        valid_to = _parse(body.get("valid_to"))
        segments = self.segments.setdefault(slot_id, [])
        overlapping = [s for s in segments if self._overlaps(s, valid_from, valid_to)]
        resolved_overlaps = [s for s in overlapping if s["kind"] == "resolved"]
        unambiguous = not overlapping or (
            valid_to is None
            and len(overlapping) == 1
            and resolved_overlaps == overlapping
            and overlapping[0]["upper"] is None
            and (overlapping[0]["lower"] is None or overlapping[0]["lower"] <= valid_from)
        )
        slot = self.slots[slot_id]
        if unambiguous:
            for segment in resolved_overlaps:
                if segment["lower"] is None or segment["lower"] < valid_from:
                    segment["upper"] = valid_from
                else:
                    segments.remove(segment)
            segments.append({"lower": valid_from, "upper": valid_to,
                             "kind": "resolved", "revision_ids": [revision["id"]]})
            outcome = "resolved"
        else:
            contenders = {revision["id"]}
            for segment in overlapping:
                contenders.update(segment["revision_ids"])
            segments.append({"lower": valid_from, "upper": valid_to,
                             "kind": "conflict",
                             "revision_ids": sorted(contenders)})
            outcome = "conflict"
        slot["version"] += 1
        self.resolutions.setdefault(slot_id, []).append({
            "slot_version": slot["version"], "decided_at": _now(),
            "segments": [dict(s) for s in segments],
        })
        return {"outcome": outcome, "slot_version": slot["version"]}

    @staticmethod
    def _overlaps(segment: dict, lower: datetime, upper: datetime | None) -> bool:
        seg_lower, seg_upper = segment["lower"], segment["upper"]
        if seg_upper is not None and seg_upper <= lower:
            return False
        if upper is not None and seg_lower is not None and seg_lower >= upper:
            return False
        return True

    def _roots_status(self, record: dict, principal: str) -> dict:
        roots = []
        valid = True
        for event_id, generation in record["root_links"].items():
            event = self._readable_event(event_id, principal)
            if event is None:
                status = "inaccessible"
            elif event["erased_at"]:
                status = "erased"
            elif event["retracted_at"]:
                status = "retracted"
            elif event["generation"] != generation:
                status = "superseded"
            else:
                status = "valid"
            if status != "valid":
                valid = False
            roots.append({"event_id": event_id, "status": status})
        return {"roots": roots, "roots_valid": valid}

    def _record_view(self, record: dict, *, revision: dict | None = None,
                     principal: str | None = None) -> dict:
        revision = revision or self.revisions[record["current_revision_id"]]
        view = {
            "record_id": record["record_id"], "type": record["type"],
            "scope_id": record["scope_id"], "admission": record["admission"],
            "slot_id": record["slot_id"],
            "slot_cardinality": record.get("slot_cardinality"),
            "producer_id": record["producer_id"],
            "historical": revision["id"] != record["current_revision_id"],
            "revision": revision,
        }
        if principal is not None:
            view |= self._roots_status(record, principal)
        else:
            view |= {"roots": [], "roots_valid": True}
        return view

    def _get_record(self, record_id: str, principal: str, *, seq: int | None) -> httpx.Response:
        record = self.records.get(record_id)
        if record is None or not self._can(principal, record["scope_id"], "read"):
            return _err(404, "not_found", f"unknown record '{record_id}'")
        revision = None
        if seq is not None:
            matches = [r for r in self.revisions.values()
                       if r["record_id"] == record_id and r["seq"] == seq]
            if not matches:
                return _err(404, "not_found", f"record '{record_id}' has no revision seq {seq}")
            revision = matches[0]
        return httpx.Response(200, json=self._record_view(
            record, revision=revision, principal=principal))

    def _retract_record(self, record_id: str, principal: str) -> httpx.Response:
        record = self.records.get(record_id)
        if record is None or not self._can(principal, record["scope_id"], "retract"):
            return _err(404, "not_found", f"unknown record '{record_id}'")
        for revision in self.revisions.values():
            if revision["record_id"] == record_id:
                revision["lifecycle"] = "retracted"
        record["admission"] = "archived"
        return httpx.Response(200, json=self._record_view(record, principal=principal))

    def _correct(self, record_id: str, body: dict, principal: str) -> httpx.Response:
        record = self.records.get(record_id)
        if record is None or not self._can(principal, record["scope_id"], "propose"):
            return _err(404, "not_found", f"unknown record '{record_id}'")
        roots, error = self._verify_roots([str(i) for i in body["source_event_ids"]],
                                          principal)
        if error is not None:
            return error
        if not roots:
            return _err(400, "invalid_request", "correction requires evidence")
        current = self.revisions[record["current_revision_id"]]
        if current["seq"] != body["expected_seq"]:
            return _err(409, "illegal_transition", "expected_seq does not match",
                        {"current_seq": current["seq"]})
        revision = self._new_revision(
            record_id, current["seq"] + 1,
            {"payload": body["payload"],
             "valid_from": body.get("valid_from") or current["valid_from"],
             "valid_to": body.get("valid_to") or current["valid_to"]},
            principal, supersedes=current["id"], correction_of=current["id"],
        )
        current["lifecycle"] = "superseded"
        record["current_revision_id"] = revision["id"]
        record["admission"] = "accepted"
        for root in roots:
            record["root_links"][root["id"]] = root["generation"]

        resolution = None
        if record["slot_id"] and record.get("slot_cardinality") == "single_valued":
            for segment in self.segments.get(record["slot_id"], []):
                segment["revision_ids"] = [
                    revision["id"] if rid == current["id"] else rid
                    for rid in segment["revision_ids"]
                ]
            slot = self.slots[record["slot_id"]]
            slot["version"] += 1
            self.resolutions.setdefault(record["slot_id"], []).append({
                "slot_version": slot["version"], "decided_at": _now(),
                "segments": [dict(s) for s in self.segments[record["slot_id"]]],
            })
            resolution = {"outcome": "corrected", "slot_version": slot["version"]}
        return httpx.Response(200, json=self._record_view(record, principal=principal)
                              | {"resolution": resolution})

    def _resolve(self, slot_id: str, body: dict, principal: str) -> httpx.Response:
        slot = self.slots.get(slot_id)
        if slot is None or not self._can(principal, slot["scope_id"], "resolve"):
            return _err(404, "not_found", f"unknown claim slot '{slot_id}'")
        if slot["version"] != body["expected_version"]:
            return _err(409, "illegal_transition", "expected_version does not match",
                        {"current_version": slot["version"]})
        slot_revisions = {r["id"] for r in self.revisions.values()
                          if self.records[r["record_id"]]["slot_id"] == slot_id}
        accepted = {r["id"] for r in self.revisions.values()
                    if self.records[r["record_id"]]["slot_id"] == slot_id
                    and self.records[r["record_id"]]["admission"] == "accepted"}
        new_segments = []
        for segment in body["segments"]:
            rid = str(segment["revision_id"])
            if rid not in slot_revisions:
                return _err(400, "invalid_request",
                            f"revision '{rid}' does not belong to slot '{slot_id}'")
            if rid not in accepted:
                return _err(400, "invalid_request",
                            "candidates and quarantined content cannot take a current head")
            new_segments.append({"lower": _parse(segment.get("valid_from")),
                                 "upper": _parse(segment.get("valid_to")),
                                 "kind": "resolved", "revision_ids": [rid]})
        for a in new_segments:
            for b in new_segments:
                if a is not b and self._overlaps(a, b["lower"] or datetime.min.replace(
                        tzinfo=timezone.utc), b["upper"]):
                    return _err(400, "invalid_request",
                                "resolved segments overlap in valid time")
        self.segments[slot_id] = new_segments
        slot["version"] += 1
        self.resolutions.setdefault(slot_id, []).append({
            "slot_version": slot["version"], "decided_at": _now(),
            "segments": [dict(s) for s in new_segments],
        })
        return httpx.Response(200, json={"slot_id": slot_id,
                                         "slot_version": slot["version"],
                                         "segments": self._segment_views(slot_id)})

    def _segment_views(self, slot_id: str, *, at: datetime | None = None) -> list[dict]:
        views = []
        for segment in self.segments.get(slot_id, []):
            if at is not None and not self._covers(segment, at):
                continue
            views.append({
                "valid_from": _iso(segment["lower"]), "valid_to": _iso(segment["upper"]),
                "kind": segment["kind"], "revision_ids": segment["revision_ids"],
                "revisions": [self.revisions[r] for r in segment["revision_ids"]
                              if self.revisions[r]["erased_at"] is None],
            })
        return views

    @staticmethod
    def _covers(segment: dict, at: datetime) -> bool:
        return ((segment["lower"] is None or segment["lower"] <= at)
                and (segment["upper"] is None or segment["upper"] > at))

    def _claims(self, params: dict, principal: str) -> httpx.Response:
        scope = params["scope_id"]
        if not self._can(principal, scope, "read"):
            return httpx.Response(200, json={"claims": []})
        at = _parse(params.get("at")) or _now()
        as_of_record = _parse(params.get("as_of_record"))
        claims = []
        for slot in self.slots.values():
            if slot["scope_id"] != scope or slot["subject"] != params["subject"]:
                continue
            if params.get("predicate") and slot["predicate"] != params["predicate"]:
                continue
            entry: dict[str, Any] = {
                "slot_id": slot["id"], "subject": slot["subject"],
                "predicate": slot["predicate"], "qualifiers": slot["qualifiers"],
                "cardinality": slot["cardinality"], "slot_version": slot["version"],
            }
            if slot["cardinality"] == "single_valued":
                if as_of_record is not None:
                    decisions = [d for d in self.resolutions.get(slot["id"], [])
                                 if d["decided_at"] <= as_of_record]
                    entry["segments"] = []
                    if decisions:
                        for segment in decisions[-1]["segments"]:
                            if self._covers(segment, at):
                                entry["segments"].append({
                                    "valid_from": _iso(segment["lower"]),
                                    "valid_to": _iso(segment["upper"]),
                                    "kind": segment["kind"],
                                    "revision_ids": segment["revision_ids"],
                                    "historical": True,
                                })
                else:
                    entry["segments"] = self._segment_views(slot["id"], at=at)
            else:
                members = []
                for record in self.records.values():
                    if record["slot_id"] != slot["id"] or record["admission"] != "accepted":
                        continue
                    revision = self.revisions[record["current_revision_id"]]
                    if revision["lifecycle"] != "active" or revision["erased_at"]:
                        continue
                    lower = _parse(revision["valid_from"])
                    upper = _parse(revision["valid_to"])
                    if (lower is None or lower <= at) and (upper is None or upper > at):
                        members.append(revision)
                entry["members"] = members
            claims.append(entry)
        return httpx.Response(200, json={"scope_id": scope, "subject": params["subject"],
                                         "at": _iso(at), "claims": claims})

    # ── contexts ──────────────────────────────────────────────────────────

    def _create_context(self, body: dict, principal: str) -> httpx.Response:
        scope = body["scope_id"]
        if scope not in self.scopes or not self._can(principal, scope, "propose"):
            return _err(404, "not_found",
                        f"scope '{scope}' does not exist or grants you no propose capability")
        context = {
            "id": str(uuid.uuid4()), "scope_id": scope,
            "kind": body.get("kind", "task"), "title": body.get("title", ""),
            "state": body.get("state", {}), "state_revision": 1,
            "created_by": principal, "updated_at": _iso(_now()),
        }
        self.contexts[context["id"]] = context
        return httpx.Response(201, json=context)

    def _patch_context(self, context_id: str, body: dict, principal: str) -> httpx.Response:
        context = self.contexts.get(context_id)
        if context is None or not self._can(principal, context["scope_id"], "propose"):
            return _err(404, "not_found", "unknown work context")
        if context["state_revision"] != body["expected_revision"]:
            return _err(409, "illegal_transition", "expected_revision does not match",
                        {"current_revision": context["state_revision"]})
        if body.get("state") is not None:
            context["state"] = body["state"]
        elif body.get("patch"):
            merged = {**context["state"], **body["patch"]}
            context["state"] = {k: v for k, v in merged.items() if v is not None}
        if body.get("title") is not None:
            context["title"] = body["title"]
        context["state_revision"] += 1
        context["updated_at"] = _iso(_now())
        return httpx.Response(200, json=context)

    # ── search ────────────────────────────────────────────────────────────

    def _search(self, body: dict, principal: str) -> httpx.Response:
        if not body.get("query_text") and body.get("query_embedding") is None:
            return _err(400, "invalid_request", "search requires query_text or query_embedding")
        scopes = [s for s in body["scope_ids"] if self._can(principal, s, "read")]
        tokens = set(re.findall(r"[0-9a-zA-Zà-öø-ÿÀ-ÖØ-ß]+",
                                (body.get("query_text") or "").lower()))
        skipped = {"stale": 0, "roots_invalid": 0, "ineligible": 0}
        scored = []
        for record in self.records.values():
            if record["scope_id"] not in scopes or record["admission"] != "accepted":
                continue
            revision = self.revisions[record["current_revision_id"]]
            if revision["lifecycle"] != "active" or revision["erased_at"]:
                continue
            payload = revision["payload"]
            text_parts = [str(payload.get(k, "")) for k in ("text", "value")]
            if record["slot_id"]:
                slot = self.slots[record["slot_id"]]
                text_parts += [slot["subject"], slot["predicate"]]
            words = set(re.findall(r"[0-9a-zA-Zà-öø-ÿÀ-ÖØ-ß]+",
                                   " ".join(text_parts).lower()))
            overlap = len(tokens & words)
            if overlap == 0:
                continue
            roots = self._roots_status(record, principal)
            if not roots["roots_valid"]:
                skipped["roots_invalid"] += 1
                continue
            slot_info = None
            if record["slot_id"]:
                slot = self.slots[record["slot_id"]]
                head = any(
                    revision["id"] in s["revision_ids"]
                    for s in self.segments.get(record["slot_id"], [])
                    if s["kind"] == "resolved" and self._covers(s, _now())
                )
                slot_info = {"subject": slot["subject"], "predicate": slot["predicate"],
                             "cardinality": slot["cardinality"], "is_current_head": head}
            scored.append((overlap, {
                "record_id": record["record_id"], "revision_id": revision["id"],
                "type": record["type"], "scope_id": record["scope_id"],
                "payload": payload, "recorded_at": revision["recorded_at"],
                "slot": slot_info, "roots_valid": True, "channels": ["lexical"],
            }))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["record_id"]))
        items = [card for _, card in scored[:body.get("limit", 8)]]
        return httpx.Response(200, json={
            "items": items, "channel_hits": {"lexical": len(scored)}, "skipped": skipped,
        })


def provision_profile(service: "EphemeralMemoryService", profile_name: str,
                      scopes) -> str:
    """Idempotent zero-config provisioning for `backend: ephemeral`: the
    profile's principal, every scope it names, and the matching grants —
    returning a principal token. What a real deployment does through the
    operator surface, done in-process because the trust domain IS this
    process."""
    if profile_name not in service.principals:
        service.principals[profile_name] = {"kind": "agent", "disabled": False}
    token = _TOKEN_PREFIX + profile_name
    service.tokens[_sha(token)] = profile_name
    for scope_id in {*scopes.read, *scopes.propose, scopes.default_write}:
        service.scopes.setdefault(scope_id, {"kind": "profile"})
    for scope_id in scopes.read:
        service.grants.add((profile_name, scope_id, "read"))
    for scope_id in scopes.propose:
        for verb in ("propose", "resolve", "retract"):
            service.grants.add((profile_name, scope_id, verb))
    return token


# One shared instance per process for `backend: ephemeral` profiles — the
# whole point is that tools, boundary and worker-free flows all see the
# same state inside this container.
_shared: EphemeralMemoryService | None = None


def shared_service() -> EphemeralMemoryService:
    global _shared
    if _shared is None:
        _shared = EphemeralMemoryService()
    return _shared
