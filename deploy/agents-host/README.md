# Agents host: bring-up runbook

This stack runs MiraRun's engineer lanes (mirarun ADR-041) on a host of their own: `miragend` plus `lanes-gateway`, with every agent on the `miragen-engineer` image. The host is `agents-hel1-1`, Portainer environment 10, built by movingfirm-infra `agents-host/`. MiraRun stays on the main VPS and reaches this host through its lane-only daemon settings (mirarun#190). Its side is in mirarun `docs/runbook-agents-host.md`.

```
MiraRun (VPS) ──WG──▶ :8000 miragend ──docker.sock──▶ lane containers
MiraRun (VPS) ──WG──▶ :8001 lanes-gateway /a/<lane-…>/… ──miragen-agent-<name>──▶ <agent>:8000
lane container ──miragen-agent-<name>──▶ github-broker:8080 (= lanes-gateway) ──WG──▶ MiraRun broker
lane container ──lanes-egress──▶ internet (Anthropic, design.muutto365.fi, memory.muutto365.fi)
```

## What protects what

The stack runs miragend with `MIRAGEND_ISOLATED_AGENTS=on`.

**Who can reach what**
- **miragend** is on the stack's default network only. No agent is on that network.
  - Its API is published on the WireGuard address only. Docker-published ports bypass ufw, so the bind address is the guard, together with the host rules in step 4.
  - It needs `MIRAGEND_TOKEN` (from a file).
  - It serves neither the run-control contract nor schedules. Those shared-token routes would let an agent run another agent's profile.
- **Each agent** gets its own internal network, `miragen-agent-<name>` (host bridge `lna…`). The only other member is `lanes-gateway`, which miragend re-attaches on every daemon and agent start.
  - No agent shares a network with miragend or with another agent.
  - `lanes-egress` is the agents' internet route. It is `lna-egress` on the host, with container-to-container traffic off.
- **`lanes-gateway`**:
  - accepts only `MIRARUN_SOURCE_ADDRESS` on `:8001` (agents get a 403 there). That address is the VPS **host**, so it admits any VPS container whose traffic leaves through it. The VPS-side rule in step 6 narrows it to MiraRun's own services;
  - routes `/a/lane-<name>/…` on the raw request URI;
  - is `github-broker` on each agent's network, forwarding to MiraRun's broker over WireGuard.
- **Host egress guard** (`AGENTS-EGRESS`, step 4): lane bridges (`lna+`) can't open connections to the mesh, `10.0.0.0/16`, the metadata service, the host itself (INPUT), or any port published on the WireGuard address (DNAT, `--ctorigdst`). The one exception is the broker line in `/etc/agents-egress-allow`.

