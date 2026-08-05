"""
miragend — the miragen swarm lifecycle daemon.

The one process that holds the Docker socket and owns the swarm workspace
(agents/, compose.yml). Everything else — miragen-mcp, mirarun, any future
control plane — is a client of its HTTP API and never touches Docker or the
workspace directly. Agents cannot announce themselves into the registry:
membership is derived from the workspace and container state this daemon
itself maintains, so a compromised agent cannot forge or poison it.

Install with the daemon extra:  pip install miragen[daemon]
Run with:                       miragend
"""

from miragen.daemon.core import (
    AgentExists,
    AgentNotFound,
    DaemonError,
    InvalidAgentName,
    LifecycleCore,
    ValidationFailed,
)

__all__ = [
    "AgentExists",
    "AgentNotFound",
    "DaemonError",
    "InvalidAgentName",
    "LifecycleCore",
    "ValidationFailed",
]
