# Kimi Code + Grok Build executor backends

**Status:** accepted architecture (2026-07-25) — design commit first;
implementation sequenced as separate PRs (`kimi-code` → `grok-build` headless
→ optional MIT Grok client package / ACP).
**Companion:** [subscription-homes.md](subscription-homes.md) (shared auth
store model generalized from [codex-auth.md](codex-auth.md));
[executor-tier.md](../executor-tier.md) (shipped contract).

## 1. Purpose

Drive **self-harnessed** coding agents — Kimi Code and Grok Build — on the
same executor tier as Codex and Claude Code: **workspace-in /
diff-and-events-out**. miragen does not own their agent loops; it supplies
workspace, budget/timeout, event stream, resume, harvest, and optional host
leash.

The **primary auth path is subscription OAuth**, not metered API keys:

| Backend | Subscription path | Metered fallback |
|---|---|---|
| `codex` (shipped) | ChatGPT OAuth → `CODEX_HOME/auth.json` | `CODEX_API_KEY` / `OPENAI_API_KEY` |
| `kimi-code` | Kimi Code OAuth → `$KIMI_CODE_HOME` | Moonshot / Kimi API key env |
| `grok-build` | SuperGrok / X Premium+ OAuth → `$GROK_HOME/auth.json` | `XAI_API_KEY` |

Agent containers **never log in**. Operators (or mirarun) authenticate once
into a shared home volume; every container mounts it. Full model:
[subscription-homes.md](subscription-homes.md).

## 2. Does Grok force an SDK-like wrapper?

**Not for Phase 1. Yes as the clean long-term product shape.**

There is no first-party Grok agent SDK analogous to `openai-codex`,
`claude-agent-sdk`, or `kimi-agent-sdk`. Integration surfaces today:

| Surface | What it gives | Resume | Approvals / leash | Schema maturity |
|---|---|---|---|---|
| Headless CLI (`grok -p … --output-format streaming-json`) | One-shot turns, NDJSON | `-s` / `-r` / sessions under `$GROK_HOME/sessions` | `--always-approve` only (weak) | Event union poorly published |
| ACP (`grok agent stdio`) | JSON-RPC sessions | `sessionId` | Protocol-dependent; better long-term seam | Documented request/response shapes |
| Model API (`xai-sdk` / Responses) | Completions only | N/A | N/A | Wrong tier — not self-harnessed |

**Decision:**

1. **Phase 1 (in-tree adapter):** a dedicated `GrokBuildExecutor` inside
   miragen that shells the headless CLI (or a minimal in-tree ACP client).
   No separate package required to ship value.
2. **Phase 2 (optional MIT package):** extract a thin **client library** —
   not a second harness — that wraps headless + ACP the way
   `kimi-agent-sdk` wraps Kimi CLI / `openai-codex` wraps the App Server.
   License: **MIT**, open-sourced independently so other orchestrators can
   reuse it. miragen then depends on that package instead of keeping the
   transport private.
3. **Never** put Grok on the model tier (`spec`) for coding workers. The
   model API is for when *miragen* owns the loop; here Grok Build owns it.

Kimi does **not** need a miragen-owned SDK: Moonshot already ships
`kimi-agent-sdk` (Python/Node/Go), a thin client over the Kimi Code runtime.

## 3. Backend inventory after this work

| `executor.executor` | Package / binary | Resume handle | Usage | Host leash |
|---|---|---|---|---|
| `codex` | `openai-codex` | thread id | yes | `approval_handler` |
| `claude-code` | `claude-agent-sdk` | session id | yes | `can_use_tool` |
| `spawn` | argv template | none | none | none |
| `kimi-code` **(new)** | `kimi-agent-sdk` | session id | `StatusUpdate` / token usage | `ApprovalRequest.resolve` |
| `grok-build` **(new)** | Grok Build CLI (+ later MIT client) | session id | best-effort → ACP | P1 weak / P2 ACP |

The ABC contract is unchanged. Adapters implement `_stream_turn()` and yield
normalized payloads only.

## 4. Profile shape

