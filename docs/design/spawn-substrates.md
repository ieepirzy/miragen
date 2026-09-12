# Spawn substrates: Docker Compose and Kubernetes

**Status:** shipped. `feat/spawn-driver-seam` (extraction) +
`feat/kubernetes-spawn-driver` (this).

## 1. Through-line

> **miragen is one execution substrate; mirarun is one orchestrator that can
> drive it — not the only one that ever will.**

Nothing before this pair of changes wrote that down anywhere in this repo
(confirmed by grep across docs/README/CLAUDE.md — the closest existing
statement is `mirarun-substrate-contracts.md`'s "miragen owns run state; the
control plane owns product entities," which draws the ownership boundary but
doesn't say a *second* control plane is a design goal, not an accident). It
is written down now because the first concrete case for it — mirarun asking
for network-layer access grants on a spawned agent's pod — forced the
question of whether miragen's `/agents` API is allowed to know what
`access.mirarun.io/*` means. It is not. The API accepts an opaque
`labels: dict[str, str]`, records it, forwards it to whichever spawn driver
is active, and interprets none of it. Any other orchestrator gets the same
door.

## 2. What changed

Before: `LifecycleCore` (`miragen/daemon/core.py`) inlined every Docker
Compose operation directly against an injected `docker_client` +
`subprocess.run`-compatible `runner` — no seam, so a second substrate meant
branching inside two dozen methods.

After: those operations sit behind `SpawnDriver`
(`miragen/daemon/spawn/base.py`) — `ensure_ready`, `define_service`, `up`,
`remove_service`, `status`, `logs`, `restart`, `stop`, `teardown`,
`current_image`, `endpoint`. `LifecycleCore` keeps everything that *isn't*
about where an agent runs: naming, workspace layout, profile/contract
validation, credential-env assembly, boot-watching, rollback ordering. Two
implementations exist:

- `DockerComposeSpawnDriver` (`spawn/docker_compose.py`) — the original
  logic, moved, not rewritten. `feat/spawn-driver-seam`'s test plan is
  exactly "the same 92 tests pass unchanged."
- `KubernetesSpawnDriver` (`spawn/kubernetes.py`) — new. One bare `Pod` per
  agent plus a companion `ConfigMap` (holding `agent.yaml`, `tools.py`, and
  the resolved `ServiceSpec` as JSON, since a Pod can't be edited in place
  the way a compose service can — `up()`/`restart()` delete and recreate the
  Pod from what's in the ConfigMap). No Deployment, no Service.

A deployment picks one via `MIRAGEN_SPAWN_SUBSTRATE` (`docker-compose`
default, `kubernetes` opt-in) — see `daemon/api.py:main()`. The choice is
the daemon operator's, not the orchestrator's; the `/agents` HTTP contract
is identical either way.

## 3. Why a bare Pod, not a Deployment

Matches mirarun's ADR-026 (agents are one-agent-one-container, not
one-run-one-container) directly — a Deployment's whole value is managing
*replicas* of a fungible pod template, which is the wrong shape for "exactly
one named, stateful-ish agent process." A bare Pod is also the minimum
footprint consistent with §4 below: nothing here creates more cluster
objects than it has to.

## 4. What this deliberately does not do (read before "fixing" it)

- **No Namespace, no NetworkPolicy, no Service.** `ieepirzy/infra`'s
  ADR-0011: a Flux-reconciled cluster accepts imperative `kubectl apply`
  only as break-glass. Whatever namespace this driver targets, and whatever
  network-layer policy governs the pods in it, is provisioned by GitOps —
  never by this driver. This is *why* labels exist as a passthrough instead
  of miragen calling a NetworkPolicy API itself: mirarun computes the
  label set an agent's pod needs
  ([mirarun#61](https://github.com/ieepirzy/mirarun/issues/61)) and hands
  it to this endpoint; a Flux-managed baseline policy in the target's own
  namespace selects on it declaratively.
- **No image-contract validation on this substrate either.** Stayed in
  `LifecycleCore`, unconditionally, on both substrates — kubelet exposes no
  image-inspect API, so there's no substrate-neutral way to do the #75
  contract check the way it's done against the Docker SDK today. A
  Kubernetes-only deployment still needs a Docker socket for this one
  check.
- **No Docker-secrets equivalent.** `*_API_KEY_FILE`-style file-backed
  secrets still reach the pod's env as a var naming a path, but no file is
  mounted there. Fails at whatever reads the missing file, not at spawn
  time — a known v1 gap, not a silent one.
- **No stable per-agent DNS.** No Service means no cluster-DNS name;
  `endpoint()` reads the pod's live IP from the API on every call rather
  than caching one that can go stale on recreate.
- **Built and tested with no live cluster**, the same way the read-only
  Kubernetes target inspector on the mirarun side was: against a fake HTTP
  transport standing in for the API server
  (`tests/test_kubernetes_spawn_driver.py`, mirroring
  `test_daemon_core.py`'s `FakeDocker`/`RecordingRunner`). Whether a pod
  actually boots on a real cluster is unverified until one exists.

## 5. Multi-tenant / customer clusters

Out of scope here and likely for a long time — noted as a future
possibility, not designed for, per
[mirarun#59](https://github.com/ieepirzy/mirarun/issues/59). This driver
assumes one cluster the daemon itself runs in (in-cluster ServiceAccount
auth — `KubernetesSpawnDriver.from_incluster_config()` — the same
local-socket symmetry Compose already has: reach the substrate using
whatever identity the daemon is already running under).
