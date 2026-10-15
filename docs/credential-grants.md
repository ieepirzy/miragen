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

**The trust boundary is write access to the profile.** Whoever can create,
update or import an agent through miragend decides which of the daemon's
keys that agent gets, by naming them in `credentials:` or by implying them
(a model, a `${…}` reference). Grants do not protect keys from someone
who can write profiles. They protect each agent from holding keys its
profile never asked for. The agent itself is outside that boundary: its
`agent.yaml` sits in its read-write `/agent` mount, so miragend never
derives credentials from that file after it has written it (see
[Updates](#updates)).

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
| A pydantic-ai model anywhere: `spec.model`, `memory.extraction.model`, `memory.recall.model`, an `ImageGeneration` `fallback_model` | That provider's key. `openai:` / `openai-chat:` / `openai-responses:` → `OPENAI_API_KEY`, `anthropic:` → `ANTHROPIC_API_KEY`, `google-gla:` → `GEMINI_API_KEY`, `groq:` → `GROQ_API_KEY`, and so on (the full table is `PROVIDER_CREDENTIAL_ENV`). The provider is read by pydantic-ai's own `parse_model_id`, so the bare names it still accepts count too: `gpt-4o`, `o3-mini` → `OPENAI_API_KEY`, `claude-…` → `ANTHROPIC_API_KEY`, `gemini-…` → `GOOGLE_API_KEY` / `GEMINI_API_KEY`. `gateway/<provider>:model` → `PYDANTIC_AI_GATEWAY_API_KEY` / `PAIG_API_KEY`. An unknown provider implies nothing. |
| `executor: kimi-code` | `KIMI_API_KEY`, `MOONSHOT_API_KEY`, `KIMI_CODE_API_KEY` |
| `executor: grok-build` | `XAI_API_KEY`, `GROK_CODE_XAI_API_KEY`, except with `grok_auth: subscription` |
| `voice:` | its `api_key_env`. For `provider: openai` this defaults to `OPENAI_API_KEY`. |
| Any `*_env` field naming a key, e.g. `bearer_token_env: FOO_API_KEY` | that key |
| A `${FOO_API_KEY}` or `${FOO_API_KEY:-…}` reference anywhere in the profile | that key. The loader inside the container fails on an undefined reference, so withholding it would crash-loop the agent. |

The subscription harnesses get **no** API key: `claude-code:`, `codex:` and
`grok-build:` models imply none. They run on the subscription.

`executor: spawn` implies nothing either: a spawned command gets no key
unless the profile grants it.

### Subscription executors: the metered key is explicit only

`executor: claude-code` and `executor: codex` run on the subscription: the
long-lived `CLAUDE_CODE_OAUTH_TOKEN`, and the mounted Codex `auth.json`. A
metered key outranks both. Claude Code prefers `ANTHROPIC_API_KEY` over its
token, and the codex executor used to log in with `CODEX_API_KEY` /
`OPENAI_API_KEY` whenever one was set. Either one silently switches the agent to
metered billing. So for these two executors:

- `ANTHROPIC_API_KEY` (claude-code) and `CODEX_API_KEY` / `OPENAI_API_KEY`
  (codex) reach the agent **only when `credentials:` lists them**. No
  implied route delivers them: not a memory model (`anthropic:` /
  `openai:`), not a `${…}` reference, not a `*_env` field.
- The executor enforces the same rule inside the container. At startup it
  removes those keys from its own environment unless the profile grants
  them, before any Claude Code or Codex child process can inherit them.
  `ANTHROPIC_AUTH_TOKEN` is never grantable and is always removed for
  claude-code. This also covers keys that arrive another way, through
  `MIRAGEND_AGENT_ENV_PASSTHROUGH` or a hand-run container.
- The codex executor calls `login_api_key` only with a granted key
  (`CODEX_API_KEY` first, then `OPENAI_API_KEY`).

The grant is all or nothing per key. A claude-code agent with an
`anthropic:` memory model needs `credentials: [ANTHROPIC_API_KEY]` for that
model to work, and **then Claude Code bills that key as well**. Use a
`claude-code:` memory model to stay on the subscription. The same holds for
codex with an `openai:` memory model.

This is a behaviour change. Before it, a codex executor got
`CODEX_API_KEY` / `OPENAI_API_KEY` automatically, and the key won over a
mounted `auth.json`. A deployment that relied on that metered path must add
the key to the profile's `credentials:`.

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

### Interpolated profiles

Models and the executor kind are read through the loader, as the container
will load them. A `${VAR}` resolves against **what the container will see**,
not the daemon's whole environment. That view holds the variables every
agent receives, the `MIRAGEND_AGENT_ENV_PASSTHROUGH` names, and the
`*_API_KEY` family. With `model: ${LANE_MODEL:-anthropic:claude-sonnet-4-6}`:

- If the daemon sets `LANE_MODEL` but does not forward it, the container
  uses the default, and so does the plan (`ANTHROPIC_API_KEY`).
- If `LANE_MODEL` is forwarded through passthrough, the container and the
  plan both see its value.

### Failing closed

If the daemon cannot load an agent's profile, it spawns that agent with no
keys and no token, and logs a warning.

### Updates

A config update plans the agent's unit from the **submitted** profile text.
It then compares the planned environment and secret mounts with the ones
the agent's unit **actually has**: the spawn driver's stored definition,
which lives outside the agent's mount. Any difference recreates the
container. A plain restart would keep the old environment. This covers:

- a new model provider, a `claude-code:` model, a grant added or dropped;
- an **unchanged** profile re-submitted after a daemon upgrade narrowed
  what it forwards. This is how an existing agent loses keys it no longer
  should hold;
- a change in the daemon's own forwarded settings.

A recreate that keeps the executor kind keeps the runtime image the
container already runs, so the digest pin survives. A spawn driver that
cannot report its stored definition gets a recreate on every update.

miragend keeps its own copy of each agent's last successfully applied
profile, under `<workspace>/.miragend/profiles/`, outside every agent mount.
It is written after a create, import or update has booted, and removed with
the agent. The update reads the agent's previous executor kind from this
copy. The on-disk `agent.yaml` could have been rewritten by the agent.

If the update fails to boot, the rollback:

- writes that copy back as `agent.yaml`;
- re-applies the previous unit **verbatim**: the same environment, secrets
  and image the agent had.

The rollback never recomputes credentials from profile text, so a profile
the agent rewrote cannot widen its keys that way. An agent applied before
the copy existed has no copy. Its rollback writes back `agent.yaml` as the
update found it, but the unit is still the previous definition, so its
credentials are the same as before.

### Not covered

`MIRAGEND_AGENT_ENV_PASSTHROUGH` is unchanged. It is an operator's explicit
broadcast of named variables to **every** agent, outside the per-profile
grants. Do not list provider keys or `CLAUDE_CODE_OAUTH_TOKEN` there. Both
have scoped paths now, and passthrough would bypass them. miragend logs a
startup warning when it lists either.

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
   For claude-code and codex executors, also add the metered key if the
   agent is meant to use it (`ANTHROPIC_API_KEY`; `CODEX_API_KEY` /
   `OPENAI_API_KEY`). Without the grant they run on the subscription.
3. **Re-create existing agents.** A running container keeps the environment
   it was created with until it is re-created. Until then, agents created
   before the upgrade still hold every key. Re-submit each agent's profile
   through a config update. Even an unchanged profile recreates the
   container when its unit holds more than the profile now entitles it to.
   This step is what actually revokes the keys.
   - **An agent pinned to a pre-v6 runtime image cannot take `credentials:`
     in place.** A same-kind update keeps the image digest the agent runs,
     and that image rejects contract v6. Export the agent, delete it, add
     `credentials:` to the exported `agent.yaml`, and import it. Import (like
     create) validates against the current base image and pins that.
     Re-submitting the profile without `credentials:` works in place and
     still revokes keys.
4. **Remove scoped variables from `MIRAGEND_AGENT_ENV_PASSTHROUGH`.** If it
   lists `CLAUDE_CODE_OAUTH_TOKEN` or a provider key (`*_API_KEY`,
   `*_API_KEY_FILE`) from before automatic delivery existed, remove the
   entry. Otherwise passthrough still broadcasts that variable to every
   agent. miragend logs a warning at startup while it does.
