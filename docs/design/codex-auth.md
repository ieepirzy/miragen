# Codex authentication (issue #38)

**Status:** implemented (`miragen codex-login` CLI, shared-CODEX_HOME model);
mirarun "Login with ChatGPT" button is a documented seam, not yet built.

The migration to the `openai-codex` App Server SDK (Phase 1) unlocked
subscription-backed auth: a Codex executor can run on a ChatGPT plan instead
of metered API credits. That is the *point* of the migration, but it changes
the auth story from "inject an API key" to "hold a live OAuth session," and a
fleet of ephemeral agent containers must not each run a browser login. This
doc pins down how the login happens, where the credentials live, and how a
finished mirarun surfaces it as one button.

## 1. What auth actually is

The App Server reads its credentials from a **credential store** on disk —
`auth.json` under `CODEX_HOME` (default `/agent/codex-home`). Two ways to
populate it:

- **API key** (`CODEX_API_KEY` / `OPENAI_API_KEY`): metered, per-session,
  zero interaction. `_login()` calls `login_api_key(key)` when the env var is
  set. No OAuth, no browser. This is the CI / no-subscription path.
- **ChatGPT OAuth** (the subscription path): an interactive login writes a
  refreshable OAuth token into `auth.json`. No env var; the App Server just
  finds the file. This is what the SDK migration was *for*.

`prepare()` warns at startup when neither is present — an empty `CODEX_HOME`
and no key means every turn will fail auth.

## 2. The OAuth flow, and how it surfaces

The `openai-codex` SDK exposes two OAuth entry points on `Codex`:

- `login_chatgpt()` — spins up a **loopback HTTP listener** and opens (or
  prints) a `https://auth.openai.com/...` URL. The browser round-trips the
  token back to the listener. Great on a workstation with a browser;
  useless on a headless box where nothing can bind a browser to the
  container's loopback.
- `login_chatgpt_device_code()` — the **device-code** flow. Returns a
  `DeviceCodeLoginHandle(login_id, verification_url, user_code)`. You print
  the URL + code, the human opens it on *any* device, approves, and
  `handle.wait()` returns. `handle.cancel()` aborts. No inbound port, no
  browser on the host — the right primitive for headless/remote.

miragen standardizes on **device-code** for the helper because miragen runs
where there is no local browser (a remote container, a CI box, a mesh node).
The user sees:

```
Authenticating Codex into /agent/codex-home …

  Open:  https://auth.openai.com/device
  Code:  ABCD-1234

Waiting for you to approve in the browser (Ctrl-C to cancel) …
✓ Codex authenticated — credentials written to /agent/codex-home
```

Surfacing, by front end:

- **CLI today:** the URL + code are printed to the terminal (§3).
- **mirarun tomorrow:** a "Login with ChatGPT" button renders the same URL +
  code (or a QR of the URL) in the web UI (§5). Same device-code flow, just a
  nicer envelope. miragen owns the OAuth; mirarun owns the pixels.

## 3. The `miragen codex-login` helper

```
miragen codex-login [--codex-home /agent/codex-home]
```

Runs the device-code flow once and writes `auth.json` into the given
`CODEX_HOME` (default `/agent/codex-home`, env `CODEX_HOME`). Point
`--codex-home` at **the shared volume** every codex agent container mounts as
`executor.codex_home`. Re-run to refresh a stale/expired session. Ctrl-C
cancels the pending device grant cleanly.

It lazy-imports `openai_codex` and fails with an install hint if the `codex`
extra is absent, so the base CLI has no hard dependency on the SDK.

## 4. Authenticate once, not per container — the shared store

The load-bearing constraint (owner): *ideally you authenticate all sessions
at once, not once per agent container as they get spun up/down constantly.*

Agent containers are cattle. Running an interactive OAuth flow inside each one
as it spawns is both impossible (no human at spawn time) and wrong (N logins
for one identity). So:

> **Agent containers NEVER authenticate. They mount a shared, pre-populated
> `CODEX_HOME` read-mostly and reuse the one login.**

The lifecycle:

1. **Once**, out of band, an operator (or mirarun's button) runs the
   device-code flow against the shared `CODEX_HOME` volume → `auth.json`.
2. Every codex agent container mounts that same volume at
   `executor.codex_home`. `prepare()` finds `auth.json`, no per-container
   login.
3. Containers come and go freely; the credential outlives them.
4. When the OAuth token expires, re-run the login once against the shared
   volume; every container picks up the refreshed `auth.json`.

### The refresh / read-only nuance

OAuth access tokens expire and the SDK refreshes them by **rewriting
`auth.json`**. That collides with a strictly read-only mount:

- Mounting the shared store **read-only** is safest (containers can't corrupt
  the credential) but blocks in-container refresh — a token that expires
  mid-fleet-life goes stale until the operator re-logs in.
- Mounting **read-write** lets any container refresh in place, but now any
  container can rewrite the shared credential, and two containers refreshing
  concurrently can race on the file.

v1 keeps this an operational choice rather than baking in a broker: mount
read-only and re-run `codex-login` on expiry (simple, safe, one writer), or
mount read-write and accept the shared-writer semantics. A dedicated
**refresh broker** — one sidecar owns the write, containers read a token it
publishes — is the clean long-term answer and is deferred until the fleet is
big enough to need it. Flagged here so it's a known trade-off, not a surprise.

## 5. The mirarun "Login with ChatGPT" button (future seam)

Owner's vision: *once mirarun is finished you press a button there to
login/auth with ChatGPT, which is handled by miragen; mirarun just shows a
nice button.*

The device-code flow is already a two-step "start → wait" shape, which maps
cleanly onto an HTTP start/poll pair that miragen would expose and mirarun's
button would drive. **Not built yet** — documented so the contract is settled
before code:

```
POST /codex-auth/login        → { login_id, verification_url, user_code }
        # miragen calls login_chatgpt_device_code() against the shared
        # CODEX_HOME, holds the pending handle keyed by login_id, returns the
        # display fields. mirarun renders url + code (or a QR of the url).

GET  /codex-auth/login/{id}   → { status: "pending" | "authorized" | "error",
                                  detail? }
        # mirarun polls while the user approves on their phone. On
        # "authorized", auth.json is now written to the shared volume and the
        # whole fleet is logged in. mirarun swaps the button to a ✓.

DELETE /codex-auth/login/{id} → cancels the pending device grant (handle.cancel()).
```

Design invariants for whoever builds this:

- The endpoint runs **in the process that owns the shared `CODEX_HOME`**, so
  the resulting `auth.json` lands where the fleet reads it. It is *not* a
  per-agent-container endpoint.
- It is an **operator/admin** action (it changes fleet-wide identity), so it
  sits behind the same `MIRAGEN_INTERNAL_TOKEN` guard as the other control
  endpoints — never exposed unauthenticated.
- The pending handle is in-memory and single-flight per shared store; a second
  `POST` while one is pending returns the existing `login_id` rather than
  racing a second device grant.
- Same underlying `login_chatgpt_device_code()` the CLI uses — the button and
  the CLI are two front ends over one flow, so they can't drift.

## 6. Summary of the model

| | API key | ChatGPT OAuth |
|---|---|---|
| Credential | `CODEX_API_KEY` env | `auth.json` in `CODEX_HOME` |
| Billing | metered credits | subscription |
| Interaction | none | one device-code login |
| Who logs in | — | operator / mirarun button, **once** |
| Agent containers | read env | mount shared store read-mostly |
| Refresh | rotate the key | re-run `codex-login` (or RW mount / broker) |

The through-line: **one identity, one login, many ephemeral containers.**
Containers are consumers of a credential they never create.
