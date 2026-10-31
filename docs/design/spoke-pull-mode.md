# Spoke pull mode: miragend as a cluster's provisioner

**Status:** built and tested against fakes. It has not run on a cluster.
It implements item 3 of the first slice in MiraRun ADR-045, "Agent workloads
on Kubernetes clusters: MiraRun holds desired state, each cluster's spoke
pulls it".

## 1. What it is

A control plane (the *hub*) holds desired state for agent workloads in many
clusters. It never connects into a cluster. Each cluster instead runs one
miragend in **pull mode**, the *spoke*. The spoke dials out to the hub,
claims what is addressed to it, makes its cluster match, and reports back.

`miragend-spoke` (`miragen/daemon/spoke/`) is that process. It is off unless
`MIRAGEND_SPOKE_CONFIG` names its config file.

The push-mode daemon (`miragend`, `POST /agents`) is unchanged, and so is
its bare-Pod driver (`spawn/kubernetes.py`). That driver keeps the gaps
listed in §6, which the spoke's own driver does not have.

## 2. One tick

1. **Intents.** `POST /api/spoke/v1/intents/claim` returns intents under a
   lease. For each one:
   - **Check it against the local allowlist.** That covers the namespace
     served, the workload classes allowed there, the organizations it
     serves, and the hub-requested network labels allowed. A refused
     intent is reported `refused` and acknowledged, so it is not offered
     again, and it is never applied.
   - **Render it from the spoke's own class template and apply it.** The
     hub sends no pod spec, image, or environment.
   - **Acknowledge** the generation under the lease.
   - **Report** what the cluster says.

   Applying the same generation twice changes nothing, so a lease offered
   again is safe.
2. **Watch.** The spoke reports a tracked workload's phase when it changes.
   - An ephemeral Job that has ended is deleted only after the hub took its
     final report, with a 204 answer. Its runs' events are relayed first.
     The Job's `ttlSecondsAfterFinished` is a backstop of days, for a spoke
     that dies in between.
   - An agent is a server, so its Job does not end on its own. It ends
     when the hub sets the intent to `stopped` or `deleted`, or at its
     deadline (`activeDeadlineSeconds`).
3. **Deliveries.** `POST /api/spoke/v1/deliveries/claim`. Each delivery is
   forwarded to its agent in-cluster (pod IP, `X-Miragen-Token`) and then
   settled:

   | Kind | Agent call | On an unknown outcome |
   |---|---|---|
   | `launch`, `turn`, `answer` | `POST /instances/{instance}/turns` with the delivery's `idempotency_key` | left unsettled. The hub offers it again; the agent deduplicates by key |
   | `discard` | `DELETE /instances/{instance}` (idempotent) | left unsettled |
   | `resume` | `POST /runs/{run_id}/resume` (no key) | settled `uncertain`, never sent again |
   | `abandon` | `POST /runs/{run_id}/abandon` (no key) | settled `uncertain`, never sent again |

   If the connection never opened, nothing was delivered. An at-most-once
   delivery is then settled `rejected`. An agent's 4xx is settled
   `rejected` with its reason.

   The spoke keeps a bounded memory of the at-most-once deliveries it has
   sent, so one is never sent twice even if it were offered again. It also
   keeps settles it could not deliver to the hub, and retries only the
   settle.
4. **Events.** Each turn or resume with a `run_intent_id` is tracked. Its
   event pages are read from the agent by cursor
   (`GET /runs/{id}/events?after=N`) and pushed unchanged to the hub at
   `RUN_EVENTS_ROUTE` as `{"run": <GET /runs/{id}>, "events": <page>}`.
   `(run_id, seq)` deduplicates them, so pushing a page twice is harmless.
   Tracking ends when the run is no longer running and a read made after
   seeing it stopped is empty. Its stopped record then goes up once more
   with an empty page, so the hub sees the terminal status.

   An accepted `launch` is settled with `result: {run_id, status,
   duplicate}`: the agent's turn id is the run the hub's queued launch maps
   to.

A hub that is unreachable means exponential backoff with jitter, capped at
`poll.max_backoff_seconds`. Claims long-poll (`wait_seconds`, at most 25):

- the delivery claim waits `poll.wait_seconds`, and the intent claim a
  fifth of that;
- while runs are being relayed, both return at once and the loop paces
  itself at `poll.interval_seconds`;
- a claim that returns work means the spoke polls again at once.

## 3. Configuration

Annotated in the module docstring of `miragen/daemon/spoke/config.py`:

- **`hub.url`** and **`hub.token_file`**: the token is read from a file,
  never inline and never from the environment, and never logged.
