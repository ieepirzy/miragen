"""Source identities and rendering accounting are deterministic, never model scores."""

import copy
import subprocess

import pytest

from miragen.memory.resources import inspect_resources, resource_key
from miragen.memory.selection import render_optional_section
from tests.test_memory_recall_lane import (
    _card,
    _lifecycle,
    _seed_record,
    _selector,
    service as service,
    memory_env as memory_env,
)


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "user.name", "fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "remote", "add", "origin", "https://github.com/example/repo.git")
    (root / "pricing.py").write_text("def price():\n    return 10\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return root


def inspect(root, symbol="price"):
    return inspect_resources(root, [{"path": "pricing.py", "symbol": symbol}])[0]


def test_line_shift_identity_body_drift_and_rebuild(repository):
    original = inspect(repository)
    source = repository / "pricing.py"
    source.write_text("# irrelevant line shift\n\n" + source.read_text())
    shifted = inspect(repository)
    assert resource_key(original["resource"]) == resource_key(shifted["resource"])
    assert original["content_digest"] == shifted["content_digest"]
    source.write_text(source.read_text().replace("return 10", "return 20"))
    changed = inspect(repository)
    assert changed["resource"] == original["resource"]
    assert changed["content_digest"] != original["content_digest"]
    assert inspect(repository)["content_digest"] == changed["content_digest"]
    assert original["snapshot"]["dirty_digest"] != changed["snapshot"]["dirty_digest"]


def test_rename_edit_missing_and_duplicate_symbols(repository):
    (repository / "pricing.py").write_text("def new_price():\n    return 20\n")
    assert inspect(repository)["outcome"] == "missing"
    assert inspect(repository, "new_price")["outcome"] == "present"
    (repository / "pricing.py").write_text(
        "def price(): return 10\ndef price(): return 20\n"
    )
    assert inspect(repository)["outcome"] == "ambiguous"
    (repository / "pricing.py").unlink()
    assert inspect(repository)["outcome"] == "missing"


def test_branch_worktree_and_repository_identity(repository, tmp_path):
    original = inspect(repository)
    git(repository, "checkout", "-b", "feature")
    branch = inspect(repository)
    assert branch["snapshot"]["branch"] != original["snapshot"]["branch"]
    other = tmp_path / "other"
    git(repository, "worktree", "add", "-b", "other", str(other))
    assert (
        inspect(other)["snapshot"]["checkout_id"] != original["snapshot"]["checkout_id"]
    )
    mismatched = inspect_resources(
        repository, [original["resource"] | {"repository": "different/repo"}]
    )[0]
    assert mismatched["outcome"] == "unverified" and mismatched["snapshot"] is None


def test_symbol_qualifications_decorators_and_unsafe_paths(repository, tmp_path):
    (repository / "pricing.py").write_text(
        "class Price:\n    @staticmethod\n    def total():\n        return 10\n"
    )
    original = inspect(repository, "Price.total")
    assert original["outcome"] == "present"
    (repository / "pricing.py").write_text(
        "class Price:\n    @classmethod\n    def total():\n        return 10\n"
    )
    assert (
        inspect(repository, "Price.total")["content_digest"]
        != original["content_digest"]
    )
    assert (
        inspect_resources(repository, [{"path": "../secret.py"}])[0]["outcome"]
        == "unverified"
    )
    (tmp_path / "secret.py").write_text("SECRET = 1")
    (repository / "secret.py").symlink_to(tmp_path / "secret.py")
    assert (
        inspect_resources(repository, [{"path": "secret.py"}])[0]["outcome"]
        == "unverified"
    )


def test_snapshot_change_during_read_is_unknown(repository, monkeypatch):
    import miragen.memory.resources as adapter

    original = adapter.snapshot
    calls = 0

    def raced(root):
        nonlocal calls
        calls += 1
        snap = original(root)
        if calls > 1:
            snap["source_revision"] = "changed"
        return snap

    monkeypatch.setattr(adapter, "snapshot", raced)
    assert inspect(repository)["outcome"] == "unverified"


async def test_partial_boundary_and_prompt_manifests_are_exact(service, tmp_path):
    cards = [_card("x" * 250), _card("y" * 250)]
    service.search_results = cards
    for card in cards:
        _seed_record(service, card)
    lifecycle = _lifecycle(
        service,
        tmp_path,
        _selector(lambda _: [(c["record_id"], "r") for c in cards]),
        recall={"max_optional_chars": 500},
    )
    packet = await lifecycle.prepare_context(
        instance="ops", run_id="r", trigger="http", prompt_hint="q"
    )
    assert len(packet.items) == 2  # required state plus one emitted memory
    assert packet.rendering["emitted"] == [
        {"record_id": cards[0]["record_id"], "revision_id": cards[0]["revision_id"]}
    ]
    assert packet.rendering["omitted"][0]["revision_id"] == cards[1]["revision_id"]
    assert packet.rendering["omitted"][0]["reason"] == "budget_exceeded"
    await lifecycle.recall_section(instance="ops", prompt_hint="q", run_id="r")
    for manifest in service.manifests:
        assert [i["revision_id"] for i in manifest["items"]] == [
            cards[0]["revision_id"]
        ]
        assert manifest["policy"]["stage"] == "rendered"
        assert manifest["policy"]["delivery_status"] == "unconfirmed"
        assert manifest["policy"]["rendering"]["truncated"]


async def test_qualifications_after_500_characters_survive_or_whole_record_is_omitted(
    service, tmp_path
):
    card = _card("explanation " * 55 + " NEVER use this in production.")
    card["payload"]["conditions"] = ["fixture only"]
    service.search_results = [card]
    _seed_record(service, card)
    lifecycle = _lifecycle(
        service, tmp_path, _selector(lambda _: [(card["record_id"], "r")])
    )
    packet = await lifecycle.prepare_context(
        instance="ops", run_id="r", trigger="http", prompt_hint="q"
    )
    assert "NEVER use this in production" in packet.text
    assert "fixture only" in packet.text
    lifecycle.spec.recall.max_optional_chars = 500
    packet = await lifecycle.prepare_context(
        instance="ops", run_id="r", trigger="http", prompt_hint="q"
    )
    assert "explanation" not in packet.text
    assert not packet.rendering["emitted"]


def test_budget_counts_header_newlines_duplicates_and_skips_oversized():
    entries = [
        {
            "record_id": str(i),
            "revision_id": str(i),
            "type": "observation",
            "text": text,
            "reason": "r",
        }
        for i, text in enumerate(["x" * 2000, "small", "small"])
    ]
    entries.append(copy.deepcopy(entries[1]))
    result = render_optional_section(entries, 200)
    assert result.used_chars == len(result.text) <= 200
    assert [i["record_id"] for i in result.emitted] == ["1", "2"]
    assert [i["reason"] for i in result.omitted] == [
        "budget_exceeded",
        "duplicate_record",
    ]
    exact = render_optional_section(entries[1:3], result.used_chars)
    assert len(exact.emitted) == 2
    below = render_optional_section(entries[1:3], result.used_chars - 1)
    assert len(below.emitted) == 1


async def test_timeout_after_manifest_write_never_means_delivered(service, tmp_path):
    import asyncio

    lifecycle = _lifecycle(service, tmp_path, None)
    original = lifecycle.client.create_manifest

    async def stalled(body):
        await original(body)
        await asyncio.sleep(10)

    lifecycle.client.create_manifest = stalled
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            lifecycle.prepare_context(instance="ops", run_id="r", trigger="hook"), 0.02
        )
    assert service.manifests
    assert service.manifests[0]["policy"]["delivery_status"] == "unconfirmed"


