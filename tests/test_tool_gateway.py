"""The tool gateway: one MCP endpoint serving an agent's tools to a foreign
harness, with approvals and tool-call records held by the host."""

from __future__ import annotations

import contextlib
import json

import anyio
import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from miragen.approval import ApprovalDenied  # noqa: F401  (import path is public)
from miragen.factory import register_approval_handler
from miragen.harness.gateway import GatewayConfigError, GatewayRunContext, ToolGateway
from miragen.models import AgentProfile, ApprovalResponse


def upstream_server() -> FastMCP:
    up = FastMCP("upstream")

    @up.tool()
    def lookup(key: str) -> str:
        """Look a key up."""
        return f"value-of-{key}"

    @up.tool()
    def delete_everything() -> str:
        """Dangerous."""
        return "deleted"
    return up


def memory_client_factory(server: FastMCP, seen_headers: list):
    @contextlib.asynccontextmanager
    async def factory(url, headers=None):
        seen_headers.append((url, headers))
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as tg:
                low = server._mcp_server
                tg.start_soon(lambda: low.run(server_streams[0], server_streams[1],
                                              low.create_initialization_options()))
                yield client_streams[0], client_streams[1], lambda: None
                tg.cancel_scope.cancel()
    return factory


def profile(**over) -> AgentProfile:
    body = {
        "name": "g", "mode": "interactive", "triggers": [{"type": "http"}],
        "spec": {"model": "grok-build:grok-4.6", "instructions": "hi",
                 "capabilities": [{"MCP": {"url": "http://up/mcp", "name": "home",
                                           "bearer_token_env": "HOME_TOKEN",
                                           "allowed_tools": ["lookup"]}}]},
    }
    body.update(over)
    return AgentProfile.model_validate(body)


def speak(text: str) -> str:
    """Say something."""
    return f"spoke:{text}"


def tools_py_tool(ctx, city: str) -> str:
    """A @register tool that reads the run context."""
    return f"{city}|{ctx.run_id}|{ctx.instance}|{isinstance(ctx, GatewayRunContext)}"


@pytest.fixture
def seen():
    return []


def make(seen, **kw) -> ToolGateway:
    bound = kw.pop("bound", [])

    @contextlib.contextmanager
    def bind(run_id, instance):
        bound.append((run_id, instance))
        yield
    return ToolGateway(
        kw.pop("profile", profile()), runtime_tools=[speak],
        registered_tools={"weather": tools_py_tool}, bind_context=bind,
        env={"HOME_TOKEN": "upstream-secret"},
        upstream_client=memory_client_factory(upstream_server(), seen), **kw)


def text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def test_lists_runtime_and_upstream_tools_namespaced_and_filtered(seen):
    gw = make(seen)
    names = {t.name for t in await gw._list_tools()}
    assert names == {"speak", "home_lookup"}  # delete_everything not in allowed_tools
    # miragen authenticates upstream with its own credential
    assert seen[0] == ("http://up/mcp", {"Authorization": "Bearer upstream-secret",
                                         "X-Miragen-Agent": "g"})


async def test_registered_tool_is_served_with_a_run_context_shim(seen):
    gw = make(seen, profile=profile(tools=["weather"]))
    tool = next(t for t in await gw._list_tools() if t.name == "weather")
    assert list(tool.inputSchema["properties"]) == ["city"]   # ctx hidden from the schema
    with gw.turn("inst", "run-1") as log:
        res = await gw.call_tool("weather", {"city": "Oulu"}, instance="inst")
    assert text(res) == "Oulu|run-1|inst|True" and log.calls[0].ok


async def test_fails_closed_without_credential_or_turn(seen):
    gw = make(seen)
    res = await gw.call_tool("speak", {"text": "x"}, instance=None)
    assert res.isError and "credential" in text(res)
    res = await gw.call_tool("speak", {"text": "x"}, instance="inst")
    assert res.isError and "no turn in progress" in text(res)


async def test_runtime_tool_runs_with_app_context_bound_and_is_recorded(seen):
    bound = []
    gw = make(seen, bound=bound)
    with gw.turn("inst", "run-7") as log:
        res = await gw.call_tool("speak", {"text": "moi"}, instance="inst")
    assert text(res) == "spoke:moi" and bound == [("run-7", "inst")]
    assert [(c.tool_name, json.loads(c.args), c.ok) for c in log.calls] == [
        ("speak", {"text": "moi"}, True)]


async def test_upstream_calls_carry_run_and_instance_identity(seen):
    gw = make(seen)
    with gw.turn("tg-111", "run-42"):
        await gw.call_tool("home_lookup", {"key": "k"}, instance="tg-111")
    url, headers = seen[-1]
    assert headers["X-Miragen-Run-Id"] == "run-42" and headers["X-Miragen-Instance"] == "tg-111"
    assert headers["Authorization"] == "Bearer upstream-secret"  # miragen's own credential


def test_native_capabilities_are_left_to_the_harness(seen):
    p = profile(spec={"model": "grok-build:x", "instructions": "i",
                      "capabilities": ["WebSearch", "WebFetch"]})
    make(seen, profile=p, native_capabilities=frozenset({"WebSearch", "WebFetch"}))
    with pytest.raises(GatewayConfigError, match="Thinking"):
        make(seen, profile=profile(spec={"model": "grok-build:x", "instructions": "i",
                                         "capabilities": [{"Thinking": {"effort": "low"}}]}),
             native_capabilities=frozenset({"WebSearch", "WebFetch"}))