- **`images`**: the only images a template may use.
- **`classes.ephemeral`** and **`classes.persistent`**, each with:
  - `image`, `service_account_name`, `runtime_class_name`;
  - `node_selector`, `tolerations`;
  - `requests`, `limits`;
  - `run_as_user`/`run_as_group`/`fs_group`;
  - `backstop_ttl_seconds`;
  - for persistent only, `storage_size` and `storage_class_name`.
- **`namespaces.<name>`**: `classes` and `organizations`.
- **`network_label_keys`**.
- **`poll`**: `interval_seconds`, `max_backoff_seconds`, `claim_limit`.

The file is mounted by the cluster's GitOps. Nothing in it comes from the
hub.

## 4. What the driver renders (`spawn/kubernetes_workloads.py`)

- **`ephemeral` is a Job:**
  - `backoffLimit: 0`, with `activeDeadlineSeconds` from the intent's
    deadline;
  - `ttlSecondsAfterFinished` as the backstop only;
  - `restartPolicy: Never`.
- **`persistent` is a StatefulSet:**
  - `replicas` 0 or 1, so stopped is scale to zero and the volume stays;
  - a `volumeClaimTemplate` mounted at `/agent`, with an init container
    that recreates the image's state directories on an empty volume;
  - a `Retain` PVC policy. Deleting an intent deletes the claim
    explicitly.
- **Both:**
  - the profile is one ConfigMap key, mounted read-only by `subPath` at
    `/etc/miragen/profile/agent.yaml`, with `AGENT_PROFILE` pointing at it;
  - `automountServiceAccountToken: false`;
  - non-root, RuntimeDefault seccomp, every capability dropped, no
    privilege escalation;
  - nodeSelector, tolerations, RuntimeClass and resources from the
    template.
- **Not created:** no Namespace, NetworkPolicy, Role or Service. The
  namespace's admission and default-deny policies are the cluster's GitOps,
  and they are the boundary. The spoke's RBAC needs:
  - `jobs`, `statefulsets` and `configmaps`: create, get, list, update,
    patch, delete;
  - `pods`: get, list;
  - `persistentvolumeclaims`: delete.

  All of it is scoped to the namespaces it serves.

## 5. Credentials

- **The pod spec never holds a model or provider credential.** `Workload`
  refuses any environment name that looks like one.
- **One exception, `MIRAGEN_INTERNAL_TOKEN`.** It is a random value per
  workload that guards only that agent's own API from its neighbours. The
  spoke reads it back from the unit's spec after a restart.