Shared knobs stay as they are (`instructions`, `model`, `approval_policy`,
`mcp_servers`, `turn_timeout_s`, `leash`, `artifact_sink`, `workspace_root`).

**Home fields** (one per backend that owns a credential/config directory):

```yaml
executor:
  executor: codex | claude-code | kimi-code | grok-build | spawn
  # …
  codex_home: /agent/codex-home    # codex only → CODEX_HOME
  kimi_home: /agent/kimi-home      # kimi-code only → KIMI_CODE_HOME
  grok_home: /agent/grok-home      # grok-build only → GROK_HOME
```

Loud rejection (same philosophy as `command` / spawn): a home field set on
the wrong backend is a validation error. Defaults point at container paths
meant to be volume-mounted, not at the developer's laptop `~`.

### 4.1 Verified: `codex_home` vs the Codex SDK

Inspected `openai-codex` 0.144.4 (`CodexConfig` in `openai_codex/client.py`):

- There is **no** `codex_home=` constructor parameter on `CodexConfig`.
- The App Server process is spawned with `env = os.environ.copy()` plus
  optional `CodexConfig.env` overrides.
- `default_codex_home()` returns `~/.codex` — CLI convention, not a miragen
  invent.
- miragen correctly maps `ExecutorSpec.codex_home` → `os.environ["CODEX_HOME"]`
  in `prepare()` (and could equivalently pass
  `CodexConfig(env={"CODEX_HOME": …})`).

So `*_home` profile fields are **miragen bindings onto env vars each product
already documents**, not SDK-only inventions. Same pattern for Kimi
(`KIMI_CODE_HOME`, default `~/.kimi-code`) and Grok Build (`GROK_HOME`,
default `~/.grok`).

## 5. `kimi-code` adapter (PR1)

**File:** `miragen/executor/kimi_code.py`  
**Extra:** `miragen[kimi-code]` → `kimi-agent-sdk` (pulls `kimi-cli` runtime).

| Contract piece | Realization |
|---|---|
| workspace | `Session.create(work_dir=KaosPath(...))` |
| thread_id | `session.id`; resume via `Session.resume(work_dir, session_id=…)` |
| unattended | `yolo=True` when `approval_policy: never` and leash off |
| leash on | `yolo=False`; on each `ApprovalRequest` map → `GateOperation`, `gate_decide`, `resolve("approve"|"reject")` |
| events | Wire → normalize: `TextPart` → agent_message; `ToolCall` → tool_use; usage from `StatusUpdate` / turn end; errors → `turn.failed` |
| MCP | `mcp_configs=` from `spec.mcp_servers` (HTTP + bearer from env) |
| model | `model=` from `spec.model` |
| cancel | `session.cancel()` then `close()` on `CancelledError` |
| home | set `KIMI_CODE_HOME` from `kimi_home` in `prepare()` |
| auth warn | no OAuth store in home and no API key env → startup warning |

Test seam: injectable `session_factory` returning async iterators of already-
normalized dicts or stand-in Wire types (same pattern as Claude Code).

Profile sketch:

```yaml
name: kimi-worker
mode: interactive
triggers:
  - type: http
limits:
  tokens_per_run: 500000
executor:
  executor: kimi-code
  instructions: |
    You operate on the repository mounted in your workspace.
  model: null                 # product default when omitted
  approval_policy: never
  kimi_home: /agent/kimi-home
  turn_timeout_s: 1800
  mcp_servers:
    - name: loimi
      url: https://loimi.mesh/mcp/
      bearer_token_env: LOIMI_TOKEN
  leash:
    gate: [write, command, network]
```

Login helper (same once-for-the-fleet shape as `miragen codex-login`):

```
miragen kimi-login [--kimi-home /agent/kimi-home]
```

Preferred implementation: shell out to Kimi Code's documented device-code
login (`kimi login` / non-interactive device flow) against
`KIMI_CODE_HOME`, so we do not reimplement OAuth. Can land in the same PR as
the adapter or immediately after if the CLI flag surface needs a live check.

## 6. `grok-build` adapter (PR2+)

### Phase A — headless process adapter (ship value)

