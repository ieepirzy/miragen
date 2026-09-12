"""Voice backends (docs/design/voice.md): text in, speech out.

miragen owns the speak contract. The `http` provider POSTs miragen's schema
— JSON ``{"text": ..., "voice": ..., "agent": ...}``, optional bearer auth —
to any endpoint implementing it; that endpoint owns synthesis AND playback,
answering 202/204 with no body (it played the audio itself) or a 2xx with an
``audio/*`` body (miragen stores it as a run artifact). External endpoints
conform to this schema; miragen is never adapted to a particular endpoint's
API. Cloud providers (``openai``) synthesize to bytes — a container has no
speaker, so the stored artifact is the deliverable.

Credentials arrive by env var NAME (``api_key_env``), resolved at call time
— after the app's ``_load_file_secrets()`` — and ride the daemon's existing
``*_API_KEY`` / ``*_API_KEY_FILE`` forwarding into the container.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from miragen.models import VoiceSpec

logger = logging.getLogger(__name__)

_EXT_BY_CONTENT_TYPE = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/flac": "flac",
    "audio/aac": "aac",
    "audio/webm": "webm",
}

_REQUEST_TIMEOUT_S = 60.0


class VoiceError(Exception):
    """The provider refused or failed a speak call; the message is what the
    agent (or operator) sees, and says what to fix."""


@dataclass(frozen=True)
class SpeechAudio:
    """Synthesized audio a provider handed back (as opposed to playing it)."""

    data: bytes
    extension: str


class VoiceBackend:
    """One implementation per provider. `speak` returns SpeechAudio when the
    provider hands audio back, or None when it handled playback itself."""

    provider = "abstract"

    def __init__(
        self,
        spec: VoiceSpec,
        agent_name: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.spec = spec
        self.agent_name = agent_name
        # Test seam: httpx.MockTransport in, no network out.
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S, transport=self._transport)

    async def speak(self, text: str, *, voice: str | None = None) -> SpeechAudio | None:
        raise NotImplementedError


class HTTPVoiceBackend(VoiceBackend):
    """Self-hosted / local endpoint implementing miragen's speak schema."""

    provider = "http"

    async def speak(self, text: str, *, voice: str | None = None) -> SpeechAudio | None:
        headers = {}
        if self.spec.api_key_env:
            token = os.environ.get(self.spec.api_key_env)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        payload: dict = {"text": text, "agent": self.agent_name}
        effective_voice = voice or self.spec.voice
        if effective_voice:
            payload["voice"] = effective_voice

        async with self._client() as client:
            try:
                resp = await client.post(self.spec.url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise VoiceError(f"voice endpoint unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise VoiceError(
                f"voice endpoint answered {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code in (202, 204) or not resp.content:
            return None  # the endpoint played the audio itself
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        if content_type.startswith("audio/"):
            return SpeechAudio(
                data=resp.content,
                extension=_EXT_BY_CONTENT_TYPE.get(content_type, "bin"),
            )
        # A 2xx with a non-audio body is treated as "handled": the schema
        # allows e.g. a JSON status document from a playback endpoint.
        logger.debug(
            "voice endpoint returned non-audio content-type %r — treating as played",
            content_type,
        )
        return None


class OpenAIVoiceBackend(VoiceBackend):
    """OpenAI TTS (POST /v1/audio/speech). Returns mp3 bytes."""

    provider = "openai"
    ENDPOINT = "https://api.openai.com/v1/audio/speech"
    DEFAULT_MODEL = "gpt-4o-mini-tts"
    DEFAULT_VOICE = "alloy"

    async def speak(self, text: str, *, voice: str | None = None) -> SpeechAudio | None:
        key_env = self.spec.api_key_env or "OPENAI_API_KEY"
        api_key = os.environ.get(key_env)
        if not api_key:
            raise VoiceError(
                f"voice provider 'openai' requires the {key_env} environment "
                "variable — set it on the daemon (it is auto-forwarded) or "
                "point voice.api_key_env at the variable that holds the key"
            )
        payload = {
            "model": self.spec.model or self.DEFAULT_MODEL,
            "voice": voice or self.spec.voice or self.DEFAULT_VOICE,
            "input": text,
        }
        async with self._client() as client:
            try:
                resp = await client.post(
                    self.ENDPOINT,
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except httpx.HTTPError as exc:
                raise VoiceError(f"OpenAI TTS unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise VoiceError(f"OpenAI TTS answered {resp.status_code}: {resp.text[:200]}")
        return SpeechAudio(data=resp.content, extension="mp3")


_BACKENDS: dict[str, type[VoiceBackend]] = {
    "http": HTTPVoiceBackend,
    "openai": OpenAIVoiceBackend,
}


def build_voice_backend(
    spec: VoiceSpec,
    agent_name: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> VoiceBackend:
    try:
        backend_cls = _BACKENDS[spec.provider]
    except KeyError:  # pragma: no cover — the Literal already forbids this
        raise VoiceError(f"unknown voice provider '{spec.provider}'") from None
    return backend_cls(spec, agent_name, transport=transport)
