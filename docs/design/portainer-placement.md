# Placement: choosing where an agent runs, through Portainer

**Status:** proposal, 2026-10-02. Nothing here is built. The open decisions
at the end are Ilari's.

## 1. What is being asked for

Today one miragend creates agents on one Docker engine: its own. The fleet
has more than one. The Muutto365 Portainer has three environments (the main
VPS, the CRM host and the Odoo host), and the main VPS, where every agent
runs, is the fullest of them.

The ask (Ilari, 2026-10-01): miragend should be able to pick the
environment an agent is deployed into, the way miradeploy picks one for a
stack. It is a workaround, and named as one. Kubernetes has a controller
that places workloads; Portainer over a handful of hosts does not. When the
fleet moves to Kubernetes, placement goes back to the cluster, and the
control plane's dispatch goes around a miragend-shaped controller there.
The order is ARC for CI first, then agents.

So the design has two jobs: make placement work on Portainer now, and
leave nothing behind that the Kubernetes move has to undo.

## 2. What exists

- **The seam.** `LifecycleCore` hands a resolved `ServiceSpec` to a
  `SpawnDriver` (`daemon/spawn/base.py`). Two drivers: Docker Compose
  (default) and Kubernetes (a bare Pod and a ConfigMap, never run against a
  live cluster). `docs/design/spawn-substrates.md`.
- **The substrate is chosen once, at daemon start**
  (`MIRAGEN_SPAWN_SUBSTRATE`). That document says so on purpose: "The choice
  is the daemon operator's, not the orchestrator's."
- **`POST /agents` takes** `name`, `yaml_source`, `tools_source` and
  `labels`. Labels are opaque passthrough. There is no placement.
- **The Compose driver assumes one host** in three places:
  - the unit's files are a bind mount, `./agents/{name}:/agent`, on the
    daemon's own disk. Run records, executor workspaces and the
    codex/grok/kimi homes all live under it;
  - the agent's address is its container name on `miragen-net`, a bridge
    network that exists only on that host;
  - image-contract validation inspects the image through the local Docker
    socket.
- **Portainer** exposes each environment's Docker API through a proxy
  (`/api/endpoints/{id}/docker/...`), authenticated with an API key that is
  scoped to a Portainer user. miradeploy uses it for container listing and
  logs. miradeploy's own stack calls are git-backed stacks only: no
  stack-from-a-compose-string, no delete.

## 3. The shape proposed

**A third spawn driver, `portainer`, and a `placement` on the agent.**

- `POST /agents` gains an optional `placement: {"environment": "<name>"}`.
  Absent means the daemon's default. The value is a name the daemon
  operator configured, never an endpoint id or a URL: the caller picks from
  a list it was given.
- The daemon is configured with the Portainer URL, an API key, and a map of
  placement names to endpoint ids (`MIRAGEN_PORTAINER_ENVIRONMENTS`). An
  environment that is not in the map does not exist as far as callers are
  concerned. `GET /placements` lists the names, so a control plane can
  offer them.
- The driver talks to the chosen environment's Docker API through
  Portainer's proxy: create, start, stop, remove, inspect, logs. The same
  operations the Compose driver makes through the SDK today, one container
  per agent, no stack. Plain containers rather than Portainer stacks,
  because a stack per agent would have to be created from a compose string,
  updated and deleted, and the stack is not the unit anyone manages here.
- The agent's placement is recorded with the agent and is returned by
  `GET /agents`. Moving an agent is delete and create. There is no
  migration.

This keeps `spawn-substrates.md`'s rule in the form that matters. The
operator still decides which substrate the daemon speaks and which
environments exist. What the orchestrator gains is a choice among
environments the operator listed. On Kubernetes the same field becomes a
namespace or a node selector, or disappears.

## 4. What has to be solved for it to work

These are not details. Each one is a reason the Compose driver cannot
simply be pointed at another host.

1. **The agent's files.** A bind mount from the daemon's disk does not
   exist on another host. The unit needs a named volume on its own host,
   and the daemon needs a way to put `agent.yaml`, `tools.py` and later
   config updates into it. The Docker API's archive upload (`PUT
   /containers/{id}/archive`) does that through the proxy. Everything that
   reads the workspace from the daemon's side today (`/agents/{name}/files`,
   export, import) has to go through the container instead, or be refused
   for a remote placement.
2. **Reaching the agent.** A control plane reaches an agent at the address
   the daemon reports. Across hosts that cannot be a container name on a
   local bridge. On this fleet it would be the host's WireGuard address and
   a published port, which means allocating ports per host and binding them
   to the WireGuard interface only. The daemon's own calls to the agent
   (`/health` while watching boot, schedules, the run-control proxy) take
   the same path.
3. **Secrets.** Docker secrets named in the profile must exist on the
   target host. Either they are provisioned per host out of band, or the
   driver refuses a profile that needs one on a host that lacks it. The
   Kubernetes driver has the same gap and says so.
4. **Image contract.** The check inspects an image locally. For a remote
   placement it has to inspect on the target through the proxy, after a
   pull there. The registry credential for a private image is the target
   environment's, held by Portainer.
5. **Portainer's own limits.** The API key's user must be allowed the
   Docker proxy on each environment, and must not be an administrator.
   What a non-admin can do through the proxy differs between Portainer CE
   and BE and is unverified for the version deployed.
6. **Capacity.** Nothing in miragend knows how much room a host has.
   `MIRAGEND_MAX_AGENTS` counts profiles in total. A per-environment limit
   is the least that is needed; choosing the emptiest host is a scheduler,
   and a scheduler is exactly what this is a stand-in for.

## 5. The alternative: one miragend per host

Run a daemon on each host that should take agents, each with its local
Compose driver, unchanged. The control plane keeps a list of daemons and
picks one.

- For: no new driver, no remote files, no cross-host reachability inside
  miragend. Each host is the single-host case that already works.
- Against: the placement logic and the registry of daemons move into the
  control plane (mirarun has one `MIRARUN_MIRAGEND_URL` today), every host
  runs a daemon with a Docker socket, and it is the control plane, not
  miragend, that selects the environment, which is not what was asked for.
- On Kubernetes it maps to "one controller per cluster", which is the
  shape §5 of `spawn-substrates.md` already assumes.

## 6. What carries over to Kubernetes

- `placement` as an operator-listed name carries over. Its meaning changes.
- The Portainer driver does not. It is the workaround.
- The file and reachability work in §4 does not either: Kubernetes has
  volumes and Services. That is the cost of the workaround, and the size
  of it is the argument for §5 if the Kubernetes move is near.
- One gap is shared and worth fixing regardless: on Kubernetes `/agent` is
  a ConfigMap and `stop` deletes the Pod, so a stopped agent keeps no run
  records, workspaces or executor threads. Nothing that resumes a stopped
  agent (a wake, an answer to a question it asked) can work there until
  the agent's state is on a volume.

## 7. Decisions needed

1. **Driver or daemons.** §3 (a Portainer driver, miragend selects) or §5
   (one miragend per host, the control plane selects). §3 is what was asked
   for and is the larger build; §5 is nearly free and is closer to where
   Kubernetes ends up.
2. **How near is Kubernetes.** If agents move within a couple of months,
   §4 is a lot of work to throw away.
3. **Which environments.** The CRM and Odoo hosts are small and run
   production workloads. Is an agent allowed on them, and with what limits?
4. **Trust boundary.** The homelab is its own Portainer and must not
   become an environment of the company daemon, or the reverse. Confirm
   that placement is always within one Portainer.
5. **Reachability.** WireGuard address plus a published port per agent, or
   something else?