Dedicated adapter (not generic `spawn`):

```text
grok --no-auto-update
     -p <prompt>
     --cwd <workspace>
     --output-format streaming-json
     --always-approve          # when approval_policy=never and leash off
     -s <thread_id> | -r <thread_id>
     [-m <model>]
```

Env: `GROK_HOME=<grok_home>`, plus `XAI_API_KEY` only as metered fallback.
Process-group kill on cancel (spawn contract). NDJSON → best-effort
normalize; fixture-driven tests; usage nullable when absent.

**Leash:** weak under `--always-approve`. With leash on, either refuse to
start (loud) until ACP lands, or run without auto-approve only if a
documented non-interactive approval path exists (unlikely for headless).
Document the gap; do not pretend the host gate works.

### Phase B — ACP + MIT client (shipped)

In-repo MIT package `packages/grok-build-client` (import `grok_build_client`):
headless + ACP stdio transports. miragen's `GrokBuildExecutor` selects via
`grok_transport: headless | acp`. ACP supports host leash
(`session/request_permission`) and MCP on `session/new`.

Login helper:

```
miragen grok-login [--grok-home /agent/grok-home]
```

Mirror `codex-login`: operator runs once against the shared volume. Exact
headless OAuth mechanism (`grok login`, device-code if available, browser
print) verified at implementation time against the installed CLI.

### Phase C — locked-down headless Grok (tool allowlist + hermetic home)

For a persistent agent that must reason with Grok but act **only** through
an explicit set of MCP servers (no shell, files, web or subagents), with the
boundary enforced by grok's own permission/policy layers rather than by the
prompt. Verified against grok 1.0.41 (`~/.grok/docs/user-guide/`
05, 07, 09, 10, 14, 16, 22, 26 and `grok inspect`).

```yaml
executor:
  executor: grok-build
  instructions: "You are Mira. Act only through the tools you are given."
  grok_home: /agent/grok-home-mira      # dedicated: hermetic owns this home
  grok_auth: subscription               # XAI_API_KEY never reaches grok
  grok_hermetic: true                   # miragen owns config.toml + requirements.toml
  mcp_servers:
    - name: loimi
      url: https://memory.example/mcp
      bearer_token_env: LOIMI_TOKEN     # per-run secret or container env; never on disk
  web_search: false                     # -> --disable-web-search
  grok_tools: [search_tool, use_tool]   # --tools: the MCP dispatchers only
  grok_disallowed_tools: [Agent]        # --disallowed-tools: no subagents
  grok_permission_mode: dontAsk         # --permission-mode (replaces --always-approve)
  grok_allow:                           # --allow, one per permitted MCP tool
    - MCPTool(loimi__memory_recall)
    - MCPTool(loimi__memory_remember)
  grok_deny: [Bash, Read, Edit, Write, Grep, WebFetch, WebSearch]
  grok_max_turns: 12                    # --max-turns
```

What each layer does, and why none of them is enough alone:

