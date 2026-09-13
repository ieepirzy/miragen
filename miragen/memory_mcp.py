"""The MCP memory tools — the executor tier's door to the memory lifecycle
(§18.8), mirroring voice_mcp's serving pattern.

Mounted at `/mcp/memory` behind the MIRAGEN_INTERNAL_TOKEN guard; executor
profiles opt in via an `executor.mcp_servers` entry, exactly like
/mcp/ask-human and /mcp/voice. Run binding follows ask_human's rule: an
explicit run_id, else the single running run — that run's instance is the
state scope the tools act on.
"""

from __future__ import annotations

import json
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger("miragen.memory_mcp")


class MemoryToolError(Exception):
    """Raised when a memory tool cannot run; the message says what to do."""


def _resolve_run(run_store, run_id: str | None):
    if run_store is None:
        return None
    if run_id is not None:
        return run_store.get(run_id)
    running = run_store.list(limit=100, status="running")
    if len(running) == 1:
        return run_store.get(running[0].run_id)
    return None


def build_memory_mcp(get_state) -> FastMCP:
    """`get_state` is a zero-arg callable returning
    (lifecycle | None, run_store | None), read per call so the mounted
    server sees what the lifespan wired later."""
    mcp = FastMCP(
        "miragen-memory",
        instructions=(
            "miragen's memory tools. memory_checkpoint persists durable "
            "working state; memory_remember proposes one focused durable "
            "observation; memory_read fetches a record by id. A write "
            "counts as saved only when the result says accepted."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        # Same-deployment endpoint guarded by MIRAGEN_INTERNAL_TOKEN; see
        # intervention_mcp for why Host-header checking is disabled.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def _lifecycle_and_run(run_id: str | None):
        lifecycle, run_store = get_state()
        if lifecycle is None:
            raise MemoryToolError(
                "this agent has no memory configured — add a `memory:` block "
                "to the profile"
            )
        record = _resolve_run(run_store, run_id)
        return lifecycle, record

    @mcp.tool()
    async def memory_checkpoint(state: dict, run_id: str | None = None) -> str:
        """Persist durable working state (shallow-merged; null removes a key).

        Args:
            state: Fields to merge, e.g. {"goal": ..., "pending_actions": [...]}.
            run_id: Only needed when several runs are active.
        """
        lifecycle, record = _lifecycle_and_run(run_id)
        result = await lifecycle.checkpoint(
            instance=record.instance if record else None, patch=state
        )
        return json.dumps(result)

    @mcp.tool()
    async def memory_remember(content: str, run_id: str | None = None) -> str:
        """Propose one focused durable observation worth recalling later.

        Args:
            content: The observation, self-contained and specific.
            run_id: Only needed when several runs are active.
        """
        lifecycle, record = _lifecycle_and_run(run_id)
        result = await lifecycle.remember(
            instance=record.instance if record else None,
            run_id=record.run_id if record else None,
            content=content,
        )
        return json.dumps(result)

    @mcp.tool()
    async def memory_read(record_id: str) -> str:
        """Read one memory record by id.

        Args:
            record_id: The record id to fetch.
        """
        lifecycle, _ = _lifecycle_and_run(None)
        return json.dumps(await lifecycle.read(record_id))

    return mcp
