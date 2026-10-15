"""The optional recall lane (memory pass PR 4b, §17.7): hybrid-search
candidates → zero-or-more relevance selection → canonical re-render under
budget. The load-bearing honesty rules: selector failure injects NOTHING
optional (no neighbor-stuffing), absence stays distinguishable from
outage, and the selector can only choose from actual candidates.
"""

import json
import sys
import uuid

import httpx
import pytest

import miragen.app  # noqa: F401
app_module = sys.modules["miragen.app"]
from miragen.memory import MemoryClient, MemoryLifecycle
from miragen.memory.selection import (
    Selection,
    SelectionResult,
    clamp_selections,
    render_optional_section,
    render_selector_input,
)
from miragen.models import AgentProfile
from tests.test_memory_lifecycle import MEMORY_BLOCK, FakeMemoryService


def _make_profile(**kw):
    return AgentProfile.model_validate({
        "name": "test-agent", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
        **kw,
    })


@pytest.fixture(autouse=True)
def memory_env(monkeypatch):
    monkeypatch.setenv("LOIMI_MEMORY_URL", "http://loimi.test")
    monkeypatch.setenv("LOIMI_MEMORY_TOKEN", "lmm_test-token")


@pytest.fixture
def service():
    return FakeMemoryService()


def _card(text, record_id=None, type="observation"):
    return {
        "record_id": record_id or str(uuid.uuid4()),
        "revision_id": str(uuid.uuid4()),
        "type": type, "scope_id": "profile:test-agent",
        "payload": {"text": text}, "slot": None,
        "roots_valid": True, "channels": ["lexical"],
    }


def _selector(selections_for):
    """A fake selector; `selections_for` maps → list of (record_id, reason)."""
    calls = []

    async def select(request, cards):
        calls.append({"request": request, "cards": cards})
        return SelectionResult(selections=[
            Selection(record_id=rid, reason=reason) for rid, reason in selections_for(cards)
        ])

    select.calls = calls
    return select


def _lifecycle(service, tmp_path, selector, **memory_overrides):
    profile = _make_profile(memory={**MEMORY_BLOCK, **memory_overrides})
    return MemoryLifecycle(
        profile.memory, "test-agent",
        MemoryClient(profile.memory, transport=service.transport()),
        state_dir=tmp_path / "memory", selector=selector,
    )


def _seed_record(service, card):
    """Make get_record(record_id) answer canonically for a search card."""
    service.records[card["record_id"]] = {
        "record_id": card["record_id"], "type": card["type"],
        "scope_id": card["scope_id"], "admission": "accepted", "slot_id": None,
        "revision": {"id": card["revision_id"], "seq": 1,
                     "payload": card["payload"], "lifecycle": "active"},
        "resolution": None, "roots": [], "roots_valid": True,
    }


