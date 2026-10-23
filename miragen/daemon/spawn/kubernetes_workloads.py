"""Kubernetes workloads for a spoke: a Job per ephemeral agent, a
StatefulSet per persistent one.

The pull-mode spoke (`miragen.daemon.spoke`) renders every object here from
its own templates. The hub names a namespace, a workload class and a profile;
it never sends a pod spec, an image or an environment. What this module
fixes against the bare-Pod driver in `kubernetes.py` (the 2026-10-14
assessment's G1-G5):

- **G1, profile over /agent.** The profile is one ConfigMap key mounted
  read-only by `subPath` at `/etc/miragen/profile/agent.yaml`, and the agent
  is pointed at it with `AGENT_PROFILE`. `/agent` stays the image's own
  writable directory (a Job) or a volume (a StatefulSet).
- **G2, credentials in plaintext.** No credential is written into a
  ConfigMap, and `Workload.env` refuses credential-looking names. The one
  exception is `MIRAGEN_INTERNAL_TOKEN`: a random per-workload value that
  only guards this one agent's own API from its neighbours, set by the spoke
  and readable only by what may read the spoke's pods. Model credentials come
  from a `ModelCredentialSource` (`miragen.daemon.spoke.credentials`), which
  by default provides none.
- **G3, unschedulable pods.** nodeSelector, tolerations and the RuntimeClass
  (gVisor) come from the spoke's class template.
- **G4, bare Pods.** A Job or a StatefulSet, so the autoscaler can drain a
  node and a stopped persistent agent keeps its volume.
- **G5, toy limits.** Requests and limits come from the class template.

Agent pods never mount a ServiceAccount token, drop every capability, run
as non-root with RuntimeDefault seccomp, and cannot escalate.

Not done, by design: no Namespace, NetworkPolicy, Role or Service is created.
Those belong to the cluster's GitOps (infra ADR-0025: the namespace's
admission and default-deny policies are the boundary). An agent is reached at
its pod IP, which the spoke reads from the API each time.

Built and tested against a fake API server (httpx.MockTransport), like
`kubernetes.py`. Whether a pod boots on a real cluster is not yet verified.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from miragen.daemon.spawn.base import SpawnOperationFailed

WORKLOAD_CLASSES = ("persistent", "ephemeral")
AGENT_PORT = 8000
PROFILE_DIR = "/etc/miragen/profile"
PROFILE_KEY = "agent.yaml"
STATE_DIR = "/agent"
CONTAINER = "agent"

MANAGED_BY = "miragen.io/managed-by"
MANAGED_BY_VALUE = "miragen-spoke"
WORKLOAD_LABEL = "miragen.io/workload"
GENERATION_ANNOTATION = "miragen.io/generation"

_DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
# Names that look like a credential never reach a pod's spec (G2).
_CREDENTIAL_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)")
_INTERNAL_TOKEN_ENV = "MIRAGEN_INTERNAL_TOKEN"
# The agent image's state directories (Dockerfile), recreated on an empty
# persistent volume before the agent starts.
_STATE_SUBDIRS = (
    "runs", "workspaces", "histories", "schedules", "memory",
    "grok-home", "claude-home", "codex-home", "kimi-home",
)
_INCLUSTER_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class InClusterConfigUnavailable(Exception):
    """This process is not running inside a Kubernetes pod."""


@dataclass(frozen=True)
class WorkloadTemplate:
    """The spoke's own pod template for one workload class."""

    image: str
    service_account_name: str
    runtime_class_name: str | None = None
    node_selector: Mapping[str, str] = field(default_factory=dict)
    tolerations: Sequence[Mapping[str, Any]] = ()
    requests: Mapping[str, str] = field(default_factory=dict)
    limits: Mapping[str, str] = field(default_factory=dict)
    run_as_user: int = 1000
    run_as_group: int = 1000
    fs_group: int = 1000
    # A Job is deleted by the spoke once the hub has its final report; this
    # is only the backstop for a spoke that never comes back (days).
    backstop_ttl_seconds: int = 3 * 24 * 3600
    storage_size: str | None = None
    storage_class_name: str | None = None
    # The provider gateway this class's agents call models through (MiraRun
    # ADR-045 §5): its root and the default connection. The workload's model
    # token then lives on an in-memory volume at `model_token_dir`.
    model_gateway_url: str | None = None
    model_connection: str | None = None
    model_token_dir: str = "/run/miragen/model"


