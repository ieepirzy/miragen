# Per-profile credential grants

miragend holds a deployment's provider keys: as `NAME_API_KEY` env vars, or
as Docker secrets named by `NAME_API_KEY_FILE`. Until this change it handed
**every** one of them to **every** agent it spawned. It mounted each
`*_API_KEY_FILE` secret and forwarded each `*_API_KEY` / `*_API_KEY_FILE`
variable, whatever the agent's profile said. An agent that needed one
provider could therefore read every key the daemon held. A prompt-injected or
misbehaving agent could spend or leak keys it never needed.

Now an agent receives **only the keys its profile implies or declares**, and
none by default. The code is in `miragen/credentials.py`; miragend applies it
in `daemon/core.py`.

## What an agent receives

A grant is a `*_API_KEY` name. When the daemon holds the key, granting
`NAME_API_KEY` forwards three things:

- `NAME_API_KEY`, if the daemon has it set;
- `NAME_API_KEY_FILE`, if set;
- the Docker secret that `NAME_API_KEY_FILE` points at.

Nothing outside the `*_API_KEY` family can be granted this way.

### Implied: no configuration needed

| The profile says | The agent gets |
|---|---|
| A pydantic-ai model anywhere: `spec.model`, `memory.extraction.model`, `memory.recall.model`, an `ImageGeneration` `fallback_model` | That provider's key. `openai:` / `openai-chat:` / `openai-responses:` → `OPENAI_API_KEY`, `anthropic:` → `ANTHROPIC_API_KEY`, `google-gla:` → `GEMINI_API_KEY`, `groq:` → `GROQ_API_KEY`, and so on (the full table is `PROVIDER_CREDENTIAL_ENV`). An unknown provider implies nothing. |
| `executor: codex` | `CODEX_API_KEY`, `OPENAI_API_KEY` (the metered fallback) |
| `executor: kimi-code` | `KIMI_API_KEY`, `MOONSHOT_API_KEY`, `KIMI_CODE_API_KEY` |
| `executor: grok-build` | `XAI_API_KEY`, `GROK_CODE_XAI_API_KEY`, except with `grok_auth: subscription` |
| `voice:` | its `api_key_env`. For `provider: openai` this defaults to `OPENAI_API_KEY`. |
| Any `*_env` field naming a key, e.g. `bearer_token_env: FOO_API_KEY` | that key |
| A `${FOO_API_KEY}` or `${FOO_API_KEY:-…}` reference anywhere in the profile | that key. The loader inside the container fails on an undefined reference, so withholding it would crash-loop the agent. |

The subscription harnesses get **no** API key: `claude-code:`, `codex:` and
`grok-build:` models, and `executor: claude-code`. They run on the
subscription. Inside Claude Code, `ANTHROPIC_API_KEY` outranks the
subscription token, so forwarding it would silently switch the agent to
metered billing.

### Declared: `credentials`

Anything else must be named in the profile:

```yaml
credentials: [TAVILY_API_KEY]   # e.g. a tool that reads the key from its env
```

- Each entry must match `^[A-Z][A-Z0-9_]*_API_KEY$`. A profile cannot grant
  itself `MIRAGEN_INTERNAL_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` or any other
  daemon variable.
- A grant for a key the daemon does not hold forwards nothing.
- Declaring the field implies **profile contract v6**. miragend will not
  place such a profile on an older runtime image, because older images reject
  the unknown key. An empty list is omitted from serialized profiles, so
  re-submitting a dumped profile never raises its contract.

### The subscription token

`CLAUDE_CODE_OAUTH_TOKEN` goes to agents that run Claude Code, and to no
others. Those are agents with `executor: claude-code`, and agents with a
`claude-code:` model: `spec.model` (the harness), or `memory.recall.model`
or `memory.extraction.model` (these run headless `claude -p` inside the
agent). Models are read through the loader, so
`model: ${LANE_MODEL:-claude-code:…}` counts.
Previously only `executor: claude-code` got it, which left `claude-code:`
model-tier profiles without a credential.

### Failing closed

If the daemon cannot load an agent's profile, it spawns that agent with no
keys and no token, and logs a warning.

### Updates

A config update that changes what the agent is entitled to recreates the
container, even when the executor kind stays the same. This covers a new
model provider, a `claude-code:` model, and a grant added or dropped. A
plain restart would keep the old environment. On this path the recreate
keeps the runtime image the container already runs. A revoked key therefore
leaves both the environment and the secret mounts.

### Not covered

`MIRAGEND_AGENT_ENV_PASSTHROUGH` is unchanged. It is an operator's explicit
broadcast of named variables to **every** agent, outside the per-profile
grants. Do not list provider keys or `CLAUDE_CODE_OAUTH_TOKEN` there. Both
have scoped paths now, and passthrough would bypass them.

## Migration

1. **Upgrade miragend and the runtime image together.** The runtime image
   must advertise contract 6 before any profile declares `credentials`.
2. **Find agents that read a key their profile does not imply.** Typical
   cases:
   - a `tools.py` tool or a custom capability that reads `SOME_API_KEY` from
     the environment;
   - a claude-code agent that relied on metered `ANTHROPIC_API_KEY` instead
     of the subscription token.

   Add each such key to `credentials:`.
3. **Re-create existing agents.** A running container keeps the environment
   it was created with until it is re-created. Until then, agents created
   before the upgrade still hold every key. Re-create them through a config
   update that changes their credentials, or by exporting, deleting and
   importing them. This step is what actually revokes the keys.
4. **Remove scoped variables from `MIRAGEND_AGENT_ENV_PASSTHROUGH`.** If it
   lists `CLAUDE_CODE_OAUTH_TOKEN` or a provider key from before automatic
   delivery existed, remove the entry. Otherwise passthrough still
   broadcasts that variable to every agent.
