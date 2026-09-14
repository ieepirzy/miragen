"""miragend's session plane: participation of EXTERNAL harness sessions
(Claude Code, Codex, future adapters) in the memory system.

Design record: docs/design/external-sessions.md. The plane is
harness-agnostic — adapters (miragen_hook) serialize harness payloads
into the normalized event vocabulary; the plane owns session
registration, project/scope resolution, context retrieval and injection,
out-of-band capture, compaction/finalization, journaling and telemetry,
and delegates every durable write to the existing MemoryLifecycle →
Loimi path.
"""

from miragen.daemon.sessions.config import SessionsConfig, load_sessions_config
from miragen.daemon.sessions.models import EventEnvelope, ExternalSession
from miragen.daemon.sessions.plane import SessionPlane

__all__ = [
    "EventEnvelope",
    "ExternalSession",
    "SessionPlane",
    "SessionsConfig",
    "load_sessions_config",
]