@dataclass(frozen=True)
class Workload:
    name: str
    namespace: str
    workload_class: str
    template: WorkloadTemplate
    profile_yaml: str
    generation: int
    running: bool
    internal_token: str
    labels: Mapping[str, str] = field(default_factory=dict)
    annotations: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    deadline_seconds: int | None = None

    def __post_init__(self) -> None:
        for value, what in ((self.name, "name"), (self.namespace, "namespace")):
            if not _DNS1123.fullmatch(value):
                raise SpawnOperationFailed(f"{what} '{value}' is not a Kubernetes name")
        if self.workload_class not in WORKLOAD_CLASSES:
            raise SpawnOperationFailed(f"unknown workload class '{self.workload_class}'")
        for key in self.env:
            if not _ENV_NAME.fullmatch(key):
                raise SpawnOperationFailed(f"'{key}' is not an environment variable name")
            if _CREDENTIAL_NAME.search(key):
                raise SpawnOperationFailed(
                    f"'{key}' looks like a credential; credentials never go into a pod spec"
                )
        if self.workload_class == "persistent" and not self.template.storage_size:
            raise SpawnOperationFailed("a persistent workload needs storage_size in its template")
        if self.deadline_seconds is not None and self.deadline_seconds < 1:
            raise SpawnOperationFailed("the deadline has already passed")


@dataclass(frozen=True)
class WorkloadStatus:
    """What the cluster says, as the spoke will report it (a claim)."""

    phase: str  # absent, pending, running, stopped, succeeded, failed
    generation: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    oom_killed: bool | None = None
    pod_uid: str | None = None
    pod_ip: str | None = None
    reason: str | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in ("succeeded", "failed")


@dataclass(frozen=True)
class ManagedWorkload:
    name: str
    namespace: str
    workload_class: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]


def profile_configmap_name(name: str) -> str:
    return f"{name}-profile"