- **Model access is the workload's model token, through the provider
  gateway, and nothing else** (`miragen/model_token.py`).
  - The hub mints a 15-minute token per workload and hands it over at claim
    as `credentials.model` `{env, token, expires_at}`. The spoke forwards it
    on the turn (`credentials.model`, never `secret_env`, which is a run's).
  - About 5 minutes before it expires, the spoke refreshes it
    (`POST /api/spoke/v1/workloads/{org}/{intent}/token`) and hands it to
    the agent (`PUT /credentials/model`). Only live workloads are refreshed.
    The hub's once-a-minute 429 is waited out. A token the agent did not
    take is pushed again, never minted again.
  - The agent keeps it in memory and in one 0600 file on an in-memory
    volume (`emptyDir` `medium: Memory`, at `MIRAGEN_MODEL_TOKEN_DIR`). The
    file sits in a `private/` directory the runtime creates and keeps at
    0700, because the volume root is the kubelet's and 0777. It is cleared
    at shutdown. It is never persisted, logged or put in a repr,
    and never appears in the pod spec, the workspace or a run record.
  - Claude Code reads it through an `apiKeyHelper` set in flag settings,
    which outrank every settings file, with
    `CLAUDE_CODE_API_KEY_HELPER_TTL_MS=60000`. `ANTHROPIC_BASE_URL` and the
    TTL also sit in the flag settings' `env`, which beats the process env and
    any `settings.json`, so a session cannot point the token at another
    host. Its `ANTHROPIC_BASE_URL` is
    `<MIRAGEN_MODEL_GATEWAY_URL>/anthropic/<connection>`; the connection
    comes from the class's `model_connection`. Both come from configuration,
    never from the hub. A per-profile connection would be a profile-contract
    change (v7, with the image label), so it is left for its own PR.
  - **No fallback.** No subscription token or API key reaches Claude Code,
    and a saved login refuses the harness. With no token, or an expired one,
    the helper fails and so does every model call. This follows
    code.claude.com/docs/en/llm-gateway-connect ("Rotate credentials with
    apiKeyHelper") and /authentication ("Authentication precedence").
  - **Nothing echoes it.** Every 422 drops `input` and `ctx` (an app-wide
    handler). The turns route takes `credentials` off the body before
    validating the rest. The spoke scrubs anything token-shaped from a
    peer's error detail before logging it or settling with it.
  - **Egress is the cluster's.** The spoke creates no NetworkPolicy, and
    its RBAC has none. The boundary is the sandbox namespaces'
    default-deny `CiliumNetworkPolicy` from the deployment's infra
    (ADR-0025). It allows DNS and an FQDN allowlist, and denies the
    metadata service and cluster internals. The provider gateway's and the
    broker's hostnames must be on that allowlist before any agent can
    reach a model.
  - **After a spoke restart**, the expiry of the token each agent holds is
    unknown, so gateway workloads are refreshed on the first tick. A minted
    token that expired before the agent took it is replaced, never handed
    over.
  - Spoke class config: `model_gateway_url` (https, or http only to a cluster-local Service such as `*.svc.cluster.local`),
    `model_connection`, and `model_token_dir`.

## 6. Not done

- **Event cursors live in memory.** After a spoke restart, relay for a run
  resumes only when the hub delivers to that run again. Persisting cursors,
  for example as an annotation on the unit, is a follow-up.
- **Harness and runtime share one UID** (`agentuser`, Dockerfile). The
  run record miragen writes is therefore only as strong as the agent's
  inability to edit it (ADR-045 §7, tier 2). Separating them needs an image
  change: a second user for the harness and a spool the harness cannot
  write.
- **The token file is readable by the harness's UID**, which is also the
  runtime's (see the UID item above). A separate harness user would make it
  readable by the harness only.
- **A managed-settings lock.** An image-level `managed-settings.json` with
  `allowedProviders: ["customEndpoint"]` and the gateway in its `env` would
  make Claude Code itself refuse any other provider. That is an image (deploy)
  change, so it belongs in its own PR.
- **Liveness and readiness probes for the spoke itself.** Nothing serves
  HTTP. An exec probe on a heartbeat file is the likely shape.
- **The push-mode bare-Pod driver** (`spawn/kubernetes.py`) still has G1-G5:
  - it mounts the profile over `/agent`;
  - it writes credentials into its ConfigMap and the pod env;
  - it sets no scheduling or RuntimeClass;
  - it creates bare Pods;
  - it uses toy limits.

  The spoke does not use it.

## 7. Notes on the hub contract

- **The contract is MiraRun's published spec.** That is
  `docs/contracts/spoke-api/openapi.json`; this repository keeps a copy in
  `tests/contracts/`.
  - The unit tests' fake hub (`tests/spoke_fakes.py`) validates every
    request and response against it, and enforces the hub's leases,
    write-once turn results and event cursors.
  - `tests/test_hub_contract.py` (marker `hub_contract`, skipped unless
    mirarun is importable; `MIRARUN_SRC` adds a checkout) runs the spoke's
    `HubClient` against MiraRun's real `spoke_router`.
  - That test uses the hub's own run-record and event-page parsers, and its
    turn-record checks, fed by miragen's own `RunStore` and event writer.
  - Last run against feat/workload-deliveries-wiring `42d015d`: 5 passed.
- **Settle means dispatched.** A `turn`'s or `answer`'s reply goes to
  `POST deliveries/{id}/result` once the turn has finished. A settle that
  carries one is refused.
- **Resuming after a restart.** `GET runs/open` returns the spoke's
  unfinished runs and the hub's cursor for each, and pushed pages are
  idempotent. A restarted spoke resumes relaying from the hub's position.

- **Credentials minted at claim are forwarded into the turn.** A claimed
  `launch` may carry the run's credentials. The spoke forwards them as the
  turn's `credentials` (`POST /instances/{i}/turns`, base tier only). The
  agent holds them for that turn in memory and drops them at its end:
  - a pydantic-ai profile resolves them in-process;
  - a CLI harness's tool gateway uses them for the matching upstream.

  Two contract points:
  - the base tier finds them by the environment variable a capability names
    (`bearer_token_env`), so the hub must send `secret_env` keyed by that
    name. Values keyed by EDF binding name (`secrets`) are refused, never
    dropped silently;
  - repository bindings are refused, because the base tier prepares no
    checkout.
- **The event route is newer than the rest.** It sits behind `EventSink`
  and one constant (`RUN_EVENTS_ROUTE`).
- **Delivery payloads are read as:**
  - `instance`, `prompt`, `provenance` and `mirarun` for turn kinds;
  - `instance` for `discard`;
  - `run_id` plus `prompt`/`answer` for `resume`;
  - `run_id` plus `discard_workspace` for `abandon`.

  A turn without `instance` uses `r-<run_intent_id prefix>`. The hub has
  to fill these consistently.
- **Key length.** A turn's idempotency key is capped at 200 characters by
  the agent, and the hub allows 255. A longer key is settled `rejected`.