async def test_exact_and_ordinary_recall_share_budget_and_deduplicate(
    service, tmp_path
):
    card = _card("same complete assertion")
    service.search_results = [card]
    _seed_record(service, card)
    lifecycle = _lifecycle(
        service, tmp_path, _selector(lambda _: [(card["record_id"], "text match")])
    )

    async def lookup(body):
        return {
            "items": [
                card
                | {"matches": [{"resource": {"path": "pricing.py", "symbol": "price"}}]}
            ]
        }

    lifecycle.client.lookup_resources = lookup
    packet = await lifecycle.prepare_context(
        instance="ops",
        run_id="r",
        trigger="http",
        prompt_hint="q",
        resources=[{"explicit": "source evidence"}],
    )
    assert len([i for i in packet.items if i["kind"] == "recalled"]) == 1
    assert "exact resources: pricing.py::price" in packet.text
    assert packet.rendering["omitted"][-1]["reason"] == "duplicate_record"


async def test_grounded_write_refuses_ephemeral_before_any_write():
    from types import SimpleNamespace
    from miragen.memory.grounded import remember

    lifecycle = SimpleNamespace(spec=SimpleNamespace(backend="ephemeral"))
    result = await remember(
        lifecycle,
        "/unavailable",
        {"path": "file.py"},
        payload={"text": "claim"},
        support={"result": "inconclusive", "method": "none"},
    )
    assert result["status"] == "unsupported_backend"


