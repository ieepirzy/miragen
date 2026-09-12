"""Voice capability (voice/v1) — docs/design/voice.md.

miragen owns the speak contract: the http provider POSTs miragen's schema to
any endpoint implementing it (the endpoint owns synthesis and playback);
cloud providers synthesize to bytes that land as run artifacts.
"""

import json
import sys

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import miragen.app  # noqa: F401
app_module = sys.modules["miragen.app"]
from miragen.models import AgentProfile, VoiceSpec
from miragen.runs import RunStore
from miragen.voice import (
    HTTPVoiceBackend,
    OpenAIVoiceBackend,
    SpeechAudio,
    VoiceError,
    build_voice_backend,
)
from miragen.voice_mcp import build_voice_mcp


def _make_profile(**kw):
    return AgentProfile.model_validate({
        "name": "test-agent",
        "mode": "interactive",
        "triggers": [{"type": "http"}],
        "spec": {"model": "anthropic:claude-haiku-4-5", "instructions": "Test."},
        **kw,
    })


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._voice = None


# ── Profile models ───────────────────────────────────────────────────────────


class TestVoiceSpec:
    def test_http_requires_url(self):
        with pytest.raises(Exception, match="requires `url`"):
            VoiceSpec.model_validate({"provider": "http"})

    def test_openai_rejects_url(self):
        with pytest.raises(Exception, match="only applies to the http provider"):
            VoiceSpec.model_validate({"provider": "openai", "url": "http://x"})

    def test_http_rejects_model(self):
        with pytest.raises(Exception, match="cloud providers"):
            VoiceSpec.model_validate({"provider": "http", "url": "http://x", "model": "tts-1"})

    def test_minimal_http(self):
        spec = VoiceSpec.model_validate({"url": "http://tts.lan:8880/speak"})
        assert spec.provider == "http"

    def test_minimal_openai(self):
        spec = VoiceSpec.model_validate({"provider": "openai"})
        assert spec.api_key_env is None  # backend defaults to OPENAI_API_KEY

    def test_on_complete_speak_requires_voice_block(self):
        with pytest.raises(Exception, match="requires a `voice:` block"):
            _make_profile(on_complete={"speak": True})

    def test_on_complete_speak_with_voice_block(self):
        profile = _make_profile(
            voice={"url": "http://tts.lan/speak"}, on_complete={"speak": True}
        )
        assert profile.on_complete.speak is True

    def test_executor_profile_may_have_voice(self):
        profile = AgentProfile.model_validate({
            "name": "a", "mode": "interactive", "triggers": [{"type": "http"}],
            "voice": {"url": "http://tts.lan/speak"},
            "executor": {"executor": "codex", "instructions": "work"},
        })
        assert profile.voice is not None


# ── Backends ─────────────────────────────────────────────────────────────────


def _http_backend(handler, **spec_kw):
    spec = VoiceSpec.model_validate({"url": "http://tts.lan/speak", **spec_kw})
    return HTTPVoiceBackend(spec, "test-agent", transport=httpx.MockTransport(handler))


