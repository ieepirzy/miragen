"""The inbox (miragen/watch.py): what's new in watched sources, known without a
model turn — and the guarantees a consumer relies on to never read "nothing
new" when the tool itself would show something."""

from __future__ import annotations

import contextlib

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import ToolAnnotations

from miragen.harness.gateway import ToolGateway
from miragen.models import AgentProfile, WatchSource
from miragen.watch import Inbox, WatchError, payload_of


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FakeTool:
    """Returns whatever `result` currently is; raises if it's an exception."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def mails(*items):
    return {"results": [{"locator": loc, "headers": {"from": f, "subject": s}}
                        for loc, f, s in items], "truncated": True}


def make(tmp_path, result, **spec):
    body = {"name": "email", "tool": "home_search_emails", "items": "results",
            "id": "locator", "show": ["headers.from", "headers.subject"]}
    body.update(spec)
    tool = FakeTool(result)
    clock = Clock()
    inbox = Inbox([WatchSource(**body)], tool, tmp_path / "inbox.json", clock=clock)
    return inbox, tool, clock


async def test_first_poll_is_a_baseline_then_new_items_become_entries(tmp_path):
    inbox, tool, _ = make(tmp_path, mails(("a", "x@y", "old")))
    assert inbox.view()["sources"]["email"]["status"] == "never"      # not polled: not "ok"
    assert await inbox.poll("email") == 0
    view = inbox.view()
    assert view["sources"]["email"]["status"] == "ok" and view["entries"] == []
    tool.result = mails(("b", "boss@co", "urgent"), ("a", "x@y", "old"))
    assert await inbox.poll("email") == 1
    [entry] = inbox.view()["entries"]
    assert entry["kind"] == "new" and entry["key"] == "id:b"
    assert entry["summary"] == {"headers.from": "boss@co", "headers.subject": "urgent"}
    assert await inbox.poll("email") == 0                               # same view: nothing more
    assert tool.calls[0] == ("home_search_emails", {})


async def test_an_edit_is_changed_with_an_id_and_new_without(tmp_path):
    inbox, tool, _ = make(tmp_path, mails(("a", "x@y", "v1")))
    await inbox.poll("email")
    tool.result = mails(("a", "x@y", "v2"))
    assert await inbox.poll("email") == 1
    assert inbox.view()["entries"][-1]["kind"] == "changed"

    cal, ctool, _ = make(tmp_path / "cal", {"events": [{"summary": "Gym", "start": "18:00"}]},
                         name="calendar", tool="home_get_calendar_events", items="events",
                         id=None, show=[])
    await cal.poll("calendar")
    ctool.result = {"events": [{"summary": "Gym", "start": "19:00"}]}   # moved, no uid
    assert await cal.poll("calendar") == 1
    assert cal.view()["entries"][-1]["kind"] == "new"


async def test_leaving_the_window_is_not_a_change_and_coming_back_is_not_new(tmp_path):
    inbox, tool, clock = make(tmp_path, mails(("a", "x", "1"), ("b", "y", "2")))
    await inbox.poll("email")
    tool.result = mails(("b", "y", "2"))              # a scrolled out
    assert await inbox.poll("email") == 0
    clock.t += 3600
    tool.result = mails(("a", "x", "1"), ("b", "y", "2"))   # a is back, unchanged
    assert await inbox.poll("email") == 0


async def test_a_window_of_only_new_items_sets_a_sticky_overflow(tmp_path):
    inbox, tool, _ = make(tmp_path, mails(("a", "x", "1")))
    await inbox.poll("email")
    tool.result = mails(("c", "x", "3"), ("b", "x", "2"))   # nothing we knew: maybe more beyond
    await inbox.poll("email")
    assert inbox.view()["sources"]["email"]["overflow"] is True
    tool.result = mails(("c", "x", "3"), ("b", "x", "2"))
    await inbox.poll("email")                                 # a quiet poll can't hide it
    assert inbox.view()["sources"]["email"]["overflow"] is True
    inbox.ack(inbox.view()["seq"])
    assert inbox.view()["sources"]["email"]["overflow"] is False


@pytest.mark.parametrize("bad", [
    RuntimeError("upstream down"),
    {"ok": False, "error": "IMAP login failed"},
    {"results": "not a list"},
])
async def test_a_failed_poll_is_status_error_never_nothing_new(tmp_path, bad):
    inbox, tool, _ = make(tmp_path, mails(("a", "x", "1")))
    await inbox.poll("email")
    tool.result = bad
    assert await inbox.poll("email") == 0
    src = inbox.view()["sources"]["email"]
    assert src["status"] == "error" and src["last_error"]
    tool.result = mails(("a", "x", "1"), ("b", "y", "2"))    # recovers, and still sees b
    assert await inbox.poll("email") == 1
    assert inbox.view()["sources"]["email"]["status"] == "ok"


async def test_entries_stay_until_acknowledged_and_survive_a_restart(tmp_path):
    inbox, tool, clock = make(tmp_path, mails(("a", "x", "1")))
    await inbox.poll("email")
    tool.result = mails(("c", "x", "3"), ("b", "x", "2"), ("a", "x", "1"))
    await inbox.poll("email")
    view = inbox.view()
    assert [e["key"] for e in view["entries"]] == ["id:c", "id:b"]
    await inbox.poll("email")                               # observed again: still unacknowledged
    assert len(inbox.view()["entries"]) == 2
    first = view["entries"][0]["seq"]
    assert inbox.ack(first) == 1
    again = Inbox(list(inbox.sources.values()), tool, tmp_path / "inbox.json", clock=clock)
    assert [e["key"] for e in again.view()["entries"]] == ["id:b"]
    assert await again.poll("email") == 0                   # its seen-set survived too


def test_payload_of_reads_structured_text_and_errors():
    class R:
        def __init__(self, **kw):
            self.isError = kw.get("isError", False)
            self.structuredContent = kw.get("structured")
            self.content = kw.get("content", [])

    class T:
        def __init__(self, text):
            self.text = text

    assert payload_of(R(structured={"result": [1]})) == [1]
    assert payload_of(R(content=[T('{"a": 1}')])) == {"a": 1}
    with pytest.raises(WatchError):
        payload_of(R(isError=True, content=[T("boom")]))
    with pytest.raises(WatchError):
        payload_of(R(content=[T("not json")]))


# ── the gateway's read path: same upstream, but read-only and ungated only ──
def upstream() -> FastMCP:
    up = FastMCP("up")

    @up.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search_emails(limit: int = 20) -> dict:
        """Read-only."""
        return {"results": [{"locator": "1"}], "limit": limit}

    @up.tool()
    def get_home_state() -> dict:
        """Not annotated read-only."""
        return {}

    @up.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def create_booking() -> str:
        """Read-only by annotation, but approval-gated by the profile."""
        return "booked"
    return up


def client_factory(server: FastMCP):
    @contextlib.asynccontextmanager
    async def factory(url, headers=None):
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as tg:
                low = server._mcp_server
                tg.start_soon(lambda: low.run(server_streams[0], server_streams[1],
                                              low.create_initialization_options()))
                yield client_streams[0], client_streams[1], lambda: None
                tg.cancel_scope.cancel()
    return factory


def gateway() -> ToolGateway:
    profile = AgentProfile.model_validate({
        "name": "g", "mode": "interactive", "triggers": [{"type": "http"}],
        "approval_required": ["home_create_booking"],
        "spec": {"model": "grok-build:grok-4.6", "instructions": "hi",
                 "capabilities": [{"MCP": {"url": "http://up/mcp", "name": "home"}}]},
        "watch": [{"name": "email", "tool": "home_search_emails", "items": "results",
                   "id": "locator"}],
    })

    def speak(text: str) -> str:
        """Local runtime tool."""
        return text
    return ToolGateway(profile, runtime_tools=[speak], env={},
                       upstream_client=client_factory(upstream()))


async def test_read_tool_calls_the_same_upstream_outside_a_turn():
    gw = gateway()
    result = await gw.read_tool("home_search_emails", {"limit": 5})
    assert payload_of(result) == {"results": [{"locator": "1"}], "limit": 5}


@pytest.mark.parametrize("name,why", [
    ("home_get_home_state", "read-only"),
    ("home_create_booking", "approval"),
    ("speak", "upstream"),
    ("home_nope", "unknown"),
])
async def test_read_tool_refuses_anything_not_plainly_read_only(name, why):
    with pytest.raises(PermissionError, match=why):
        await gateway().read_tool(name, {})


async def test_inbox_over_the_real_gateway_path(tmp_path):
    gw = gateway()
    inbox = Inbox(gw.profile.watch, gw.read_tool, tmp_path / "inbox.json")
    await inbox.poll("email")
    assert inbox.view()["sources"]["email"]["status"] == "ok"


async def test_a_hung_upstream_times_out_into_status_error(tmp_path, monkeypatch):
    import asyncio

    import miragen.watch as watch
    monkeypatch.setattr(watch, "POLL_TIMEOUT_S", 0.05)

    async def hangs(name, args):
        await asyncio.sleep(10)
    inbox = Inbox([WatchSource(name="email", tool="home_search_emails", items="results",
                               id="locator")], hangs, tmp_path / "inbox.json")
    await inbox.poll("email")
    src = inbox.view()["sources"]["email"]
    assert src["status"] == "error" and "Timeout" in src["last_error"]
    assert src["every_s"] == 300.0


async def test_a_failed_first_poll_does_not_make_the_next_success_all_new(tmp_path):
    inbox, tool, _ = make(tmp_path, RuntimeError("OAuth not ready"))
    await inbox.poll("email")
    assert inbox.view()["sources"]["email"]["status"] == "error"
    tool.result = mails(("a", "x", "1"), ("b", "y", "2"))
    assert await inbox.poll("email") == 0        # this is the baseline
    tool.result = mails(("c", "z", "3"), ("a", "x", "1"), ("b", "y", "2"))
    assert await inbox.poll("email") == 1


async def test_compare_ignores_fields_background_jobs_touch(tmp_path):
    def rows(status, updated):
        return [{"id": 7, "status": status, "move_date": "2026-09-28", "updated_at": updated}]
    inbox, tool, _ = make(tmp_path, rows("reserved", "t1"), name="email", items=None, id="id",
                          show=["status"], compare=["status", "move_date"])
    await inbox.poll("email")
    tool.result = rows("reserved", "t2")               # only updated_at moved
    assert await inbox.poll("email") == 0
    tool.result = rows("confirmed", "t3")              # a real change
    assert await inbox.poll("email") == 1
    assert inbox.view()["entries"][-1]["kind"] == "changed"
    tool.result = rows("confirmed", "t3") + [{"id": 8, "status": "reserved", "move_date": "x",
                                              "updated_at": "t4"}]
    assert await inbox.poll("email") == 1               # a new booking is new by id


async def test_a_compare_path_missing_everywhere_is_an_error(tmp_path):
    inbox, _tool, _ = make(tmp_path, [{"id": 1, "status": "x"}], items=None, id="id",
                           compare=["statsu"])          # typo
    await inbox.poll("email")
    src = inbox.view()["sources"]["email"]
    assert src["status"] == "error" and "statsu" in src["last_error"]


async def test_changing_a_sources_definition_rebaselines_instead_of_flooding(tmp_path):
    rows = [{"id": i, "status": "reserved", "updated_at": "t1"} for i in range(5)]
    inbox, tool, clock = make(tmp_path, rows, items=None, id="id", show=[])
    await inbox.poll("email")
    spec = inbox.sources["email"].model_copy(update={"compare": ["status"]})
    again = Inbox([spec], tool, tmp_path / "inbox.json", clock=clock)
    assert await again.poll("email") == 0         # same data, new hashing: no entries
    tool.result = [{**r, "status": "confirmed"} if r["id"] == 3 else r for r in rows]
    assert await again.poll("email") == 1


async def test_a_rebaseline_still_reports_items_that_are_really_new(tmp_path):
    rows = [{"id": 1, "status": "reserved", "updated_at": "t1"}]
    inbox, tool, clock = make(tmp_path, rows, items=None, id="id", show=[])
    await inbox.poll("email")
    spec = inbox.sources["email"].model_copy(update={"compare": ["status"]})
    again = Inbox([spec], tool, tmp_path / "inbox.json", clock=clock)
    tool.result = rows + [{"id": 2, "status": "reserved", "updated_at": "t9"}]  # arrived meanwhile
    assert await again.poll("email") == 1
    assert again.view()["entries"][-1]["key"] == "id:2"


async def test_state_from_before_definitions_is_not_rebaselined(tmp_path):
    """Upgrading miragen must not swallow an edit on the first poll after it."""
    inbox, tool, clock = make(tmp_path, mails(("a", "x", "v1")))
    await inbox.poll("email")
    inbox.state["sources"]["email"].pop("definition")        # state written by #164/#165
    inbox._save()
    again = Inbox(list(inbox.sources.values()), tool, tmp_path / "inbox.json", clock=clock)
    tool.result = mails(("a", "x", "v2"))
    assert await again.poll("email") == 1