class TestOptionalLane:
    async def test_selected_memories_ride_the_packet_with_reasons(
        self, service, tmp_path
    ):
        card = _card("the hel1 deploy needs the vault mounted first")
        service.search_results = [card, _card("unrelated lunch note")]
        _seed_record(service, card)
        selector = _selector(lambda cards: [(card["record_id"], "same deploy target")])
        lifecycle = _lifecycle(service, tmp_path, selector)

        packet = await lifecycle.prepare_context(
            instance="ops", run_id="r1", trigger="http",
            prompt_hint="deploy to hel1 again",
        )
        assert "vault mounted first" in packet.text
        assert "why: same deploy target" in packet.text
        assert "lunch note" not in packet.text
        recalled = [i for i in packet.items if i["kind"] == "recalled"]
        assert recalled == [{
            "kind": "recalled", "record_id": card["record_id"],
            "revision_id": card["revision_id"], "reason": "same deploy target",
        }]
        # The search was scoped to the profile's READ scopes.
        assert service.searches[0]["scope_ids"] == MEMORY_BLOCK["scopes"]["read"]

    async def test_selecting_nothing_is_clean(self, service, tmp_path):
        service.search_results = [_card("something vaguely related")]
        lifecycle = _lifecycle(service, tmp_path, _selector(lambda cards: []))
        packet = await lifecycle.prepare_context(
            instance="ops", run_id="r1", trigger="http", prompt_hint="new topic",
        )
        assert "recalled memories" not in packet.text
        assert packet.degraded is None

    async def test_empty_store_is_empty_not_degraded(self, service, tmp_path):
        service.search_results = []
        selector = _selector(lambda cards: [])
        lifecycle = _lifecycle(service, tmp_path, selector)
        packet = await lifecycle.prepare_context(
            instance="ops", run_id="r1", trigger="http", prompt_hint="anything",
        )
        assert packet.degraded is None
        assert selector.calls == []  # no candidates → no model call

    async def test_selector_failure_injects_nothing_and_says_so(
        self, service, tmp_path
    ):
        """§17.7: no fallback to stuffing unvetted nearest neighbors."""
        service.search_results = [_card("candidate A"), _card("candidate B")]

        async def exploding(request, cards):
            raise RuntimeError("selector model down")

        lifecycle = _lifecycle(service, tmp_path, exploding)
        packet = await lifecycle.prepare_context(
            instance="ops", run_id="r1", trigger="http", prompt_hint="deploy",
        )
        assert "candidate A" not in packet.text
        assert "optional recall degraded" in packet.text
        assert lifecycle.degraded_count == 1
        # Required lane intact:
        assert "[working state" in packet.text

    async def test_search_outage_degrades_optional_without_failing_required(
        self, service, tmp_path
    ):
        original = service._handle

        def flaky(request):
            if request.url.path.endswith("/search"):
                raise httpx.ConnectError("refused")
            return original(request)

        service._handle_override = None
        lifecycle = _lifecycle(service, tmp_path, _selector(lambda cards: []))
        lifecycle.client._transport = httpx.MockTransport(flaky)
        packet = await lifecycle.prepare_context(
            instance="ops", run_id="r1", trigger="http", prompt_hint="deploy",
        )
        assert packet.degraded is None  # required lane fine
        assert "optional recall degraded" in packet.text

    async def test_selector_cannot_smuggle_foreign_records(self, service, tmp_path):
        card = _card("legit candidate")
        foreign_id = str(uuid.uuid4())
        service.search_results = [card]
        _seed_record(service, card)
        lifecycle = _lifecycle(service, tmp_path, _selector(
            lambda cards: [(foreign_id, "trust me"), (card["record_id"], "ok")]
        ))
        packet = await lifecycle.prepare_context(
            instance="ops", run_id="r1", trigger="http", prompt_hint="q",
        )
        recalled = [i["record_id"] for i in packet.items if i["kind"] == "recalled"]
        assert recalled == [card["record_id"]]

    async def test_canonical_rerender_drops_no_longer_accepted_records(
        self, service, tmp_path
    ):
        card = _card("was fine at index time")
        service.search_results = [card]
        _seed_record(service, card)
        service.records[card["record_id"]]["admission"] = "archived"
        lifecycle = _lifecycle(service, tmp_path, _selector(
            lambda cards: [(card["record_id"], "reason")]
        ))
        packet = await lifecycle.prepare_context(
            instance="ops", run_id="r1", trigger="http", prompt_hint="q",
        )
        assert "was fine at index time" not in packet.text

    async def test_selection_cache_reuses_ids_per_state_and_query(
        self, service, tmp_path
    ):
        card = _card("cached memory")
        service.search_results = [card]
        _seed_record(service, card)
        selector = _selector(lambda cards: [(card["record_id"], "r")])
        lifecycle = _lifecycle(service, tmp_path, selector)
        for _ in range(3):
            await lifecycle.prepare_context(
                instance="ops", run_id="r", trigger="http", prompt_hint="same query",
            )
        assert len(selector.calls) == 1
        # Different query busts the cache.
        await lifecycle.prepare_context(
            instance="ops", run_id="r", trigger="http", prompt_hint="different query",
        )
        assert len(selector.calls) == 2

    async def test_disabled_and_unconfigured_lanes_do_not_search(
        self, service, tmp_path
    ):
        lifecycle = _lifecycle(service, tmp_path, _selector(lambda c: []),
                               recall={"enabled": False})
        await lifecycle.prepare_context(instance="ops", run_id="r",
                                        trigger="http", prompt_hint="q")
        assert getattr(service, "searches", []) == []

        no_selector = _lifecycle(service, tmp_path, None)
        packet = await no_selector.prepare_context(
            instance="ops", run_id="r", trigger="http", prompt_hint="q",
        )
        assert getattr(service, "searches", []) == []
        assert packet.degraded is None


