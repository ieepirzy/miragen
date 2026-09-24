"""The MCP `speak` tool — the executor tier's door to the voice backend
(docs/design/voice.md), mirroring intervention_mcp's serving pattern.

miragen's own FastAPI app mounts this server (streamable HTTP, stateless)
at `/mcp/voice`, behind the same MIRAGEN_INTERNAL_TOKEN guard. Executor
profiles opt in by pointing an `executor.mcp_servers` entry at it:

    executor:
      mcp_servers:
        - name: miragen-voice
          url: http://localhost:8000/mcp/voice
          bearer_token_env: MIRAGEN_INTERNAL_TOKEN   # when the app is token-guarded

Synthesized audio (a provider that returns bytes rather than playing them)
is stored against the calling run: the app supplies a store callable that
resolves `run_id` (explicit, or the single running run) and writes into
that run's audio/ directory. Unresolvable = synthesized-but-not-stored,
reported honestly in the tool result — voice is best-effort by design.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from miragen.voice import SpeechAudio, VoiceBackend, VoiceError, with_speak_guidance

logger = logging.getLogger("miragen.voice_mcp")


def build_voice_mcp(get_state, *, speak_guidance: str | None = None) -> FastMCP:
    """Build the FastMCP server. `get_state` is a zero-arg callable returning
    (voice_backend | None, store_audio) — read per call so the server mounted
    at import time sees the state the lifespan (or a test) wired later.
    `store_audio(run_id | None, SpeechAudio) -> str | None` stores returned
    bytes against a run and answers the saved path, or None when no run is
    resolvable."""
    mcp = FastMCP(
        "miragen-voice",
        instructions=(
            "miragen's voice front door. Call speak to say something aloud "
            "through this agent's configured speech provider."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        # Same-deployment endpoint guarded by MIRAGEN_INTERNAL_TOKEN; see
        # intervention_mcp for why Host-header checking is disabled.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def speak(text: str, voice: str | None = None, run_id: str | None = None) -> str:
        """Speak `text` aloud through this agent's configured voice provider.

        Args:
            text: What to say (required).
            voice: Provider-defined voice id; the profile's default when omitted.
            run_id: Bind synthesized audio to this run — only needed when
                several runs are active and the provider returns audio files.
        """
        backend: VoiceBackend | None
        store_audio: Callable[[str | None, SpeechAudio], str | None]
        backend, store_audio = get_state()
        if backend is None:
            raise VoiceError(
                "this agent has no voice configured — add a `voice:` block "
                "to the profile"
            )
        audio = await backend.speak(text, voice=voice)
        if audio is None:
            return "Spoken."
        saved = store_audio(run_id, audio)
        if saved is not None:
            return f"Audio synthesized and stored at {saved}."
        return (
            "Audio synthesized, but no run could be resolved to store it "
            "against — pass run_id if you need the file kept."
        )

    speak.__doc__ = with_speak_guidance(speak.__doc__ or "", speak_guidance)
    mcp.tool()(speak)
    return mcp
