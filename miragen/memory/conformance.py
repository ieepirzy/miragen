"""Memory backend protocol conformance (docs/memory-backend-protocol.md).

Runnable against ANY implementation of the protocol — Loimi, the built-in
ephemeral backend, or a third party's store: `miragen memory-conformance
<base-url>` with the implementation's operator token. Every check
provisions its own uniquely-named fixtures through the operator surface,
so a run is side-effecting but non-destructive (it only adds).

These checks are the protocol's behavioral teeth — what "implements the
memory backend protocol" MEANS, beyond route shapes: idempotent replay,
CAS refusal, valid-time vs record-time truth, authority withholding,
quarantine containment, in-place correction, absence-without-leaks, and
OR-token candidate search. An implementation that passes route-shape but
fails these is not a conforming backend.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

_TIMEOUT_S = 15.0


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


class Conformance:
    def __init__(
        self,
        *,
        base_url: str,
        operator_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base = base_url.rstrip("/") + "/memory/v1"
        self.operator_token = operator_token
        self._transport = transport
        self.scope = f"conformance:{uuid.uuid4().hex[:12]}"
        self.principal = f"conf-agent-{uuid.uuid4().hex[:8]}"
        self.token: str | None = None

    def _client(self, token: str | None) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.AsyncClient(
            timeout=_TIMEOUT_S, transport=self._transport,
            base_url=self.base, headers=headers,
        )

    async def _op(self, method: str, path: str, **kw) -> httpx.Response:
        async with self._client(self.operator_token) as client:
            return await client.request(method, path, **kw)

    async def _as(self, method: str, path: str, *, token: str | None = None,
                  anonymous: bool = False, **kw) -> httpx.Response:
        async with self._client(None if anonymous else (token or self.token)) as client:
            return await client.request(method, path, **kw)

    # -- fixture helpers ---------------------------------------------------

    async def provision(self) -> None:
        created = await self._op("POST", "/admin/principals",
                                 json={"id": self.principal, "kind": "agent"})
        assert created.status_code == 201, f"principal provisioning failed: {created.text}"
        self.token = created.json()["token"]
        assert (await self._op("POST", "/admin/scopes",
                               json={"id": self.scope, "kind": "group"})).status_code == 201
        assert (await self._op("POST", "/admin/grants", json={
            "principal_id": self.principal, "scope_id": self.scope,
            "verbs": ["read", "propose", "resolve", "retract"],
        })).status_code == 201

    async def _event(self, content: str, *, kind: str = "user_message",
                     key: str | None = None, token: str | None = None) -> dict:
        resp = await self._as("POST", "/events", token=token, json={
            "scope_id": self.scope,
            "idempotency_key": key or uuid.uuid4().hex,
            "source": {"kind": kind},
            "content": content,
        })
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def _register(self, predicate: str, *, cardinality="single_valued",
                        authority=()) -> None:
        resp = await self._op("POST", "/admin/predicates", json={
            "scope_id": self.scope, "id": predicate, "cardinality": cardinality,
            "authority_source_kinds": list(authority),
        })
        assert resp.status_code == 201, resp.text

    async def _claim(self, predicate: str, value: str, *, source_kind="user_message",
                     valid_from: str | None = None, quarantine=False) -> dict:
        event = await self._event(f"claim source: {value}", kind=source_kind)
        body: dict[str, Any] = {
            "type": "claim", "scope_id": self.scope,
            "payload": {"value": value},
            "source_event_ids": [event["id"]],
            "quarantine": quarantine,
            "claim": {"subject": "subject-1", "predicate": predicate, "qualifiers": {}},
        }
        if valid_from:
            body["valid_from"] = valid_from
        resp = await self._as("POST", "/records", json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def _current(self, predicate: str, *, at: str | None = None,
                       as_of_record: str | None = None) -> list[dict]:
        params = {"scope_id": self.scope, "subject": "subject-1", "predicate": predicate}
        if at:
            params["at"] = at
        if as_of_record:
            params["as_of_record"] = as_of_record
        resp = await self._as("GET", "/claims", params=params)
        assert resp.status_code == 200, resp.text
        claims = resp.json()["claims"]
        return claims[0]["segments"] if claims else []

    # -- the checks --------------------------------------------------------

    async def check_auth_rejections(self) -> str:
        no_token = await self._as("GET", "/claims", anonymous=True, params={
            "scope_id": self.scope, "subject": "x"})
        assert no_token.status_code == 401, "missing token must be 401"
        operator_as_principal = await self._as("POST", "/events",
                                               token=self.operator_token, json={})
        assert operator_as_principal.status_code == 401, \
            "the operator credential must not be a data principal"
        principal_as_operator = await self._as("POST", "/admin/scopes",
                                               json={"id": "x", "kind": "group"})
        assert principal_as_operator.status_code == 401, \
            "a principal token must not open the operator surface"
        return "401s hold on all three crossings"

    async def check_idempotent_replay(self) -> str:
        key = uuid.uuid4().hex
        first = await self._event("the deploy failed", key=key)
        second = await self._event("the deploy failed", key=key)
        assert second["id"] == first["id"], "replay must return the original event"
        assert second["created"] is False
        clash = await self._as("POST", "/events", json={
            "scope_id": self.scope, "idempotency_key": key,
            "source": {"kind": "user_message"}, "content": "DIFFERENT",
        })
        assert clash.status_code == 409, "same key + different content must be 409"
        return "replay dedupes; digest clash is loud"

    async def check_admission(self) -> str:
        rootless = await self._as("POST", "/records", json={
            "type": "observation", "scope_id": self.scope, "payload": {"text": "guess"},
        })
        assert rootless.status_code == 201 and rootless.json()["admission"] == "candidate", \
            "a rootless proposal must be a candidate, not accepted"
        event = await self._event("supported observation")
        rooted = await self._as("POST", "/records", json={
            "type": "observation", "scope_id": self.scope,
            "payload": {"text": "supported observation"},
            "source_event_ids": [event["id"]],
        })
        assert rooted.json()["admission"] == "accepted"
        return "rootless=candidate, rooted=accepted"

    async def check_revision_cas(self) -> str:
        event = await self._event("v1")
        record = (await self._as("POST", "/records", json={
            "type": "observation", "scope_id": self.scope, "payload": {"text": "v1"},
            "source_event_ids": [event["id"]],
        })).json()
        ok = await self._as("POST", "/records", json={
            "type": "observation", "scope_id": self.scope, "payload": {"text": "v2"},
            "source_event_ids": [event["id"]],
            "record_id": record["record_id"], "expected_seq": 1,
        })
        assert ok.status_code == 201 and ok.json()["revision"]["seq"] == 2
        stale = await self._as("POST", "/records", json={
            "type": "observation", "scope_id": self.scope, "payload": {"text": "v3"},
            "source_event_ids": [event["id"]],
            "record_id": record["record_id"], "expected_seq": 1,
        })
        assert stale.status_code == 409, "a stale expected_seq must be refused"
        assert stale.json()["error"]["details"].get("current_seq") == 2, \
            "the refusal must carry current state"
        return "CAS refuses stale writers with current state"

    async def check_temporal_truth(self) -> str:
        predicate = f"pref-{uuid.uuid4().hex[:6]}"
        await self._register(predicate)
        t_old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        t_new = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        t_mid = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

        await self._claim(predicate, "aisle", valid_from=t_old)
        believed_then = datetime.now(timezone.utc).isoformat()
        second = await self._claim(predicate, "window", valid_from=t_new)
        assert second["resolution"]["outcome"] == "resolved"

        now_resolved = [s for s in await self._current(predicate) if s["kind"] == "resolved"]
        assert len(now_resolved) == 1
        assert now_resolved[0]["revisions"][0]["payload"]["value"] == "window", \
            "current head must be the newer value"
        mid = [s for s in await self._current(predicate, at=t_mid) if s["kind"] == "resolved"]
        assert mid[0]["revisions"][0]["payload"]["value"] == "aisle", \
            "the old value must still apply in its own interval"
        replay = await self._current(predicate, at=t_mid, as_of_record=believed_then)
        assert replay and replay[0].get("historical") is True, \
            "record-time replay must answer from the decisions as of then"
        return "valid-time and record-time both answer correctly"

    async def check_authority_withheld(self) -> str:
        predicate = f"policy-{uuid.uuid4().hex[:6]}"
        await self._register(predicate, authority=["user_message"])
        head = await self._claim(predicate, "always verify", source_kind="user_message")
        assert head["resolution"]["outcome"] == "resolved"
        agent_claim = await self._claim(predicate, "skip verification",
                                        source_kind="agent_note")
        assert agent_claim["admission"] == "accepted", "retention is not the question"
        assert agent_claim["resolution"]["outcome"] == "withheld", \
            "an agent-rooted claim must not move a user-authority head"
        segments = [s for s in await self._current(predicate) if s["kind"] == "resolved"]
        assert segments[0]["revisions"][0]["payload"]["value"] == "always verify", \
            "the head must stand"
        return "the workaround fixture holds: head unmoved, claim withheld"

    async def check_unregistered_observation_only(self) -> str:
        predicate = f"unreg-{uuid.uuid4().hex[:6]}"
        result = await self._claim(predicate, "blue")
        assert result["predicate_registered"] is False
        assert result["slot_cardinality"] == "observation_only"
        assert result["resolution"] is None, "an unregistered predicate never gets a head"
        return "unregistered claims stay unstructured observations"

    async def check_quarantine_contained(self) -> str:
        predicate = f"q-{uuid.uuid4().hex[:6]}"
        await self._register(predicate)
        result = await self._claim(predicate, "atlantis", quarantine=True)
        assert result["admission"] == "quarantined"
        assert result["resolution"] is None
        assert await self._current(predicate) == [], "quarantine must never surface a head"
        return "quarantined content takes no head"

    async def check_correction_in_place(self) -> str:
        predicate = f"fix-{uuid.uuid4().hex[:6]}"
        await self._register(predicate)
        original = await self._claim(predicate, "aile")
        evidence = await self._event("typo correction")
        corrected = await self._as(
            "POST", f"/records/{original['record_id']}/correct", json={
                "payload": {"value": "aisle"},
                "source_event_ids": [evidence["id"]],
                "expected_seq": 1,
            })
        assert corrected.status_code == 200, corrected.text
        body = corrected.json()
        assert body["revision"]["correction_of"] == original["revision"]["id"]
        segments = [s for s in await self._current(predicate) if s["kind"] == "resolved"]
        assert segments[0]["revisions"][0]["payload"]["value"] == "aisle", \
            "the correction must replace the head in place"
        return "correction links and replaces in its interval"

    async def check_absence_without_leaks(self) -> str:
        secret = (await self._as("POST", "/records", json={
            "type": "observation", "scope_id": self.scope,
            "payload": {"text": "private"},
        })).json()
        outsider = (await self._op("POST", "/admin/principals", json={
            "id": f"outsider-{uuid.uuid4().hex[:8]}", "kind": "agent"})).json()
        real = await self._as("GET", f"/records/{secret['record_id']}",
                              token=outsider["token"])
        fake = await self._as("GET", f"/records/{uuid.uuid4()}",
                              token=outsider["token"])
        assert real.status_code == fake.status_code == 404, \
            "an unreadable record must be indistinguishable from a nonexistent one"
        return "unreadable == nonexistent (404 both)"

    async def check_context_cas(self) -> str:
        context = (await self._as("POST", "/contexts", json={
            "scope_id": self.scope, "state": {"goal": "a"},
        })).json()
        ok = await self._as("PATCH", f"/contexts/{context['id']}", json={
            "expected_revision": 1, "patch": {"goal": "b"},
        })
        assert ok.status_code == 200 and ok.json()["state_revision"] == 2
        stale = await self._as("PATCH", f"/contexts/{context['id']}", json={
            "expected_revision": 1, "patch": {"goal": "c"},
        })
        assert stale.status_code == 409, "a stale state revision must be refused"
        return "working-state CAS holds"

    async def check_search_recall(self) -> str:
        marker = uuid.uuid4().hex[:8]
        event = await self._event(f"the {marker} deploy needs the vault mounted")
        await self._as("POST", "/records", json={
            "type": "observation", "scope_id": self.scope,
            "payload": {"text": f"the {marker} deploy needs the vault mounted"},
            "source_event_ids": [event["id"]],
        })
        found = await self._as("POST", "/search", json={
            "scope_ids": [self.scope],
            "query_text": f"please deploy the new {marker} build now",
        })
        assert found.status_code == 200, found.text
        texts = [item["payload"]["text"] for item in found.json()["items"]]
        assert any(marker in text for text in texts), \
            "OR-token candidates: a natural query must match on overlap"
        # And a retracted root must pull the memory out of recall.
        await self._as("POST", f"/events/{event['id']}/retract", json={"reason": "x"})
        gone = await self._as("POST", "/search", json={
            "scope_ids": [self.scope], "query_text": marker,
        })
        assert all(marker not in item["payload"]["text"]
                   for item in gone.json()["items"]), \
            "invalid-root records must not surface in recall"
        return "OR-token recall + root invalidation both hold"


CHECKS = [
    "check_auth_rejections",
    "check_idempotent_replay",
    "check_admission",
    "check_revision_cas",
    "check_temporal_truth",
    "check_authority_withheld",
    "check_unregistered_observation_only",
    "check_quarantine_contained",
    "check_correction_in_place",
    "check_absence_without_leaks",
    "check_context_cas",
    "check_search_recall",
]


async def run_conformance(
    *,
    base_url: str,
    operator_token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[CheckResult]:
    suite = Conformance(base_url=base_url, operator_token=operator_token,
                        transport=transport)
    try:
        await suite.provision()
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return [CheckResult("provisioning", False, str(exc))]
    results = []
    for name in CHECKS:
        try:
            detail = await getattr(suite, name)()
            results.append(CheckResult(name, True, detail))
        except AssertionError as exc:
            results.append(CheckResult(name, False, str(exc)))
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult(name, False, f"errored: {exc}"))
    return results
