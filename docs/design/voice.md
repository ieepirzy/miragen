# ADR: Voice — a speak capability with cloud and self-hosted providers

Status: accepted (2026-09-12)

## Context

Agents should be able to speak: read a result aloud on home infrastructure,
or synthesize audio via a cloud TTS API. The two configurations differ in
what miragen holds — an API key (cloud) or a URL (any external endpoint).

miragen owns the speak contract. A self-hosted endpoint implements
miragen's schema and miragen is pointed at it — miragen is never adapted
to a particular endpoint's API. (mira-speak is a one-off; if it should be
drivable by miragen it grows a miragen-schema route, not the reverse.)

## Decision

1. **Profile config** — a new swarm-layer `voice:` block:

   ```yaml
   voice:
     provider: http            # http | openai (more adapters as needed)
     url: http://tts.lan:8880/speak   # http provider only
     api_key_env: OPENAI_API_KEY      # cloud providers; sensible per-provider default
     voice: alloy                     # default voice id, provider-defined
   ```

   Cloud credentials ride the existing `*_API_KEY` / `*_API_KEY_FILE` env
   forwarding — zero daemon changes.

2. **`VoiceBackend` seam**, same pattern as executor/publication backends:
   `async speak(text, *, voice) -> bytes | None`. Two v1 implementations:

   - **`http`** (self-hosted / local): `POST {url}` with miragen's schema —
     JSON `{"text": ..., "voice": ..., "agent": ...}`, optional bearer via
     `api_key_env`. The endpoint owns synthesis *and* playback. It may
     answer `202/204` empty (it played the audio itself) or `2xx` with an
     `audio/*` body (miragen stores it, below). This request schema is the
     public contract external endpoints implement.
   - **`openai`** (cloud): direct TTS API call, returns audio bytes.

3. **Audio artifacts.** When a backend returns bytes, they are written to
   the run's directory (`runs/<run_id>/audio/<n>.<ext>`) and referenced
   from the run record. A cloud container has no speaker; the artifact is
   the deliverable. Routing cloud-synthesized audio onward to a playback
   endpoint is explicitly out of scope for v1 (open question below).

4. **Exposure to the agent, both tiers:**
   - Model tier: a built-in `speak` capability resolved like other
     `spec.capabilities` entries, available when the profile has `voice:`.
   - Executor tier: a `/mcp/voice` mount beside `/mcp/ask-human`, same
     token guard, exposing the same backend as an MCP `speak` tool.

5. **`on_complete.speak: true`** — voice the run output through the
   configured backend as an additional on_complete channel. Small and
   high-value for autonomous agents; ships in v1.

## Consequences

- One schema, many voices: any local TTS stack joins by implementing one
  POST route; cloud quality is one `api_key_env` away.
- A profile without `voice:` is byte-for-byte unaffected; declaring the
  capability or `on_complete.speak` without `voice:` fails at load, loudly.

Open question (v2): chaining `provider: openai` + a playback `url` so
cloud-synthesized audio plays on local speakers; and whether long outputs
get a summarization pass before being voiced (ties into FUTURE_FEATURES
§4).