class _RecordingClient:
    def __init__(self):
        self.events = []

    async def append_event(self, **kw):
        self.events.append(kw)
        return {"id": "ev-1"}

    async def propose_record(self, body):
        return {"record_id": "rec-1", "revision": {"id": "rev-1"}}

    async def add_evidence(self, revision_id, body):
        return {"id": "evid-1"}

    async def create_grounding(self, record_id, body):
        return {"id": "g-1"}


def _loimi_lifecycle(client):
    from types import SimpleNamespace

    return SimpleNamespace(client=client, spec=SimpleNamespace(
        backend="loimi", scopes=SimpleNamespace(default_write="group:project.x")))


def _commit_bytes(root, name, data: bytes):
    (root / name).write_bytes(data)
    git(root, "add", name)
    git(root, "commit", "-m", name)


@pytest.mark.parametrize("symbol", ["price", None])
async def test_grounded_write_keeps_declared_encoding_source_exact(repository, symbol):
    """A PEP 263 Latin-1 file: the stored event is the inspected source,
    not a lossy UTF-8 approximation, and re-encodes to the digested bytes."""
    from miragen.memory.grounded import remember
    from miragen.memory.resources import digest

    data = '# -*- coding: latin-1 -*-\ndef price():\n    return "café"\n'.encode("latin-1")
    _commit_bytes(repository, "latin.py", data)
    client = _RecordingClient()
    locator = {"path": "latin.py", **({"symbol": symbol} if symbol else {})}
    result = await remember(_loimi_lifecycle(client), str(repository), locator,
                            payload={"text": "returns café"},
                            support={"result": "supports", "method": "read"})
    assert result["status"] == "accepted"
    event = client.events[0]
    assert "café" in event["content"] and "�" not in event["content"]
    encoding = event["attributes"]["source_encoding"]
    observed = event["attributes"]["resource_observation"]["content_digest"]
    assert digest(event["content"].encode(encoding)) == observed


async def test_grounded_write_refuses_bytes_invalid_in_the_declared_encoding(repository):
    from miragen.memory.grounded import remember

    # No declaration means UTF-8; \xff is not UTF-8.
    _commit_bytes(repository, "notes.txt.py", b"# data\nX = 1  # \xff\n")
    client = _RecordingClient()
    result = await remember(_loimi_lifecycle(client), str(repository), {"path": "notes.txt.py"},
                            payload={"text": "x"}, support={"result": "supports", "method": "read"})
    assert result["status"] == "unverified"
    assert client.events == []  # nothing written before the refusal


@pytest.mark.parametrize("inspect_mode", [True, False])
async def test_candidate_truncation_is_never_reported_as_exhaustive(
    service, tmp_path, monkeypatch, inspect_mode
):
    """The backend stopped scanning candidates (candidate_truncated) while
    every returned item fits: historical inspection and current recall must
    both say the result is truncated, with the limit in the omissions."""
    import miragen.memory.grounded as grounded

    card = _card("grounded assertion") | {
        "applicability": {"state": "historical"},
        "matches": [{"resource": {"path": "pricing.py", "symbol": "price"}}]}
    lifecycle = _lifecycle(service, tmp_path, None)

    async def lookup(body):
        return {"items": [card], "truncated": False,
                "candidate_truncated": True, "candidate_limit": 50}

    lifecycle.client.lookup_resources = lookup
    monkeypatch.setattr(grounded, "inspect_resources",
                        lambda checkout, locators: [{"explicit": "observation"}])
    out = await grounded.recall(lifecycle, str(tmp_path), [{"path": "pricing.py"}],
                                inspect=inspect_mode)
    rendering = out["rendering"]
    assert card["record_id"][:8] in out["text"]  # the item itself fit
    assert rendering["truncated"] is True
    assert {"reason": "candidate_limit", "limit": 50} in rendering["omitted"]
    assert rendering["candidate_truncated"] is True
