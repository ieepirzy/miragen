"""miragen's memory subsystem — the cognitive-lifecycle half of the memory
architecture pass (docs/miragen-memory-agent-architecture-pass.md §17.3).

Loimi owns persistence enforcement behind /memory/v1; this package owns
WHEN to capture, restore and checkpoint: the `MemoryClient` speaks the API,
`MemoryLifecycle` prepares context packets at the run boundary and captures
run outcomes durably, `guidance` carries the versioned always-supplied core
guide (§18.8), and `tools` exposes the small agent surface
(remember / read / checkpoint).
"""

from miragen.memory.client import MemoryAPIError, MemoryClient, MemoryUnavailable
from miragen.memory.lifecycle import MemoryLifecycle, MemoryPacket

__all__ = [
    "MemoryAPIError",
    "MemoryClient",
    "MemoryLifecycle",
    "MemoryPacket",
    "MemoryUnavailable",
]