class TestBudgetAndRendering:
    def test_budget_omits_whole_items(self):
        entries = [
            {"record_id": "a" * 32, "revision_id": "r", "type": "observation",
             "text": "x" * 300, "reason": "fits"},
            {"record_id": "b" * 32, "revision_id": "r", "type": "observation",
             "text": "y" * 300, "reason": "does not fit"},
        ]
        section = render_optional_section(entries, budget_chars=500)
        assert "x" * 100 in section.text
        assert "y" * 50 not in section.text  # omitted whole, not truncated mid-item

    def test_clamp_dedupes_and_caps(self):
        cards = [{"record_id": f"id-{i}"} for i in range(10)]
        result = SelectionResult(selections=[
            Selection(record_id="id-1", reason="a"),
            Selection(record_id="id-1", reason="dup"),
            *[Selection(record_id=f"id-{i}", reason="r") for i in range(2, 9)],
        ])
        kept = clamp_selections(result, cards, max_selected=3)
        assert [s.record_id for s in kept] == ["id-1", "id-2", "id-3"]

    def test_selector_input_is_bounded(self):
        cards = [_card("z" * 5000) for _ in range(50)]
        rendered = render_selector_input("q" * 5000, cards)
        assert len(rendered) <= 24_000


class TestWorkerEmbedBackfill:
    async def test_index_job_embeds_and_stores_space(self, service, tmp_path):
        from miragen.memory.extraction import run_worker_once

        revision_id = str(uuid.uuid4())
        service.projections = {revision_id: {
            "revision_id": revision_id, "record_id": str(uuid.uuid4()),
            "scope_id": "profile:test-agent", "record_type": "observation",
            "search_text": "the deploy failed", "embedding_space": None,
            "current": True,
        }}
        service.jobs.append({"id": str(uuid.uuid4()), "kind": "index",
                             "payload": {"revision_id": revision_id},
                             "status": "pending", "scope_id": "profile:test-agent",
                             "attempts": 0})
        profile = _make_profile(memory=MEMORY_BLOCK)
        client = MemoryClient(profile.memory, transport=service.transport())

        async def fake_embed(texts):
            return [[0.5] * 1024], "BAAI/bge-m3@rev1#1024"

        async def never(*a):
            raise AssertionError("consolidate path must not run")

        results = await run_worker_once(client, extract=never, check=never,
                                        embed=fake_embed)
        assert results[0]["status"] == "done"
        assert results[0]["embedded"] is True
        stored = service.embeddings[revision_id]
        assert stored["space"] == "BAAI/bge-m3@rev1#1024"
        assert len(stored["embedding"]) == 1024

    async def test_retired_projection_completes_as_noop(self, service, tmp_path):
        from miragen.memory.extraction import run_worker_once

        revision_id = str(uuid.uuid4())
        service.projections = {revision_id: {
            "revision_id": revision_id, "record_id": str(uuid.uuid4()),
            "scope_id": "profile:test-agent", "record_type": "observation",
            "search_text": "old text", "embedding_space": None, "current": False,
        }}
        service.jobs.append({"id": str(uuid.uuid4()), "kind": "index",
                             "payload": {"revision_id": revision_id},
                             "status": "pending", "scope_id": "profile:test-agent",
                             "attempts": 0})
        profile = _make_profile(memory=MEMORY_BLOCK)
        client = MemoryClient(profile.memory, transport=service.transport())

        async def fake_embed(texts):
            raise AssertionError("must not embed a retired projection")

        async def never(*a):
            raise AssertionError("no consolidate")

        results = await run_worker_once(client, extract=never, check=never,
                                        embed=fake_embed)
        assert results[0]["status"] == "done"
        assert results[0]["embedded"] is False
        assert getattr(service, "embeddings", {}) == {}

    async def test_without_embed_index_jobs_stay_unclaimed(self, service):
        from miragen.memory.extraction import run_worker_once

        service.jobs.append({"id": str(uuid.uuid4()), "kind": "index",
                             "payload": {"revision_id": "x"}, "status": "pending",
                             "scope_id": "profile:test-agent", "attempts": 0})
        profile = _make_profile(memory=MEMORY_BLOCK)
        client = MemoryClient(profile.memory, transport=service.transport())

        async def none(*a):
            return None

        results = await run_worker_once(client, extract=none, check=none)
        assert results == []
        assert service.jobs[0]["status"] == "pending"