async def test_upstream_call_is_proxied_and_disallowed_tools_refused(seen):
    gw = make(seen)
    with gw.turn("inst", "r") as log:
        ok = await gw.call_tool("home_lookup", {"key": "k"}, instance="inst")
        bad = await gw.call_tool("home_delete_everything", {}, instance="inst")
    assert text(ok) == "value-of-k" and not ok.isError
    assert bad.isError and "unknown tool" in text(bad)
    assert [c.ok for c in log.calls] == [True, False]


async def test_one_turn_per_instance(seen):
    gw = make(seen)
    with gw.turn("inst", "a"):
        with pytest.raises(RuntimeError):
            with gw.turn("inst", "b"):
                pass


@pytest.mark.parametrize("pattern", ["home_lookup", "lookup", "home_*"])
async def test_approval_strict_denies_by_namespaced_or_raw_name(seen, pattern):
    gw = make(seen, profile=profile(approval_required=[pattern], approval_mode="strict"))
    with gw.turn("inst", "r") as log:
        res = await gw.call_tool("home_lookup", {"key": "k"}, instance="inst")
    assert res.isError and "approval_mode: strict" in text(res)
    assert log.calls[0].ok is False
    assert seen == []  # never reached the upstream


async def test_approval_handler_approves_with_note(seen):
    requests = []

    async def handler(req):
        requests.append(req)
        return ApprovalResponse(approved=True, prompt="ok, but be quick")
    register_approval_handler(handler)
    try:
        gw = make(seen, profile=profile(approval_required=["speak"]))
        with gw.turn("inst", "r"):
            res = await gw.call_tool("speak", {"text": "hei"}, instance="inst")
    finally:
        register_approval_handler(None)
    assert requests[0].tool_name == "speak" and requests[0].tool_args == {"text": "hei"}
    assert "Approver note: ok, but be quick" in text(res) and "spoke:hei" in text(res)


def test_rejects_capabilities_a_foreign_harness_cannot_carry(seen):
    with pytest.raises(GatewayConfigError, match="WebSearch"):
        make(seen, profile=profile(spec={"model": "grok-build:x", "instructions": "i",
                                         "capabilities": ["WebSearch"]}))


def test_rejects_unknown_registered_tool(seen):
    with pytest.raises(GatewayConfigError, match="nope"):
        make(seen, profile=profile(tools=["nope"]))


async def test_http_endpoint_requires_a_known_instance_credential(seen):
    gw = make(seen)
    token = gw.credential("inst")
    assert gw.credential("inst") == token and gw.instance_for(token) == "inst"
    async with gw.running():
        transport = httpx.ASGITransport(app=gw.asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            hdr = {"accept": "application/json, text/event-stream"}
            assert (await c.post("/", json=body, headers=hdr)).status_code == 401
            assert (await c.post("/", json=body, headers={**hdr, "authorization": "Bearer x"})).status_code == 401
            ok = await c.post("/", json=body, headers={**hdr, "authorization": f"Bearer {token}"})
            assert ok.status_code == 200 and "home_lookup" in ok.text
            call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "speak", "arguments": {"text": "x"}}}
            # authenticated, but no turn in progress: refused
            refused = await c.post("/", json=call, headers={**hdr, "authorization": f"Bearer {token}"})
            assert "no turn in progress" in refused.text
            with gw.turn("inst", "r") as log:
                done = await c.post("/", json=call, headers={**hdr, "authorization": f"Bearer {token}"})
            assert "spoke:x" in done.text and log.calls[0].tool_name == "speak"


# ── argument-aware approval rules ────────────────────────────────────────────

from miragen.approval import approval_gated  # noqa: E402

CRM_RULE = "crm_execute_tool:toolName!=find_*|get_*|search_*|list_*|count_*"


@pytest.mark.parametrize("args,gated", [
    ({"toolName": "find_many_people", "arguments": {}}, False),
    ({"toolName": "get_tool_catalog"}, False),
    ({"toolName": "create_one_opportunity"}, True),
    ({"toolName": "delete_many_people"}, True),
    ({"toolName": "some_new_twenty_tool"}, True),    # unknown = gated (fail closed)
    ({}, True),                                      # missing argument = gated
    ({"toolName": 42}, True),
])
def test_negated_rule_gates_everything_but_reads(args, gated):
    p = profile(approval_required=[CRM_RULE])
    assert approval_gated(p, "crm_execute_tool", "execute_tool", args=args) is gated


def test_positive_rule_and_plain_globs():
    p = profile(approval_required=["files_write:path=/etc/*|/root/*", "booking_create_booking"])
    assert approval_gated(p, "files_write", args={"path": "/etc/passwd"})
    assert not approval_gated(p, "files_write", args={"path": "/tmp/x"})
    assert approval_gated(p, "booking_create_booking", args={})
    assert not approval_gated(p, "booking_get_price", args={})


@pytest.mark.parametrize("bad", ["tool:", "tool:arg=", ":arg=x", "tool:=x"])
def test_malformed_rules_fail_profile_validation(bad):
    with pytest.raises(Exception):
        profile(approval_required=[bad])


async def test_gateway_applies_argument_rules(seen):
    gw = make(seen, profile=profile(approval_required=["home_lookup:key!=safe_*"],
                                    approval_mode="strict"))
    with gw.turn("inst", "r") as log:
        ok = await gw.call_tool("home_lookup", {"key": "safe_one"}, instance="inst")
        bad = await gw.call_tool("home_lookup", {"key": "secret"}, instance="inst")
    assert not ok.isError and bad.isError and "approval_mode: strict" in text(bad)
