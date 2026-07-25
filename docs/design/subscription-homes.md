# Shared subscription homes (Codex / Kimi / Grok)

**Status:** design generalization of the shipped Codex model
([codex-auth.md](codex-auth.md)). Applies to new `kimi-code` and `grok-build`
executors; Codex behaviour is unchanged.
**Companion:** [kimi-and-grok-executors.md](kimi-and-grok-executors.md).

## 1. Through-line

> **One identity, one login, many ephemeral containers.**  
> Agent containers are consumers of a credential they never create.

The load-bearing constraint is the same as for Codex: containers spin up and
down constantly; interactive OAuth inside each spawn is impossible and wrong.
Subscription OAuth is the **primary** path for self-harnessed coding agents;
metered API keys are the CI / no-subscription fallback.

## 2. Product homes (verified conventions)

Each product already has a **home directory** for auth, config, and often
sessions. miragen maps a profile field onto the env var that product
documents — it does not invent a parallel store.

| Backend | Profile field | Env var | Default on host | Default in container profile | Creds (subscription) | Metered fallback |
|---|---|---|---|---|---|---|
| `codex` | `codex_home` | `CODEX_HOME` | `~/.codex` | `/agent/codex-home` | `auth.json` (ChatGPT OAuth) | `CODEX_API_KEY` / `OPENAI_API_KEY` |
| `kimi-code` | `kimi_home` | `KIMI_CODE_HOME` | `~/.kimi-code` | `/agent/kimi-home` | OAuth via Kimi Code login (config/store under home) | `KIMI_API_KEY` / `MOONSHOT_API_KEY` (and product equivalents) |
| `grok-build` | `grok_home` | `GROK_HOME` | `~/.grok` | `/agent/grok-home` | `auth.json` (SuperGrok / X Premium+ OAuth) | `XAI_API_KEY` |
| `claude-code` | *(none)* | — | `~/.claude` | mount or env | Claude Code OAuth at `~/.claude` | `ANTHROPIC_API_KEY` |

### Codex SDK note (re-verified 2026-07-25)

`openai-codex` 0.144.4:

- `CodexConfig` has **no** `codex_home` field.
- Spawn uses `env = os.environ.copy()` + optional `CodexConfig.env`.
- `default_codex_home()` → `Path.home() / ".codex"`.

miragen's `codex_home` is therefore a **profile → env** binding, which is the
correct pattern to copy for Kimi and Grok.

### Kimi

Documented: config and data under `~/.kimi-code/`, relocatable via
`KIMI_CODE_HOME`. Login: interactive `/login` or non-interactive device-code
(`kimi login`). `kimi-agent-sdk` reuses the same configuration/credentials as
the CLI.

### Grok Build

Documented: settings under `~/.grok/config.toml`, home overridable with
`$GROK_HOME`. Subscription OAuth lands as `auth.json` under that home;
sessions under `$GROK_HOME/sessions` (or `~/.grok/sessions`). Headless/ACP
also accept `XAI_API_KEY` as metered path.

## 3. Lifecycle

1. **Once**, out of band, operator (or future mirarun button) runs the
   product login against the **shared volume**:
   - `miragen codex-login --codex-home <volume>` (shipped)
   - `miragen kimi-login --kimi-home <volume>` (planned)
   - `miragen grok-login --grok-home <volume>` (planned)
2. Every agent container mounts that volume at the profile's `*_home` path.
   `prepare()` sets the env var and warns if neither OAuth store nor metered
   key is visible.
3. Containers come and go; the credential outlives them.
4. On expiry: re-run the login helper once (RO mount model) or accept shared
   RW refresh races (see Codex refresh nuance — same trade-off, no broker in
   v1).

## 4. Invariants

- **Agents never authenticate.** No browser, no device-code, no login
  endpoint inside the agent container process tree for normal turns.
- **Credentials are never profile content.** Profiles name paths and env
  *names* for MCP tokens; OAuth blobs live only on the home volume.
- **Loud empty-home warning** at startup; fail turns at the product layer if
  auth is missing (miragen does not invent a fake success).
- **MCP server bearer tokens** remain env-name references injected at spawn,
  identical to the Codex/Claude path.

## 5. Compose sketch

```yaml
volumes:
  codex-home:
  kimi-home:
  grok-home:

services:
  kimi-worker:
    image: ghcr.io/ieepirzy/miragen:latest   # + kimi-cli / SDK in image or layer
    environment:
      AGENT_PROFILE: /agent/agent.yaml
      # no KIMI_API_KEY required when OAuth home is mounted
    volumes:
      - kimi-home:/agent/kimi-home
      - kimi-workspaces:/agent/workspaces
      - kimi-runs:/agent/runs
```

Login is run once on the host (or a one-shot container) against the named
volume, then every replica mounts it.

## 6. mirarun seam (future)

Same shape as Codex's planned button
(`POST /codex-auth/login` …): one admin start/poll pair **per product home**,
served by a process that owns the shared volume. Not built yet; do not
invent per-agent login UIs.

## 7. Summary

| | Subscription OAuth | API key |
|---|---|---|
| Billing | plan / quota | metered |
| Interaction | one device/browser login | none |
| Who logs in | operator / mirarun, **once** | — |
| Agent containers | mount shared `*_home` | read env |
| miragen profile | `codex_home` / `kimi_home` / `grok_home` | same + key env |

Primary path for this architecture: **subscription OAuth into a shared home.**
