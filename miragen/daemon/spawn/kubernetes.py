"""Kubernetes spawn driver — a bare Pod per agent, no Deployment/Service.

Talks to the cluster's own API server over plain HTTPS (bearer token +
pinned CA — the standard in-cluster ServiceAccount credential every pod
gets for free), not the `kubernetes` SDK, to stay consistent with how this
codebase already talks to Kubernetes elsewhere and to avoid a heavy new
dependency for what is a handful of REST calls.

## What this driver deliberately does NOT do

- **No Namespace, no NetworkPolicy, no Service.** ADR-0011 in the infra
  repo: a Flux-reconciled cluster only accepts imperative writes as
  break-glass. This driver assumes its target namespace, and any
  reachability policy for the pods it creates, are provisioned out of band
  by GitOps — never "fix" this by having the driver create them itself.
- **No Docker-secrets equivalent.** `ServiceSpec.secret_names` (Docker
  secret names derived from `*_API_KEY_FILE` env vars) is accepted but
  ignored — those variables' raw values/paths still reach the pod via
  `ServiceSpec.environment` like every other env var, but no file is
  mounted at the path they name. A profile that needs a real file-backed
  secret on this substrate is a known v1 gap, not a silent one: it will
  fail at whatever reads the missing file, not at spawn time.
- **No image-contract validation equivalent.** `LifecycleCore` already
  skips this for any driver — kubelet exposes no image-inspect API — so
  nothing here needs to special-case it.
- **No live cluster was used to build or test this** — it is built and
  tested the same way the read-only Kubernetes target inspector on the
  mirarun side was: against a fake HTTP transport standing in for the API
  server. `ensure_ready()`/create/status/delete were each verified against
  that fake, but the actual "does the pod boot" question can only be
  answered once a cluster exists.

## Addressing

No stable per-agent DNS name exists without a Service (deliberately not
created — see above), so `endpoint()` does a live API read of the pod's
current IP each time it's called, rather than caching one.

## Identity

The agent name IS the Pod name and the ConfigMap name (mirroring Compose,
where it's the container name and compose service name) — Kubernetes'
naming rules are the tightest of any substrate this daemon speaks
(RFC 1123 label: lowercase alphanumeric or '-', must start and end
alphanumeric, no underscore), tighter than `AGENT_NAME_PATTERN` allows, so
`define_service` re-checks it and fails clearly rather than letting a name
Docker accepted reach the API server as a 422 it didn't ask for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from miragen.daemon.spawn.base import (
    ServiceSpec,
    SpawnOperationFailed,
    SpawnUnitNotFound,
)

_DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_CONTAINER_NAME = "agent"
_CONTAINER_PORT = 8000

# Docker Compose's mem_limit syntax ("512m", "1g") -> Kubernetes resource
# quantity suffixes. Both are binary (Mi/Gi = *1024, matching Docker's own
# interpretation of these suffixes), so this is a straight relabeling, not
# a unit conversion.
_MEM_SUFFIX = {"b": "", "k": "Ki", "m": "Mi", "g": "Gi"}

_INCLUSTER_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class InClusterConfigUnavailable(Exception):
    """This process is not running inside a Kubernetes pod."""


def _k8s_memory(docker_mem_limit: str) -> str:
    value = docker_mem_limit.strip()
    if not value:
        return value
    suffix = value[-1].lower()
    magnitude = value[:-1]
    if suffix in _MEM_SUFFIX and magnitude.isdigit():
        unit = _MEM_SUFFIX[suffix]
        return f"{magnitude}{unit}" if unit else magnitude
    return value


class KubernetesSpawnDriver:
    def __init__(
        self,
        *,
        base_url: str,
        namespace: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        verify: str | bool = True,
        timeout: float = 30.0,
    ) -> None:
        self._namespace = namespace
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            verify=verify if transport is None else True,
            timeout=timeout,
        )

    @classmethod
    def from_incluster_config(cls, *, timeout: float = 30.0) -> KubernetesSpawnDriver:
        """The standard ServiceAccount credential every pod is given —
        docker-compose's local-socket symmetry: a spawn driver reaches its
        substrate using whatever identity the daemon itself is already
        running under, nothing separately provisioned."""
        try:
            token = (_INCLUSTER_DIR / "token").read_text().strip()
            namespace = (_INCLUSTER_DIR / "namespace").read_text().strip()
            ca_path = _INCLUSTER_DIR / "ca.crt"
            if not ca_path.exists():
                raise FileNotFoundError(str(ca_path))
        except OSError as exc:
            raise InClusterConfigUnavailable(
                "not running inside a Kubernetes pod with a mounted "
                "ServiceAccount token"
            ) from exc
        import os

        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise InClusterConfigUnavailable(
                "KUBERNETES_SERVICE_HOST is not set — not running inside a pod"
            )
        return cls(
            base_url=f"https://{host}:{port}",
            namespace=namespace,
            token=token,
            verify=str(ca_path),
            timeout=timeout,
        )

    # -- API server helpers ---------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SpawnOperationFailed(str(exc)) from exc

    def _configmap_collection_path(self) -> str:
        return f"/api/v1/namespaces/{self._namespace}/configmaps"

    def _configmap_path(self, name: str, suffix: str = "") -> str:
        return f"{self._configmap_collection_path()}/{name}{suffix}"

    def _pod_collection_path(self) -> str:
        return f"/api/v1/namespaces/{self._namespace}/pods"

    def _pod_path(self, name: str, suffix: str = "") -> str:
        return f"{self._pod_collection_path()}/{name}{suffix}"

    def _get_configmap(self, name: str) -> dict | None:
        resp = self._request("GET", self._configmap_path(name))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SpawnOperationFailed(
                f"reading configmap '{name}' failed: {resp.status_code} {resp.text}"
            )
        return resp.json()

    def _get_pod(self, name: str) -> dict | None:
        resp = self._request("GET", self._pod_path(name))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SpawnOperationFailed(
                f"reading pod '{name}' failed: {resp.status_code} {resp.text}"
            )
        return resp.json()

    def _delete_pod(self, name: str) -> None:
        resp = self._request("DELETE", self._pod_path(name))
        if resp.status_code not in (200, 202, 404):
            raise SpawnOperationFailed(
                f"deleting pod '{name}' failed: {resp.status_code} {resp.text}"
            )

    # -- SpawnDriver ------------------------------------------------------------

    def ensure_ready(self) -> None:
        # Namespace/NetworkPolicy provisioning is out of band (GitOps) — see
        # module docstring. Nothing for this driver to do.
        pass

    def define_service(self, name: str, spec: ServiceSpec) -> None:
        if not _DNS1123_LABEL.fullmatch(name):
            raise SpawnOperationFailed(
                f"'{name}' is not a valid Kubernetes object name (must be "
                "lowercase alphanumeric or '-', starting and ending "
                "alphanumeric) — this agent cannot be spawned on the "
                "kubernetes substrate"
            )
        agent_yaml = (spec.workspace_dir / "agent.yaml").read_text()
        tools_py = (spec.workspace_dir / "tools.py").read_text()
        record = {
            "image": spec.image,
            "environment": dict(spec.environment),
            "labels": dict(spec.labels),
            "cpu_limit": spec.cpu_limit,
            "mem_limit": spec.mem_limit,
        }
        configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "labels": dict(spec.labels)},
            "data": {
                "agent.yaml": agent_yaml,
                "tools.py": tools_py,
                "spec.json": json.dumps(record),
            },
        }
        resp = self._request(
            "POST", self._configmap_collection_path(), json=configmap
        )
        if resp.status_code == 409:
            # No meaningful revision history for a ConfigMap worth
            # preserving — replace it outright rather than tracking a
            # resourceVersion for a PUT.
            self._request("DELETE", self._configmap_path(name))
            resp = self._request(
                "POST", self._configmap_collection_path(), json=configmap
            )
        if resp.status_code not in (200, 201):
            raise SpawnOperationFailed(
                f"defining configmap '{name}' failed: {resp.status_code} {resp.text}"
            )

    def up(self, name: str) -> None:
        configmap = self._get_configmap(name)
        if configmap is None:
            raise SpawnOperationFailed(
                f"agent '{name}' has no stored definition to start — "
                "define_service must be called first"
            )
        record = json.loads(configmap.get("data", {}).get("spec.json") or "{}")
        self._delete_pod(name)
        self._create_pod(name, record)

    def _create_pod(self, name: str, record: dict) -> None:
        env = record.get("environment") or {}
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": record.get("labels") or {}},
            "spec": {
                "restartPolicy": "Always",
                "containers": [
                    {
                        "name": _CONTAINER_NAME,
                        "image": record.get("image"),
                        "ports": [{"containerPort": _CONTAINER_PORT}],
                        "env": [
                            {"name": key, "value": value} for key, value in env.items()
                        ],
                        "resources": {
                            "limits": {
                                "cpu": record.get("cpu_limit", ""),
                                "memory": _k8s_memory(record.get("mem_limit", "")),
                            }
                        },
                        "volumeMounts": [{"name": "agent-config", "mountPath": "/agent"}],
                    }
                ],
                "volumes": [
                    {"name": "agent-config", "configMap": {"name": name}}
                ],
            },
        }
        resp = self._request("POST", self._pod_collection_path(), json=pod)
        if resp.status_code not in (200, 201):
            raise SpawnOperationFailed(
                f"creating pod '{name}' failed: {resp.status_code} {resp.text}"
            )

    def remove_service(self, name: str) -> None:
        resp = self._request("DELETE", self._configmap_path(name))
        if resp.status_code not in (200, 202, 404):
            raise SpawnOperationFailed(
                f"removing configmap '{name}' failed: {resp.status_code} {resp.text}"
            )

    def status(self, name: str) -> str:
        try:
            pod = self._get_pod(name)
        except SpawnOperationFailed as exc:
            return f"error: {exc}"
        if pod is None:
            return "not found"
        statuses = pod.get("status", {}).get("containerStatuses") or []
        if not statuses:
            return "pending"
        state = statuses[0].get("state", {})
        if "running" in state:
            return "running"
        if "terminated" in state:
            return "exited"
        if "waiting" in state:
            reason = state["waiting"].get("reason", "")
            if reason in ("CrashLoopBackOff", "Error", "RunContainerError"):
                return "restarting"
            return "pending"
        return "pending"

    def logs(self, name: str, *, tail: int) -> str:
        resp = self._request(
            "GET",
            self._pod_path(name, "/log"),
            params={
                "tailLines": min(max(tail, 1), 1000),
                "container": _CONTAINER_NAME,
            },
        )
        if resp.status_code == 404:
            raise SpawnUnitNotFound(name)
        if resp.status_code != 200:
            raise SpawnOperationFailed(
                f"reading logs for '{name}' failed: {resp.status_code} {resp.text}"
            )
        return resp.text

    def restart(self, name: str) -> None:
        configmap = self._get_configmap(name)
        pod = self._get_pod(name)
        if pod is None:
            raise SpawnUnitNotFound(name)
        if configmap is None:
            raise SpawnOperationFailed(
                f"agent '{name}' has a running pod but no stored definition"
            )
        record = json.loads(configmap.get("data", {}).get("spec.json") or "{}")
        self._delete_pod(name)
        self._create_pod(name, record)

    def stop(self, name: str) -> None:
        pod = self._get_pod(name)
        if pod is None:
            raise SpawnUnitNotFound(name)
        self._delete_pod(name)

    def teardown(self, name: str) -> None:
        self._delete_pod(name)

    def current_image(self, name: str) -> str | None:
        configmap = self._get_configmap(name)
        if configmap is None:
            return None
        record = json.loads(configmap.get("data", {}).get("spec.json") or "{}")
        return record.get("image")

    def endpoint(self, name: str) -> str:
        pod = self._get_pod(name)
        ip = (pod or {}).get("status", {}).get("podIP")
        if not ip:
            # No live pod / no assigned IP yet — an unreachable placeholder
            # is more honest than raising, matching how the Compose driver
            # returns a URL regardless of whether the container is running.
            return f"http://{name}.invalid:{_CONTAINER_PORT}"
        return f"http://{ip}:{_CONTAINER_PORT}"