class KubernetesWorkloadDriver:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        verify: str | bool = True,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            verify=verify if transport is None else True,
            timeout=timeout,
        )

    @classmethod
    def from_incluster_config(cls, *, timeout: float = 30.0) -> KubernetesWorkloadDriver:
        import os

        try:
            token = (_INCLUSTER_DIR / "token").read_text().strip()
            ca_path = _INCLUSTER_DIR / "ca.crt"
            if not ca_path.exists():
                raise FileNotFoundError(str(ca_path))
        except OSError as exc:
            raise InClusterConfigUnavailable(
                "not running inside a Kubernetes pod with a mounted ServiceAccount token"
            ) from exc
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise InClusterConfigUnavailable("KUBERNETES_SERVICE_HOST is not set")
        return cls(base_url=f"https://{host}:{port}", token=token, verify=str(ca_path),
                   timeout=timeout)

    # -- the API server ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SpawnOperationFailed(str(exc)) from exc

    def _expect(self, response: httpx.Response, ok: tuple[int, ...], what: str) -> httpx.Response:
        if response.status_code not in ok:
            raise SpawnOperationFailed(f"{what} failed: {response.status_code} {response.text}")
        return response

    @staticmethod
    def _path(namespace: str, kind: str, name: str = "") -> str:
        group = {
            "configmaps": "/api/v1",
            "pods": "/api/v1",
            "persistentvolumeclaims": "/api/v1",
            "jobs": "/apis/batch/v1",
            "statefulsets": "/apis/apps/v1",
        }[kind]
        return f"{group}/namespaces/{namespace}/{kind}" + (f"/{name}" if name else "")

    def _get(self, namespace: str, kind: str, name: str) -> dict | None:
        response = self._request("GET", self._path(namespace, kind, name))
        if response.status_code == 404:
            return None
        return self._expect(response, (200,), f"reading {kind}/{name}").json()

    def _delete(self, namespace: str, kind: str, name: str) -> None:
        response = self._request(
            "DELETE", self._path(namespace, kind, name),
            params={"propagationPolicy": "Background"},
        )
        self._expect(response, (200, 202, 404), f"deleting {kind}/{name}")

    def _create(self, namespace: str, kind: str, body: dict) -> None:
        response = self._request("POST", self._path(namespace, kind), json=body)
        self._expect(response, (200, 201), f"creating {kind}/{body['metadata']['name']}")

    def _list(self, namespace: str, kind: str, selector: str) -> list[dict]:
        response = self._request(
            "GET", self._path(namespace, kind), params={"labelSelector": selector}
        )
        return self._expect(response, (200,), f"listing {kind}").json().get("items") or []

    # -- rendering --------------------------------------------------------------

    @staticmethod
    def _labels(workload: Workload) -> dict[str, str]:
        return {
            **dict(workload.labels),
            MANAGED_BY: MANAGED_BY_VALUE,
            WORKLOAD_LABEL: workload.name,
        }

    @staticmethod
    def _annotations(workload: Workload) -> dict[str, str]:
        return {
            **dict(workload.annotations),
            GENERATION_ANNOTATION: str(workload.generation),
            "miragen.io/workload-class": workload.workload_class,
        }

    def _pod_spec(self, workload: Workload, *, restart_policy: str) -> dict:
        template = workload.template
        env = {
            **dict(workload.env),
            "AGENT_PROFILE": f"{PROFILE_DIR}/{PROFILE_KEY}",
            _INTERNAL_TOKEN_ENV: workload.internal_token,
        }
        if template.model_gateway_url:
            env["MIRAGEN_MODEL_GATEWAY_URL"] = template.model_gateway_url
            env["MIRAGEN_MODEL_TOKEN_DIR"] = template.model_token_dir
            if template.model_connection:
                env["MIRAGEN_MODEL_CONNECTION"] = template.model_connection
        mounts = [
            {
                "name": "profile",
                "mountPath": f"{PROFILE_DIR}/{PROFILE_KEY}",
                "subPath": PROFILE_KEY,
                "readOnly": True,
            }
        ]
        if workload.workload_class == "persistent":
            mounts.append({"name": "state", "mountPath": STATE_DIR})
        if template.model_gateway_url:
            # The model token's only home: memory, never the node's disk.
            mounts.append({"name": "model-token", "mountPath": template.model_token_dir})
        container_security = {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
        }
        resources: dict[str, Any] = {}
        if template.requests:
            resources["requests"] = dict(template.requests)
        if template.limits:
            resources["limits"] = dict(template.limits)
        container = {
            "name": CONTAINER,
            "image": template.image,
            "ports": [{"containerPort": AGENT_PORT}],
            "env": [{"name": key, "value": value} for key, value in sorted(env.items())],
            "resources": resources,
            "securityContext": container_security,
            "volumeMounts": mounts,
        }
        spec: dict[str, Any] = {
            "restartPolicy": restart_policy,
            "automountServiceAccountToken": False,
            "serviceAccountName": template.service_account_name,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": template.run_as_user,
                "runAsGroup": template.run_as_group,
                "fsGroup": template.fs_group,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [container],
            "volumes": [
                {
                    "name": "profile",
                    "configMap": {
                        "name": profile_configmap_name(workload.name),
                        "items": [{"key": PROFILE_KEY, "path": PROFILE_KEY}],
                    },
                }
            ],
        }
        if template.model_gateway_url:
            spec["volumes"].append({"name": "model-token",
                                    "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}})
        if template.runtime_class_name:
            spec["runtimeClassName"] = template.runtime_class_name
        if template.node_selector:
            spec["nodeSelector"] = dict(template.node_selector)
        if template.tolerations:
            spec["tolerations"] = [dict(t) for t in template.tolerations]
        if workload.workload_class == "persistent":
            # An empty volume hides the image's state directories; recreate
            # them as the agent's user before it starts.
            spec["initContainers"] = [
                {
                    "name": "state-dirs",
                    "image": template.image,
                    "command": ["mkdir", "-p", *(f"{STATE_DIR}/{d}" for d in _STATE_SUBDIRS)],
                    "securityContext": container_security,
                    "volumeMounts": [{"name": "state", "mountPath": STATE_DIR}],
                }
            ]
        return spec

    def render_configmap(self, workload: Workload) -> dict:
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": profile_configmap_name(workload.name),
                "labels": self._labels(workload),
                "annotations": self._annotations(workload),
            },
            "data": {PROFILE_KEY: workload.profile_yaml},
        }

    def render_job(self, workload: Workload) -> dict:
        labels = self._labels(workload)
        spec: dict[str, Any] = {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": workload.template.backstop_ttl_seconds,
            "template": {
                "metadata": {"labels": labels, "annotations": self._annotations(workload)},
                "spec": self._pod_spec(workload, restart_policy="Never"),
            },
        }
        if workload.deadline_seconds is not None:
            spec["activeDeadlineSeconds"] = workload.deadline_seconds
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": workload.name,
                "labels": labels,
                "annotations": self._annotations(workload),
            },
            "spec": spec,
        }

    def render_statefulset(self, workload: Workload) -> dict:
        labels = self._labels(workload)
        claim: dict[str, Any] = {
            "metadata": {"name": "state", "labels": labels},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": workload.template.storage_size}},
            },
        }
        if workload.template.storage_class_name:
            claim["spec"]["storageClassName"] = workload.template.storage_class_name
        return {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {
                "name": workload.name,
                "labels": labels,
                "annotations": self._annotations(workload),
            },
            "spec": {
                "replicas": 1 if workload.running else 0,
                "serviceName": workload.name,
                "selector": {"matchLabels": {WORKLOAD_LABEL: workload.name}},
                "template": {
                    "metadata": {"labels": labels, "annotations": self._annotations(workload)},
                    "spec": self._pod_spec(workload, restart_policy="Always"),
                },
                "volumeClaimTemplates": [claim],
                "persistentVolumeClaimRetentionPolicy": {
                    "whenDeleted": "Retain",
                    "whenScaled": "Retain",
                },
            },
        }

    # -- operations -------------------------------------------------------------

    def _put_configmap(self, workload: Workload) -> None:
        body = self.render_configmap(workload)
        name = body["metadata"]["name"]
        response = self._request("POST", self._path(workload.namespace, "configmaps"), json=body)
        if response.status_code == 409:
            response = self._request(
                "PUT", self._path(workload.namespace, "configmaps", name), json=body
            )
        self._expect(response, (200, 201), f"writing configmap/{name}")

    def apply(self, workload: Workload) -> None:
        """Make the cluster match this generation. Idempotent: applying the
        generation that is already there changes nothing, so a lease the hub
        offers again is safe to apply again."""
        if workload.workload_class == "ephemeral":
            self._apply_job(workload)
        else:
            self._apply_statefulset(workload)

    def _apply_job(self, workload: Workload) -> None:
        existing = self._get(workload.namespace, "jobs", workload.name)
        if not workload.running:
            self.delete(workload.namespace, workload.name, "ephemeral")
            return
        if existing is not None:
            annotations = (existing.get("metadata") or {}).get("annotations") or {}
            if annotations.get(GENERATION_ANNOTATION) == str(workload.generation):
                return
            # A Job's pod template is immutable: a new generation replaces it.
            self._delete(workload.namespace, "jobs", workload.name)
        self._put_configmap(workload)
        self._create(workload.namespace, "jobs", self.render_job(workload))

    def _apply_statefulset(self, workload: Workload) -> None:
        self._put_configmap(workload)
        body = self.render_statefulset(workload)
        existing = self._get(workload.namespace, "statefulsets", workload.name)
        if existing is None:
            self._create(workload.namespace, "statefulsets", body)
            return
        annotations = (existing.get("metadata") or {}).get("annotations") or {}
        if (
            annotations.get(GENERATION_ANNOTATION) == str(workload.generation)
            and (existing.get("spec") or {}).get("replicas") == body["spec"]["replicas"]
        ):
            return
        patch = {
            "metadata": {"annotations": body["metadata"]["annotations"]},
            "spec": {"replicas": body["spec"]["replicas"], "template": body["spec"]["template"]},
        }
        response = self._request(
            "PATCH",
            self._path(workload.namespace, "statefulsets", workload.name),
            json=patch,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        self._expect(response, (200,), f"patching statefulset/{workload.name}")

    def delete(self, namespace: str, name: str, workload_class: str, *, volumes: bool = False) -> None:
        if workload_class == "ephemeral":
            self._delete(namespace, "jobs", name)
        else:
            self._delete(namespace, "statefulsets", name)
            if volumes:
                self._delete(namespace, "persistentvolumeclaims", f"state-{name}-0")
        self._delete(namespace, "configmaps", profile_configmap_name(name))

    def _pods(self, namespace: str, name: str) -> list[dict]:
        return self._list(namespace, "pods", f"{WORKLOAD_LABEL}={name}")

    def status(self, namespace: str, name: str, workload_class: str) -> WorkloadStatus:
        kind = "jobs" if workload_class == "ephemeral" else "statefulsets"
        unit = self._get(namespace, kind, name)
        if unit is None:
            return WorkloadStatus(phase="absent")
        annotations = (unit.get("metadata") or {}).get("annotations") or {}
        raw_generation = annotations.get(GENERATION_ANNOTATION)
        generation = int(raw_generation) if raw_generation and raw_generation.isdigit() else None
        pods = self._pods(namespace, name)
        pod = pods[0] if pods else None
        pod_uid = (pod or {}).get("metadata", {}).get("uid")
        pod_ip = (pod or {}).get("status", {}).get("podIP")
        container = _container_state(pod)
        if workload_class == "ephemeral":
            status = unit.get("status") or {}
            if status.get("succeeded"):
                phase = "succeeded"
            elif status.get("failed"):
                phase = "failed"
            elif container.get("running") is not None:
                phase = "running"
            else:
                phase = "pending"
        else:
            if (unit.get("spec") or {}).get("replicas", 0) == 0:
                phase = "stopped"
            elif (unit.get("status") or {}).get("readyReplicas"):
                phase = "running"
            else:
                phase = "pending"
        terminated = container.get("terminated") or {}
        exit_code = terminated.get("exitCode")
        reason = (
            (terminated.get("reason") or _job_failure_reason(unit)) if phase == "failed" else None
        )
        return WorkloadStatus(
            phase=phase,
            generation=generation,
            started_at=(container.get("running") or terminated).get("startedAt"),
            finished_at=terminated.get("finishedAt"),
            exit_code=exit_code if isinstance(exit_code, int) else None,
            oom_killed=(terminated.get("reason") == "OOMKilled") if terminated else None,
            pod_uid=pod_uid,
            pod_ip=pod_ip,
            reason=reason,
        )

    def endpoint(self, namespace: str, name: str) -> str | None:
        for pod in self._pods(namespace, name):
            ip = (pod.get("status") or {}).get("podIP")
            if ip and (pod.get("status") or {}).get("phase") == "Running":
                return f"http://{ip}:{AGENT_PORT}"
        return None

    def internal_token(self, namespace: str, name: str, workload_class: str) -> str | None:
        """The agent API guard the spoke set, read back from the unit's own
        spec, so a restarted spoke can still reach agents it started."""
        kind = "jobs" if workload_class == "ephemeral" else "statefulsets"
        unit = self._get(namespace, kind, name)
        if unit is None:
            return None
        spec = unit["spec"]["template"]["spec"]
        for item in spec["containers"][0].get("env") or []:
            if item.get("name") == _INTERNAL_TOKEN_ENV:
                return item.get("value")
        return None

    def list_managed(self, namespace: str) -> list[ManagedWorkload]:
        selector = f"{MANAGED_BY}={MANAGED_BY_VALUE}"
        found: list[ManagedWorkload] = []
        for kind, workload_class in (("jobs", "ephemeral"), ("statefulsets", "persistent")):
            for item in self._list(namespace, kind, selector):
                metadata = item.get("metadata") or {}
                found.append(
                    ManagedWorkload(
                        name=metadata.get("name", ""),
                        namespace=namespace,
                        workload_class=workload_class,
                        labels=dict(metadata.get("labels") or {}),
                        annotations=dict(metadata.get("annotations") or {}),
                    )
                )
        return found


def _container_state(pod: dict | None) -> dict:
    if not pod:
        return {}
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    for item in statuses:
        if item.get("name") == CONTAINER:
            return item.get("state") or {}
    return {}


def _job_failure_reason(job: dict) -> str | None:
    for condition in (job.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return condition.get("reason")
    return None