**Credentials**
- **Agent tokens.** Each agent's `MIRAGEN_INTERNAL_TOKEN` is HMAC-SHA256(master, `miragen-agent-token/v1\0<name>`). The master is this stack's `miragen_internal_token`, which equals MiraRun's `MIRARUN_LANES_MIRAGEN_TOKEN`. MiraRun derives the same value per lane agent, and no agent ever holds the master. One agent's token is worth nothing against another.
- **Delivery as files.** miragend writes each granted credential (agent token, subscription token, plugin tokens, broker credential) to `<workspace>/agent-secrets/<name>/` and mounts it read-only at `/run/miragen-secrets`. The container's environment holds only `*_FILE` paths, so `docker inspect`, `compose.yml` and `/proc/1/environ` show no values.
  - The engineer entrypoint turns any remaining credential value into a file before `miragen run` starts.
  - Inside the container, the files are readable by the agent (uid 10000, with miragend's gid as a supplementary group; files 0440, directory 0750). That is the agent's own credential set, including its own token. So an agent can call its **own** guarded API, which gives it nothing it doesn't already have.
- **No GitHub token anywhere.** git and gh go through the broker.

**Container hardening and config hygiene**
- **Hardening.** Every agent runs with no-new-privileges, every capability dropped, a pid limit (`MIRAGEND_AGENT_PIDS_LIMIT`) and a read-only root filesystem (tmpfs at `/tmp` and `/run/engineer`). The image has no setuid or setgid binaries.
- **Claude config rebuilt on every boot.** The entrypoint deletes everything a session could persist under the agent's Claude config: settings, hooks, `env`, `apiKeyHelper`, plugins, user MCP servers, `CLAUDE.md`. It keeps `projects/` and miragen's harness files, then reinstalls the plugins.
  - The MiraDesign plugin is fetched over https with no redirects followed into a container-local directory.
  - If MiraDesign is unreachable at start, the container stops and retries (fail closed). It never falls back to a copy on the volume.

**Audit**
- `miragend-state:/audit/miragend.jsonl` records:
  - every lifecycle action and outcome;
  - every container's credential grants, by name;
  - agent file reads and writes, logs reads and exports.
- `gateway-logs:/agents.jsonl` records every MiraRun call to an agent, turns included.
- `gateway-logs:/broker.jsonl` records every broker connection.
- **Audit writes are fail-open.** A record that can't be written is logged as an error and the action still completes, because failing an action that already happened would misreport it. Watch for `audit: could not write` in miragend's log.

**Accepted for v1**
- Instances in one lane container share a uid (miragen#198, "Isolation").
- **Uids:** agents run as uid 10000, never host uid 1000 (`ila`, sudo and docker). miragend still runs as uid 1000 (its image), so its workspace files are 1000-owned on the host.
- **Never run git (or any tool that reads repository config) in an agent's directory on the host,** as `ila` or root. A lane writes its own hooks and config there. Inspect with `cat`/`less`, or copy the files out first.
- Egress to the internet is open.

## Ilari steps, in order

1. **WireGuard.** Set up the point-to-point link between the agents host and the VPS (mirarun#180), then revoke the mesh peer in wg-easy. Note both link addresses:
   - `AGENTS_WG`: this host's address;
   - `VPS_WG`: the VPS end.
2. **Claude token.** Run `claude setup-token` with the subscription the lanes should use (mirarun#180's open decision). Put the token in the secrets file below, never in stack.env.
3. **Secrets directory** on the agents host, for example `/opt/agents-host-secrets`. Each file has mode `0400` and owner `1000:1000` (miragend's unprivileged user). Every token here is **new**; none is copied from the VPS.

   | File | Content |
   |---|---|
   | `miragend_token` | New random token. MiraRun's `MIRARUN_LANES_MIRAGEND_TOKEN` |
   | `miragen_internal_token` | New random token, the lanes **master**. MiraRun's `MIRARUN_LANES_MIRAGEN_TOKEN`. Must differ from `MIRARUN_MIRAGEN_TOKEN`; MiraRun refuses to start otherwise |
   | `claude_code_oauth_token` | From step 2 |
   | `miradesign_token` | A MiraDesign bearer valid for the plugin bundle, hooks and MCP (`MIRADESIGN_MCP_TOKENS`) |
   | `miragen_bridge_token` | The memory bridge's bearer (memory.muutto365.fi) |
   | `gh_broker_<lane>` | One per lane: that lane's broker credential, minted by MiraRun |

   For each lane, also add `GH_BROKER_<LANE>_API_KEY_FILE=/run/agents-host-secrets/gh_broker_<lane>` to stack.env. The lane's profile then lists `GH_BROKER_<LANE>_API_KEY` under `credentials:`.
4. **Host egress guard.** Edit `/usr/local/sbin/agents-egress-guard`. The cloud-init change is movingfirm-infra#38, on top of #37.
   - **Allowlist:** add `<VPS_WG> <MIRARUN_BROKER_AGENT_PUBLISH_PORT>` to `/etc/agents-egress-allow`. Allowlist lines stay first.
   - **Reject the link:** if the point-to-point link is outside `10.8.0.0/24`, add a REJECT for its subnet (or `VPS_WG/32`).
   - **Reject from lane bridges** (`lna+`), all `--ctstate NEW`:
     - in `DOCKER-USER`, `-m conntrack --ctorigdst` for the mesh, the link and `AGENTS_WG/32`, which covers published ports hairpinned by DNAT;
     - in `INPUT`, everything, which covers sshd, the Portainer agent and the bridge gateway IPs.
   - Then run `systemctl restart agents-egress-guard`.
5. **Portainer stack** on environment 10, from this repository, with compose path `deploy/agents-host/compose.yaml`. Its stack.env holds names and addresses only:

   | Variable | Value |
   |---|---|
   | `AGENTS_HOST_BIND_ADDRESS` | `AGENTS_WG` |
   | `MIRARUN_SOURCE_ADDRESS` | `VPS_WG` |
   | `BROKER_UPSTREAM` | `VPS_WG:<broker agent port>` |
   | `AGENTS_HOST_SECRETS_DIR` | `/opt/agents-host-secrets` |
   | `ENGINEER_IMAGE` | `ghcr.io/ieepirzy/miragen-engineer@sha256:…` (publish.yml prints the digest) |
   | `MIRAGEND_IMAGE` | `ghcr.io/ieepirzy/miragend@sha256:…` from the same commit |
   | `MIRADESIGN_URL` | `https://design.muutto365.fi` |
   | `MIRAGEN_MEMORY_URL` | `https://memory.muutto365.fi` |
   | `ENGINEER_EXTRA_CA_B64` | `base64 -w0 broker-ca.pem`, only if the broker's certificate is from a private CA |
   | `GH_BROKER_<LANE>_API_KEY_FILE` | `…`, one per lane |

   Optional: `MIRAGEND_MAX_AGENTS` (4), `MIRAGEND_AGENT_MEM_LIMIT` (3g), `MIRAGEND_AGENT_CPU_LIMIT` (2), `MIRAGEND_AGENT_PIDS_LIMIT` (512) and `GATEWAY_PUBLISH_PORT` (8001).
6. **MiraRun (stack 52).** Set the broker agent listener (#188) and the `MIRARUN_LANES_MIRAGEND_*` variables (#190) from mirarun `docs/runbook-agents-host.md`. Leave `MIRARUN_MIRAGEND_URL` and `MIRARUN_MIRAGEN_TOKEN` as they are.
   - **VPS-side rule.** On the VPS, only MiraRun's `web`, `dispatcher` and `projector` containers may open connections to `AGENTS_WG`. Any other VPS container would otherwise pass the gateway's `allow` as the VPS host. In the VPS's `DOCKER-USER` chain, insert: ACCEPT from those containers' addresses (or their bridge's subnet, if it holds only them) to `AGENTS_WG` ports 8000 and 8001, then REJECT `-d AGENTS_WG/32 --ctstate NEW` from every other container. Persist it the way the VPS persists its other DOCKER-USER rules.
7. **Create the lane's managed agent** through MiraRun. Use a profile like `examples/profiles/lane-engineer-agents-host.yaml`, named `lane-<key>`. MiraRun routes only `lane-*` names to this host.

## Live check

Run these once steps 1–7 are done. Each line is something to see, not assume. `L` is a running lane container. `L2` is a second one; create a throwaway `lane-probe` if there is only one.

1. **miragend responds and is guarded.** From the VPS, `curl -s http://AGENTS_WG:8000/health` returns `"service":"miragend"`, and its capabilities include neither `schedules/v1` nor `executor-launch/v1`. `GET /agents` without the bearer returns 401. **If it does not return 401, stop the stack at once.** The usual cause is a `miragend_token` file that uid 1000 cannot read: the loader then logs an error, and the daemon serves unguarded on the WireGuard address.
2. From any other mesh peer, ports 8000 and 8001 on `AGENTS_WG` don't answer (the link is point-to-point).
3. `curl http://AGENTS_WG:8001/healthz` returns `ok` from the VPS, and 403 from anywhere else.
4. MiraRun's agent list shows the lane agent. Its `endpoint` is `http://AGENTS_WG:8001/a/lane-<key>`, and a turn from MiraRun succeeds.
5. **No secret values in the unit.**
   - `docker inspect L --format '{{json .Config.Env}}'` shows `*_FILE` paths and no token value.
   - `grep -c` of each token in `<MIRAGEN_WORKSPACE>/compose.yml` returns 0.
   - `docker exec L sh -c 'tr "\0" "\n" </proc/1/environ'` shows no value.
   - `docker exec L cat /run/miragen-secrets/miragen_internal_token` is **not** the master token.
6. `docker exec L cat /run/engineer/status.json` shows both plugins `installed: true`, `miradesign.source: daemon`, and a `config_reset` list.
7. **Isolation, each one tested by name, by IP and by published port.** From `docker exec L` (curl `-m5`), each of these must fail or be refused:
   - **miragend:** `miragend:8000` (no DNS), its container IP `:8000`, and `AGENTS_WG:8000`.
   - **Another lane:** `L2` by name and by its container IP on `:8000`.
   - **The gateway's agent port:** `github-broker:8001/a/lane-<L2>/health` and `AGENTS_WG:8001/...` return **403**.
   - **The host:** sshd on `AGENTS_WG:22`; the bridge gateway IPs `:22` (from `ip route`); the Portainer agent at `AGENTS_WG:9001`.
   - **The mesh and metadata:** `10.8.0.4` (any port), `169.254.169.254`, and the VPS end of the link on any port except the broker's.

   Each of these must succeed:
   - `curl -m5 https://api.anthropic.com` connects;
   - `github-broker:8080` connects (TLS to the broker).
8. **Gateway recreate.** Recreate `lanes-gateway` (redeploy the stack, or `docker rm -f lanes-gateway` and up), and touch nothing else. Within `MIRAGEND_PEER_RECONCILE_S` (30 s), miragend re-attaches it: `docker network inspect miragen-agent-<name>` lists `lanes-gateway` with alias `github-broker`, and turns work again. An agent restart (`restart` or `start`) also re-attaches it at once.
9. **First lane session.** MiraRun starts it, and MiraDesign shows the actor `agent:claude-code/lane-<key>-engineer-1`. The supervisor's turn appears in `gateway-logs/agents.jsonl` with a 200.
10. **Git through the broker.** The agent's `git fetch` works through the broker. `broker.jsonl` shows the connection, and the broker's audit shows the call under the lane's credential. A push to a ref outside `agent/<lane>/…` is refused.
11. **Audit.** `miragend.jsonl` has `agent.create` and `agent.start` succeeded, plus `agent.credentials.granted` listing the profile's grants as `secret_files` names, with no values.
12. **Hooks, verified live for the first time:**
    - a subagent beyond `subagent_depth` is refused;
    - `ask_human` posts a question that reaches the attention queue;
    - MiraDesign's idle wake starts no turn on its own.
13. **Tampering doesn't survive a restart.** From inside `L`, plant all of these:
    - a hook in `/agent/claude-home/settings.json`;
    - a `~/.bashrc` line under `/agent/claude-home/home/`;
    - a `projects/<p>/memory/MEMORY.md`;
    - an `mcpServers` entry in `/agent/claude-home/.claude.json`;
    - in each shared clone `/agent/claude-home/repos/*.git`: a `hooks/post-checkout`, plus `core.hooksPath`, `core.fsmonitor` and an `alias` in its `config`.

    Restart `L`. Every one of them is gone (`status.json` lists them under `config_reset`), and the worktree and its branch survive. Inside a session, `git config --get core.hooksPath` prints `/dev/null` (settings `env`). With MiraDesign unreachable, the container stops instead of starting without the plugin.
