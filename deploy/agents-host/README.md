# Agents host: bring-up runbook

This stack runs MiraRun's engineer lanes (mirarun ADR-041) on a host of their own: `miragend` plus `lanes-gateway`, with every agent on the `miragen-engineer` image. The host is `agents-hel1-1`, Portainer environment 10, built by movingfirm-infra `agents-host/`. MiraRun stays on the main VPS. Its side is in mirarun `docs/runbook-agents-host.md`.

```
MiraRun (VPS) ──WG──▶ :8000 miragend ──docker.sock──▶ lane containers
MiraRun (VPS) ──WG──▶ :8001 lanes-gateway /a/<agent>/… ──lanes-control──▶ <agent>:8000
lane container ──lanes-broker──▶ github-broker:8080 (= lanes-gateway) ──WG──▶ MiraRun broker
lane container ──lanes-egress──▶ internet (Anthropic, design.muutto365.fi, memory.muutto365.fi)
```

## What protects what

- **Lifecycle API.** It is published on the WireGuard address only. Docker-published ports bypass ufw, so the bind address is the guard. It needs `MIRAGEND_TOKEN` (from a file).
- **Agent API.** `lanes-gateway` serves it on the same address. It accepts only `MIRARUN_SOURCE_ADDRESS` and proxies only names starting with `LANE_AGENT_PREFIX`. Each agent still checks `X-Miragen-Token`.
- **Agent networks.** Agents join `lanes-control` (internal), `lanes-egress` (internet, no container-to-container traffic) and `lanes-broker` (internal; its only other member is the gateway). They are never on `miragen-net` or the stack's default network.
- **Host egress guard.** `AGENTS-EGRESS` in cloud-init still refuses every container's new connections into the mesh, `10.0.0.0/16` and the metadata service. The one exception is the broker line in `/etc/agents-egress-allow`.
- **Credentials.**
  - Secrets live as files in `AGENTS_HOST_SECRETS_DIR` and reach miragend as `*_FILE`.
  - An agent gets one only through its profile: `credentials:` grants, or the subscription token for `claude-code:` models.
  - The engineer entrypoint writes each grant to `/run/engineer/secrets/` and removes it from the environment.
  - There is no GitHub token anywhere: git and gh go through the broker.
- **Audit.**
  - `miragend-state:/audit/miragend.jsonl` records every lifecycle action with its outcome, and every container's credential grants by name.
  - `gateway-logs:/agents.jsonl` records every MiraRun call to an agent, turns included.
  - `gateway-logs:/broker.jsonl` records every broker connection.
  - MiraRun and the broker keep their own audit records.

**Accepted for v1, not solved here.**
- Instances in one lane container share a uid, so they can read each other's environment (miragen#198, "Isolation").
- Agents on `lanes-control` can reach each other's API with the shared agent token.
- The token values miragend forwards are written into `<MIRAGEN_WORKSPACE>/compose.yml` on the host (root/uid 1000 only), as on every miragend host.

## Ilari steps, in order