| Layer | Mechanism | Gap it closes |
|---|---|---|
| `grok_tools` | `--tools` built-in allowlist | Removes shell/file/web tools from the model's toolset. `search_tool`/`use_tool` are "always-on MCP meta-tools" (14-headless-mode.md) |
| `grok_disallowed_tools: [Agent]` + hermetic `subagents.enabled = false` | tool removal + pinned policy | Subagents would otherwise inherit the parent's MCP servers |
| `grok_permission_mode: dontAsk` + `grok_allow` | deny everything not pre-approved | **Not an allowlist on its own:** grok auto-approves `read_file`, `grep`, `list_dir`, `web_search`, `todo_write` and skills in every mode (22-permissions-and-safety.md), hence `grok_tools` + `grok_deny` |
| `grok_deny` | deny rules win over everything, including always-approve | Belt and braces for the read-only auto-approvals |
| `grok_hermetic` config.toml | compat cells off, subagents/memory/managed MCPs/remote managed config/leader/trace upload off; ONLY the spec's `[mcp_servers.*]`, `Authorization = "Bearer ${ENV}"` | Grok merges MCP/skills/rules/agents/hooks from `~/.claude.json`, `~/.claude/`, `~/.cursor/` by default |
| `grok_hermetic` requirements.toml | `fail_closed = true`, `allow_managed_mcp_servers_only`, exact-URL `[[allowed_mcp_servers]]`, `enable_all_project_mcp_servers = false`, `allow_managed_hooks_only`, and the same switches as policy | A workspace `.mcp.json` or `.grok/config.toml` still adds servers (compat cells do not cover them); policy keys in `config.toml` are **ignored**, and only `requirements.toml` enforces them |
| isolated `HOME` (`<grok_home>/hermetic-home`) | grok process env | Compat cells do **not** stop Claude *plugins* (`~/.claude/plugins`: skills, command hooks, stdio MCP servers) or `~/.agents/skills` |
| GROK_* scrub | grok process env keeps only `GROK_HOME` (plus `CLAUDE_CONFIG_DIR`/`CODEX_HOME`/`CURSOR_CONFIG_DIR` dropped) | `GROK_CLAUDE_MCPS_ENABLED=true` overrides even a `requirements.toml` compat cell (cells are not `pin` keys); `GROK_CONFIG`/`GROK_CONFIG_PATH` are config overlays |
| `grok_auth: subscription` | `XAI_API_KEY`/`GROK_CODE_XAI_API_KEY` removed from the grok env (headless and ACP) | Grok's auth precedence falls back to the metered key silently when the session token is missing/expired |

`grok inspect --json` with the rendered home (real `$HOME` full of Claude
plugins, a workspace `.mcp.json` and `.grok/config.toml`, and
`GROK_CLAUDE_MCPS_ENABLED=true` in the env) reported: 0 skills, 0 hooks,
0 plugins, 0 project instructions; MCP servers = the declared one, with the
workspace servers `disabledReason: not in allowedMcpServers`;
`mcpManagedServersOnly: enforced`; `nonManagedHooks` and
`projectMcpServers` enforced off. Without the isolated HOME the same
compat-off config still loaded 43 skills, 5 plugins, 3 plugin hooks and 3
plugin MCP servers.

`$GROK_HOME/requirements.toml` is grok's **signed managed-policy cache**
(26-config-reference.md). A headless run with debug logging showed that
grok's managed-config store deletes an unsigned file at bootstrap ("removed
managed config file") when no team principal serves one. That happens before
the config pins are resolved: the MCP allowlist still applied to that one
run, but the pins did not, and every later run had no policy at all. With
`fail_closed = true` the log says "keeping fail_closed managed policy on
disk; no team principal present to own a clear", followed by "deployment
requirements enforced". A server not on the list is logged as `MCP server
blocked by managed settings policy … not in allowedMcpServers`. All of this
was observed without auth. Whether a **signed-in** subscription account's
online managed-config sync keeps the file, or refuses to start because it is
unsigned, has **not been verified yet**. The live probe must check it.
Separately, headless grok already skips workspace `.mcp.json` servers in an
untrusted folder ("folder untrusted: skipping repo-local (project-scoped) MCP
server"). A stdio `.mcp.json` server in the workspace was not launched.

Ownership rules (`miragen/executor/grok_hermetic.py`):

- `config.toml` and `requirements.toml` are rewritten atomically
  (tmp + `os.replace`) at start **and before every headless turn**,
  because grok appends state to `config.toml` at runtime and may prune
  `requirements.toml`. Ownership is recorded in a marker file
  (`.miragen-hermetic-owner`), not a TOML comment.
- Everything else in GROK_HOME (`auth.json`, `sessions/`, `logs/`) is
  never touched.
- A home owned by another agent, or a `requirements.toml` miragen did not
  write (possibly an organisation's synced policy), makes `prepare()` fail
  loudly. A pre-existing `config.toml` is kept once as
  `config.toml.pre-hermetic` and restored when `grok_hermetic` is turned
  off (which also removes the policy file and marker).
- `grok_hermetic` requires a dedicated `grok_home`: its policy pins clamp
  every grok process using that home. Log in once into that home with
  `miragen grok-login --grok-home <it>`.

Validation (profiles fail loudly on dead config): every new field is
grok-build only; all but `grok_auth` require `grok_transport: headless`;
headless `mcp_servers` now require `grok_hermetic: true` (still rejected
without it — no silent drop); hermetic `bearer_token_env` must be a plain
env var name and server URLs must be literal (they are the allowlist
entries); `web_search: true` with a `grok_tools` list that omits
`web_search`/`web_fetch` is rejected.

Not covered here (see the live-probe notes in the PR): whether
`use_tool`'s file-backed argument forms (`tool_input_file`, `{"file": …}`,
07-mcp-servers.md) let the model read local files into an allowed MCP call;
and retention — grok keeps full transcripts under
`$GROK_HOME/sessions/` (the `storage.cleanup_ttl_days` key prunes them).

### Optional MIT package (`grok-build-sdk` working name)

Scope if extracted:

- Spawn/manage headless + ACP transports
- Session create/resume
- Typed or documented event stream
- Auth detection (home `auth.json` vs `XAI_API_KEY`)
- **Not** workspace harvest, budgets, or miragen run records

License: MIT. miragen depends on it optionally (`miragen[grok-build]`).
**Not a blocker for Phase A.**

## 7. Factory + extras

```python
# miragen/executor/__init__.py — build_executor
if kind == "kimi-code":
    from miragen.executor.kimi_code import KimiCodeExecutor
    return KimiCodeExecutor(...)