class TestHTTPVoiceBackend:
    async def test_202_means_played(self):
        seen = {}

        def handler(request):
            seen["payload"] = json.loads(request.content)
            return httpx.Response(202)

        backend = _http_backend(handler, voice="kore")
        assert await backend.speak("hello") is None
        assert seen["payload"] == {"text": "hello", "agent": "test-agent", "voice": "kore"}

    async def test_voice_override_beats_profile_default(self):
        seen = {}

        def handler(request):
            seen["payload"] = json.loads(request.content)
            return httpx.Response(204)

        backend = _http_backend(handler, voice="kore")
        await backend.speak("hello", voice="aura")
        assert seen["payload"]["voice"] == "aura"

    async def test_audio_body_is_returned_with_extension(self):
        def handler(request):
            return httpx.Response(
                200, content=b"MP3BYTES", headers={"content-type": "audio/mpeg"}
            )

        audio = await _http_backend(handler).speak("hello")
        assert audio == SpeechAudio(data=b"MP3BYTES", extension="mp3")

    async def test_wav_content_type_maps_to_wav(self):
        def handler(request):
            return httpx.Response(
                200, content=b"RIFF", headers={"content-type": "audio/wav"}
            )

        assert (await _http_backend(handler).speak("x")).extension == "wav"

    async def test_non_audio_2xx_body_means_played(self):
        def handler(request):
            return httpx.Response(200, json={"status": "queued"})

        assert await _http_backend(handler).speak("hello") is None

    async def test_4xx_raises_voice_error(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        with pytest.raises(VoiceError, match="500"):
            await _http_backend(handler).speak("hello")

    async def test_unreachable_raises_voice_error(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        with pytest.raises(VoiceError, match="unreachable"):
            await _http_backend(handler).speak("hello")

    async def test_bearer_token_from_env(self, monkeypatch):
        monkeypatch.setenv("TTS_API_KEY", "sekrit")
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(204)

        await _http_backend(handler, api_key_env="TTS_API_KEY").speak("hello")
        assert seen["auth"] == "Bearer sekrit"


class TestOpenAIVoiceBackend:
    def _backend(self, handler, **spec_kw):
        spec = VoiceSpec.model_validate({"provider": "openai", **spec_kw})
        return OpenAIVoiceBackend(spec, "test-agent", transport=httpx.MockTransport(handler))

    async def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        backend = self._backend(lambda request: httpx.Response(200))
        with pytest.raises(VoiceError, match="OPENAI_API_KEY"):
            await backend.speak("hello")

    async def test_synthesizes_mp3_with_defaults(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        seen = {}

        def handler(request):
            seen["payload"] = json.loads(request.content)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, content=b"AUDIO")

        audio = await self._backend(handler).speak("hello")
        assert audio == SpeechAudio(data=b"AUDIO", extension="mp3")
        assert seen["auth"] == "Bearer sk-test"
        assert seen["payload"] == {
            "model": OpenAIVoiceBackend.DEFAULT_MODEL,
            "voice": OpenAIVoiceBackend.DEFAULT_VOICE,
            "input": "hello",
        }

    async def test_spec_model_voice_and_key_env_override(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-2")
        seen = {}

        def handler(request):
            seen["payload"] = json.loads(request.content)
            return httpx.Response(200, content=b"A")

        backend = self._backend(handler, model="tts-1-hd", voice="nova", api_key_env="MY_KEY")
        await backend.speak("hi")
        assert seen["payload"]["model"] == "tts-1-hd"
        assert seen["payload"]["voice"] == "nova"

    async def test_api_error_raises(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        backend = self._backend(lambda request: httpx.Response(429, text="rate limited"))
        with pytest.raises(VoiceError, match="429"):
            await backend.speak("hello")


class TestBuildVoiceBackend:
    def test_dispatches_by_provider(self):
        http = build_voice_backend(VoiceSpec.model_validate({"url": "http://x"}), "a")
        openai = build_voice_backend(VoiceSpec.model_validate({"provider": "openai"}), "a")
        assert isinstance(http, HTTPVoiceBackend)
        assert isinstance(openai, OpenAIVoiceBackend)


# ── App integration ──────────────────────────────────────────────────────────


class _StubBackend:
    """Records speak calls; returns a queued result per call."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def speak(self, text, *, voice=None):
        self.calls.append({"text": text, "voice": voice})
        return self.results.pop(0)


class TestSpeakTool:
    async def test_played_audio_reports_spoken(self):
        tool = app_module._make_speak_tool(_StubBackend([None]))
        assert await tool("hello") == "Spoken."

    async def test_synthesized_audio_stored_against_current_run(self, tmp_path):
        app_module._run_store = RunStore(root=tmp_path)
        record = app_module._run_store.start(agent_name="a", trigger="http", prompt="p")
        token = app_module._current_run_id.set(record.run_id)
        try:
            tool = app_module._make_speak_tool(_StubBackend([SpeechAudio(b"X", "mp3")]))
            result = await tool("hello")
        finally:
            app_module._current_run_id.reset(token)
        stored = tmp_path / record.run_id / "audio" / "001.mp3"
        assert stored.read_bytes() == b"X"
        assert str(stored) in result

    async def test_synthesized_audio_without_run_is_discarded(self, tmp_path):
        app_module._run_store = RunStore(root=tmp_path)
        tool = app_module._make_speak_tool(_StubBackend([SpeechAudio(b"X", "mp3")]))
        assert "discarded" in await tool("hello")

    async def test_sequential_calls_number_artifacts(self, tmp_path):
        app_module._run_store = RunStore(root=tmp_path)
        record = app_module._run_store.start(agent_name="a", trigger="http", prompt="p")
        token = app_module._current_run_id.set(record.run_id)
        try:
            backend = _StubBackend([SpeechAudio(b"1", "mp3"), SpeechAudio(b"2", "mp3")])
            tool = app_module._make_speak_tool(backend)
            await tool("one")
            await tool("two")
        finally:
            app_module._current_run_id.reset(token)
        audio_dir = tmp_path / record.run_id / "audio"
        assert sorted(p.name for p in audio_dir.iterdir()) == ["001.mp3", "002.mp3"]


class TestAudioArtifactAnnotation:
    def test_finished_record_gains_audio_artifacts(self, tmp_path):
        store = RunStore(root=tmp_path)
        app_module._run_store = store
        record = store.start(agent_name="a", trigger="http", prompt="p")
        store.finish(record, status="succeeded", output="ok")
        audio_dir = tmp_path / record.run_id / "audio"
        audio_dir.mkdir(parents=True)
        (audio_dir / "001.mp3").write_bytes(b"X")

        app_module._record_audio_artifacts(record.run_id)

        assert app_module._run_store.get(record.run_id).audio_artifacts == [
            str(audio_dir / "001.mp3")
        ]

    def test_no_audio_dir_is_a_noop(self, tmp_path):
        store = RunStore(root=tmp_path)
        app_module._run_store = store
        record = store.start(agent_name="a", trigger="http", prompt="p")
        app_module._record_audio_artifacts(record.run_id)
        assert store.get(record.run_id).audio_artifacts is None


class TestOnCompleteSpeak:
    async def test_output_spoken_through_backend(self):
        app_module._profile = _make_profile(
            voice={"url": "http://tts.lan/speak"}, on_complete={"speak": True}
        )
        backend = _StubBackend([None])
        app_module._voice = backend

        await app_module._handle_on_complete("the result")

        assert backend.calls == [{"text": "the result", "voice": None}]

    async def test_synthesized_output_stored_against_run(self, tmp_path):
        app_module._profile = _make_profile(
            voice={"url": "http://tts.lan/speak"}, on_complete={"speak": True}
        )
        store = RunStore(root=tmp_path)
        app_module._run_store = store
        record = store.start(agent_name="a", trigger="cron", prompt="p")
        store.finish(record, status="succeeded", output="ok")
        app_module._voice = _StubBackend([SpeechAudio(b"OUT", "mp3")])

        await app_module._handle_on_complete("the result", run_id=record.run_id)

        stored = tmp_path / record.run_id / "audio" / "001.mp3"
        assert stored.read_bytes() == b"OUT"
        assert store.get(record.run_id).audio_artifacts == [str(stored)]

    async def test_no_speak_flag_never_calls_backend(self):
        app_module._profile = _make_profile(voice={"url": "http://tts.lan/speak"})
        backend = _StubBackend([None])
        app_module._voice = backend
        await app_module._handle_on_complete("out")
        assert backend.calls == []


class TestFactoryExtraTools:
    def test_extra_tools_registered_as_plain_tools(self):
        from miragen.factory import build_agent

        async def speak(text: str) -> str:
            return "ok"

        profile = _make_profile()
        with patch("miragen.factory.Agent") as MockAgent, \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            build_agent(profile, extra_tools=[speak])
            MockAgent.return_value.tool_plain.assert_called_once_with(speak)

    def test_no_extra_tools_registers_none(self):
        from miragen.factory import build_agent

        profile = _make_profile()
        with patch("miragen.factory.Agent") as MockAgent, \
             patch("miragen.factory.resolve_capabilities", return_value=[]):
            build_agent(profile)
            MockAgent.return_value.tool_plain.assert_not_called()


class TestVoiceMCP:
    async def test_speak_tool_spoken(self):
        backend = _StubBackend([None])
        mcp = build_voice_mcp(lambda: (backend, lambda run_id, audio: None))
        blocks, structured = await mcp.call_tool("speak", {"text": "hello"})
        assert structured["result"] == "Spoken."
        assert backend.calls[0]["text"] == "hello"

    async def test_speak_tool_stores_synthesized_audio(self):
        stored = {}

        def store_audio(run_id, audio):
            stored["run_id"] = run_id
            stored["audio"] = audio
            return "/agent/runs/r1/audio/001.mp3"

        backend = _StubBackend([SpeechAudio(b"X", "mp3")])
        mcp = build_voice_mcp(lambda: (backend, store_audio))
        blocks, structured = await mcp.call_tool("speak", {"text": "hi", "run_id": "r1"})
        assert "001.mp3" in structured["result"]
        assert stored["run_id"] == "r1"
        assert stored["audio"].data == b"X"

    async def test_unresolved_run_reported_honestly(self):
        backend = _StubBackend([SpeechAudio(b"X", "mp3")])
        mcp = build_voice_mcp(lambda: (backend, lambda run_id, audio: None))
        blocks, structured = await mcp.call_tool("speak", {"text": "hi"})
        assert "no run" in structured["result"]

    async def test_unconfigured_voice_is_a_tool_error(self):
        mcp = build_voice_mcp(lambda: (None, lambda run_id, audio: None))
        with pytest.raises(Exception, match="no voice configured"):
            await mcp.call_tool("speak", {"text": "hi"})


class TestResolveAndStore:
    def test_explicit_run_id_wins(self, tmp_path):
        store = RunStore(root=tmp_path)
        app_module._run_store = store
        record = store.start(agent_name="a", trigger="http", prompt="p")
        saved = app_module._resolve_and_store_audio(record.run_id, SpeechAudio(b"X", "mp3"))
        assert saved == str(tmp_path / record.run_id / "audio" / "001.mp3")

    def test_single_running_run_resolved(self, tmp_path):
        store = RunStore(root=tmp_path)
        app_module._run_store = store
        record = store.start(agent_name="a", trigger="http", prompt="p")
        saved = app_module._resolve_and_store_audio(None, SpeechAudio(b"X", "mp3"))
        assert saved == str(tmp_path / record.run_id / "audio" / "001.mp3")

    def test_ambiguous_running_runs_not_stored(self, tmp_path):
        store = RunStore(root=tmp_path)
        app_module._run_store = store
        store.start(agent_name="a", trigger="http", prompt="p1")
        store.start(agent_name="a", trigger="http", prompt="p2")
        assert app_module._resolve_and_store_audio(None, SpeechAudio(b"X", "mp3")) is None


class TestHealthVoice:
    async def test_health_reports_voice_configuration(self):
        from httpx import ASGITransport, AsyncClient

        from miragen.app import app

        app_module._profile = _make_profile(voice={"url": "http://tts.lan/speak"})
        app_module._agent = MagicMock(run=AsyncMock())
        app_module._voice = _StubBackend([])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            body = (await c.get("/health")).json()
        assert body["voice"] == {"configured": True, "provider": "http"}
        assert "voice/v1" in body["capabilities"]

    async def test_health_voice_unconfigured(self):
        from httpx import ASGITransport, AsyncClient

        from miragen.app import app

        app_module._profile = _make_profile()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            body = (await c.get("/health")).json()
        assert body["voice"] == {"configured": False, "provider": None}
