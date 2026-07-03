from __future__ import annotations

import re

import httpx
from pydantic_ai.capabilities import Toolset
from pydantic_ai.toolsets import FunctionToolset

# Same pattern as AgentProfile.name — peer names double as Docker container names.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

_DEFAULT_TIMEOUT_S = 120


def build_peer_capability(cfg: dict) -> Toolset:
    """
    Build the Peer capability: injects a `call_agent` tool that lets an agent
    prompt an explicit allowlist of peer agents over HTTP.

    PydanticAI has no "capability wrapping one function tool" primitive, so
    this wraps a single-function FunctionToolset in the generic Toolset
    capability instead.

    Usage in a profile:
        spec:
          capabilities:
            - Peer:
                agents: [researcher, writer]
                timeout_s: 120   # optional, default 120
    """
    agents = cfg.get("agents")
    if not agents:
        raise ValueError(
            "Peer capability requires a non-empty 'agents' allowlist, e.g. "
            "{'Peer': {'agents': ['researcher', 'writer']}}."
        )

    invalid = [a for a in agents if not _AGENT_NAME_RE.match(a)]
    if invalid:
        raise ValueError(
            f"Peer capability 'agents' contains invalid name(s): {invalid}. "
            "Names must match ^[a-z0-9][a-z0-9_-]{0,62}$ (same pattern as AgentProfile.name)."
        )

    timeout_s = cfg.get("timeout_s", _DEFAULT_TIMEOUT_S)
    allowlist = list(agents)

    async def call_agent(agent: str, prompt: str) -> str:
        if agent not in allowlist:
            return f"ERROR: agent '{agent}' is not in this agent's peer allowlist: {allowlist}"

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as http:
                resp = await http.post(f"http://{agent}:8000/run", json={"prompt": prompt})
                resp.raise_for_status()
                return resp.json()["output"]
        except httpx.ConnectError:
            return f"ERROR: could not reach agent '{agent}' — it may not be running."
        except httpx.TimeoutException:
            return f"ERROR: agent '{agent}' did not respond within {timeout_s}s."
        except httpx.HTTPStatusError as e:
            return f"ERROR: agent '{agent}' returned HTTP {e.response.status_code}."

    call_agent.__name__ = "call_agent"
    call_agent.__doc__ = (
        "Send a prompt to a peer agent and return its reply.\n\n"
        f"Allowed agents: {allowlist}."
    )

    toolset = FunctionToolset([call_agent])
    return Toolset(toolset=toolset, id="Peer")