if kind == "grok-build":
    from miragen.executor.grok_build import GrokBuildExecutor
    return GrokBuildExecutor(...)
```

```toml
# pyproject.toml
kimi-code = ["kimi-agent-sdk>=0.0.5"]
# grok-build: CLI binary in the image; optional MIT client later
```

## 8. Sequencing

1. **This design commit** — architecture + subscription-homes generalization.
2. **PR1 — `kimi-code`:** adapter, extra, validators, tests, executor-tier
   docs, optional `kimi-login`.
3. **PR2 — `grok-build` Phase A:** headless adapter, `grok_home`, validators,
   fixtures, auth docs, optional `grok-login`.
4. **PR3 — Grok ACP and/or MIT client** — when leash parity or multi-consumer
   reuse justifies the extract.

## 9. Non-goals

- Model-tier Grok/Kimi coding workers (wrong tier).
- Replacing Codex/Claude adapters.
- Lomitus / cross-run coordination (still above this tier).
- Making the artifact sink required.
- Publishing a miragen-owned *Kimi* SDK (upstream exists).

## 10. Decisions (owner, 2026-07-25)

1. **Grok MIT package** — extract **only later** (after in-tree headless and
   preferably ACP). No parallel package repo for Phase A.
2. **Login helpers** — ship with each backend PR (`kimi-login` in PR1;
   `grok-login` with grok-build). Subscription story stays complete.
3. **Default models** — leave `model: null` (= product default) for now;
   examples do not pin IDs.
4. **Image baking** — executor extras land in the published Docker image.
   CI: `test.yml` on PR/main; `publish.yml` on main + `v*` tags builds
   `Dockerfile` → `ghcr.io/ieepirzy/miragen` (semver + `latest` on default
   branch). `Dockerfile` installs `miragen[codex,claude-code,kimi-code]`.
5. **Tier rename** — **out of scope** for this workstream.
6. **Shared home RW vs RO** — same operational choice as Codex (see
   subscription-homes); no broker in v1.

## 11. Acceptance criteria (per backend)

- Profile validates; wrong `*_home` / `command` combinations fail loud.
- Fresh turn + resume reuse workspace and thread/session id.
- `turn_timeout_s` cancel does not leak processes/sessions.
- Budget suspend still works when usage is reported; when not, wall-clock is
  the guard (nullable usage, never fake zero).
- Subscription path: mounted home with OAuth creds, no API key, turn can
  start (live smoke optional; unit tests mock the SDK/CLI).
- Events.jsonl carries envelope + normalized types the base parser already
  understands.
- Diff harvest still exactly once on terminal success.