1. **WireGuard.** Set up the point-to-point link between the agents host and the VPS (mirarun#180), then revoke the mesh peer in wg-easy. Note both link addresses:
   - `AGENTS_WG`: this host's address;
   - `VPS_WG`: the VPS end.
2. **Claude token.** Run `claude setup-token` with the subscription the lanes should use (mirarun#180's open decision). Put the token in the secrets file below, never in stack.env.
3. **Secrets directory** on the agents host, for example `/opt/agents-host-secrets`. Each file has mode `0400` and owner `1000:1000` (miragend's unprivileged user):

   | File | Content |
   |---|---|
   | `miragend_token` | New random token; MiraRun's `MIRARUN_MIRAGEND_TOKEN` |
   | `miragen_internal_token` | Must equal MiraRun's `MIRARUN_MIRAGEN_TOKEN` (see the mirarun runbook's limitation) |
   | `claude_code_oauth_token` | From step 2 |
   | `miradesign_token` | A MiraDesign bearer valid for the plugin bundle, hooks and MCP (`MIRADESIGN_MCP_TOKENS`) |
   | `miragen_bridge_token` | The memory bridge's bearer (memory.muutto365.fi) |
   | `gh_broker_<lane>` | One per lane: that lane's broker credential, minted by MiraRun |

   For each lane, also add `GH_BROKER_<LANE>_API_KEY_FILE=/run/agents-host-secrets/gh_broker_<lane>` to stack.env. The lane's profile then lists `GH_BROKER_<LANE>_API_KEY` under `credentials:`.
4. **Egress guard.**
   - Add `<VPS_WG> <MIRARUN_BROKER_AGENT_PUBLISH_PORT>` to `/etc/agents-egress-allow`.
   - **If the point-to-point link is outside `10.8.0.0/24`, extend the guard to cover it.** Today `AGENTS-EGRESS` rejects only the mesh, `10.0.0.0/16` and the metadata address. Without the extension, lane containers could open connections to anything listening on the VPS end of the new link. Add `iptables -A AGENTS-EGRESS -d <link subnet or VPS_WG/32> -m conntrack --ctstate NEW -j REJECT` to `/usr/local/sbin/agents-egress-guard`, after the allowlist loop, so the broker's `RETURN` stays first. Carry the same rule into movingfirm-infra `agents-host/cloud-init.yaml`.
   - Run `systemctl restart agents-egress-guard`.
5. **Portainer stack** on environment 10, from this repository, with compose path `deploy/agents-host/compose.yaml`. Its stack.env holds names and addresses only:

   | Variable | Value |
   |---|---|
   | `AGENTS_HOST_BIND_ADDRESS` | `AGENTS_WG` |
   | `MIRARUN_SOURCE_ADDRESS` | `VPS_WG` |
   | `BROKER_UPSTREAM` | `VPS_WG:<broker agent port>` |
   | `AGENTS_HOST_SECRETS_DIR` | `/opt/agents-host-secrets` |
   | `ENGINEER_IMAGE` | `ghcr.io/ieepirzy/miragen-engineer@sha256:…`, pinned (publish.yml prints the digest) |
   | `MIRAGEND_IMAGE` | The miragend image from the same commit (`sha-…` or digest) |
   | `MIRADESIGN_URL` | `https://design.muutto365.fi` |
   | `MIRAGEN_MEMORY_URL` | `https://memory.muutto365.fi` |
   | `ENGINEER_EXTRA_CA_B64` | `base64 -w0 broker-ca.pem`, only if the broker's certificate is from a private CA |
   | `GH_BROKER_<LANE>_API_KEY_FILE` | `…`, one per lane |

   Optional: `MIRAGEND_MAX_AGENTS` (4), `MIRAGEND_AGENT_MEM_LIMIT` (3g), `MIRAGEND_AGENT_CPU_LIMIT` (2), `GATEWAY_PUBLISH_PORT` (8001) and `LANE_AGENT_PREFIX` (`lane-`).
6. **MiraRun (stack 52).** Set the broker agent listener and miragend variables from mirarun `docs/runbook-agents-host.md`. Read its single-daemon limitation first.
7. **Create the lane's managed agent** through MiraRun. Use a profile like `examples/profiles/lane-engineer-agents-host.yaml`, named `lane-<key>`.

## Live check

Run these once steps 1–7 are done. Each line is something to see, not assume.

1. From the VPS, `curl -s http://AGENTS_WG:8000/health` returns `"service":"miragend"`, and `GET /agents` without the bearer returns 401. **If it does not return 401, stop the stack at once.** The usual cause is a `miragend_token` file that uid 1000 cannot read: the loader then logs an error, and the daemon serves unguarded on the WireGuard address.
2. From any other mesh peer, ports 8000 and 8001 on `AGENTS_WG` do not answer (the link is point-to-point).
3. `curl http://AGENTS_WG:8001/healthz` returns `ok` from the VPS, and 403 from anywhere else.
4. MiraRun's agent list shows the lane agent. Its `endpoint` is `http://AGENTS_WG:8001/a/lane-<key>`, and `GET <endpoint>/health` from the VPS returns 200.
5. On the agents host, `docker inspect lane-<key>` shows networks `lanes-control`, `lanes-egress` and `lanes-broker` only, and `docker exec lane-<key> env` shows no `*_API_KEY`, `GH_TOKEN` or `GITHUB_TOKEN`.
6. `docker exec lane-<key> cat /run/engineer/status.json` shows both plugins `installed: true` and `miradesign.source: daemon`.
7. Inside the container, as agentuser:
   - `curl -m5 http://10.8.0.4` fails (egress guard);
   - a VPS port on the point-to-point address other than the broker's is refused, and the broker's port connects;
   - `curl -m5 https://api.anthropic.com` connects (egress open);
   - `curl -m5 http://miragend:8000/agents` answers 401 (miragend is reachable on `lanes-control`; its token guards it).
8. A first lane session: MiraRun starts it, and MiraDesign shows the actor `agent:claude-code/lane-<key>-engineer-1` registered. The supervisor's turn appears in `gateway-logs/agents.jsonl` with a 200.
9. The agent's `git fetch` works through the broker. `broker.jsonl` shows the connection, and the broker's audit shows the call under the lane's credential. A push to a non-`agent/<lane>/…` ref is refused by the broker.
10. `miragend.jsonl` has `agent.create` and `agent.start` succeeded, and `agent.credentials.granted` listing exactly the profile's grants plus `CLAUDE_CODE_OAUTH_TOKEN` and `MIRAGEN_INTERNAL_TOKEN`, with no values.
11. Hook enforcement, verified live for the first time:
    - a subagent beyond `subagent_depth` is refused;
    - `ask_human` posts a question that reaches the attention queue;
    - MiraDesign's idle wake starts no turn on its own (`wake_idle_sessions` is off).
12. Restart the agent container. Its worktree and branch survive, and the plugins come back from the cached bundle even with MiraDesign briefly unreachable.
