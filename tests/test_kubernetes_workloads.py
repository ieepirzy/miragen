"""KubernetesWorkloadDriver against a fake API server (httpx.MockTransport),
the same pattern as test_kubernetes_spawn_driver.py: no cluster."""

from __future__ import annotations

import json
from typing import ClassVar
from urllib.parse import parse_qs

import httpx
import pytest

from miragen.daemon.spawn.base import SpawnOperationFailed
from miragen.daemon.spawn.kubernetes_workloads import (
    GENERATION_ANNOTATION,
    KubernetesWorkloadDriver,
    Workload,
    WorkloadTemplate,
    profile_configmap_name,
)

NS = "agents-sandbox"


class FakeKubernetes:
    """Just enough of the API server: configmaps, jobs, statefulsets, pods,
    persistentvolumeclaims; create, get, put, merge-patch, delete, list by a
    single `key=value` label selector."""

    KINDS: ClassVar[dict[str, str]] = {
        "configmaps": "/api/v1",
        "pods": "/api/v1",
        "persistentvolumeclaims": "/api/v1",
        "jobs": "/apis/batch/v1",
        "statefulsets": "/apis/apps/v1",
    }

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, dict]] = {kind: {} for kind in self.KINDS}
        self.calls: list[tuple[str, str]] = []

    def _parse(self, path: str) -> tuple[str, str]:
        for kind, group in self.KINDS.items():
            prefix = f"{group}/namespaces/{NS}/{kind}"
            if path == prefix:
                return kind, ""
            if path.startswith(prefix + "/"):
                return kind, path[len(prefix) + 1:]
        raise AssertionError(f"unexpected path {path}")

    def handler(self, request: httpx.Request) -> httpx.Response:
        kind, name = self._parse(request.url.path)
        self.calls.append((request.method, f"{kind}/{name}"))
        store = self.objects[kind]
        if request.method == "POST":
            body = json.loads(request.content)
            key = body["metadata"]["name"]
            if key in store:
                return httpx.Response(409, json={"reason": "AlreadyExists"})
            store[key] = body
            return httpx.Response(201, json=body)
        if request.method == "GET" and not name:
            selector = parse_qs(request.url.query.decode()).get("labelSelector", [""])[0]
            label, _, value = selector.partition("=")
            items = [
                obj for obj in store.values()
                if (obj["metadata"].get("labels") or {}).get(label) == value
            ]
            return httpx.Response(200, json={"items": items})
        if request.method == "GET":
            if name not in store:
                return httpx.Response(404, json={"reason": "NotFound"})
            return httpx.Response(200, json=store[name])
        if request.method == "PUT":
            store[name] = json.loads(request.content)
            return httpx.Response(200, json=store[name])
        if request.method == "PATCH":
            if name not in store:
                return httpx.Response(404, json={})
            patch = json.loads(request.content)
            obj = store[name]
            obj["metadata"].setdefault("annotations", {}).update(
                patch.get("metadata", {}).get("annotations", {})
            )
            obj["spec"].update(patch.get("spec", {}))
            return httpx.Response(200, json=obj)
        if request.method == "DELETE":
            existed = store.pop(name, None) is not None
            return httpx.Response(200 if existed else 404, json={})
        raise AssertionError(request.method)

    def add_pod(self, workload: str, *, phase: str, ip: str = "10.0.0.7", state: dict | None = None):
        self.objects["pods"][f"{workload}-pod"] = {
            "metadata": {"name": f"{workload}-pod", "uid": "pod-uid-1",
                         "labels": {"miragen.io/workload": workload}},
            "status": {
                "phase": phase,
                "podIP": ip,
                "containerStatuses": [{"name": "agent", "state": state or {}}],
            },
        }


def _driver(fake: FakeKubernetes) -> KubernetesWorkloadDriver:
    return KubernetesWorkloadDriver(
        base_url="https://k8s.invalid", token="t", transport=httpx.MockTransport(fake.handler)
    )


TEMPLATE = WorkloadTemplate(
    image="ghcr.io/example/miragen@sha256:" + "a" * 64,
    service_account_name="agent-sandbox",
    runtime_class_name="gvisor",
    node_selector={"pool": "agent"},
    tolerations=[{"key": "class", "operator": "Equal", "value": "agent", "effect": "NoSchedule"}],
    requests={"cpu": "1", "memory": "2Gi"},
    limits={"cpu": "2", "memory": "4Gi"},
    storage_size="10Gi",
)


def _workload(**changes) -> Workload:
    fields: dict = dict(  # noqa: C408 - keyword form mirrors Workload's fields
        name="mw-" + "1" * 32,
        namespace=NS,
        workload_class="ephemeral",
        template=TEMPLATE,
        profile_yaml="name: analyst\n",
        generation=1,
        running=True,
        internal_token="internal-1",
        labels={"miragen.io/intent": "1" * 32},
        env={"MIRARUN_PRODUCT": "apogee"},
        deadline_seconds=3600,
    )
    fields.update(changes)
    return Workload(**fields)


def test_ephemeral_is_a_job_with_the_spokes_template():
    fake = FakeKubernetes()
    _driver(fake).apply(_workload())
    job = fake.objects["jobs"]["mw-" + "1" * 32]
    spec = job["spec"]
    pod = spec["template"]["spec"]
    container = pod["containers"][0]
    assert spec["activeDeadlineSeconds"] == 3600
    assert spec["ttlSecondsAfterFinished"] >= 24 * 3600
    assert spec["backoffLimit"] == 0
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["runtimeClassName"] == "gvisor"
    assert pod["nodeSelector"] == {"pool": "agent"}
    assert pod["tolerations"][0]["key"] == "class"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["resources"]["limits"] == {"cpu": "2", "memory": "4Gi"}
    # G1: the profile is one file by subPath, never mounted over /agent.
    mounts = container["volumeMounts"]
    assert [m["mountPath"] for m in mounts] == ["/etc/miragen/profile/agent.yaml"]
    assert mounts[0]["subPath"] == "agent.yaml" and mounts[0]["readOnly"] is True
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["AGENT_PROFILE"] == "/etc/miragen/profile/agent.yaml"
    # G2: the profile ConfigMap holds the profile and nothing else.
    configmap = fake.objects["configmaps"][profile_configmap_name("mw-" + "1" * 32)]
    assert set(configmap["data"]) == {"agent.yaml"}


def test_credential_looking_env_is_refused():
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "DB_PASSWORD", "GH_SECRET"):
        with pytest.raises(SpawnOperationFailed):
            _workload(env={name: "x"})


def test_reapplying_the_same_generation_changes_nothing():
    fake = FakeKubernetes()
    driver = _driver(fake)
    driver.apply(_workload())
    before = list(fake.calls)
    driver.apply(_workload())
    writes = [c for c in fake.calls[len(before):] if c[0] in ("POST", "PUT", "PATCH", "DELETE")]
    assert writes == []


def test_a_new_generation_replaces_the_job():
    fake = FakeKubernetes()
    driver = _driver(fake)
    driver.apply(_workload())
    driver.apply(_workload(generation=2, profile_yaml="name: analyst2\n"))
    job = fake.objects["jobs"]["mw-" + "1" * 32]
    assert job["metadata"]["annotations"][GENERATION_ANNOTATION] == "2"


def test_ephemeral_not_running_is_deleted():
    fake = FakeKubernetes()
    driver = _driver(fake)
    driver.apply(_workload())
    driver.apply(_workload(generation=2, running=False))
    assert fake.objects["jobs"] == {}
    assert fake.objects["configmaps"] == {}


def test_persistent_is_a_statefulset_with_a_volume_and_scales_to_zero():
    fake = FakeKubernetes()
    driver = _driver(fake)
    driver.apply(_workload(workload_class="persistent", deadline_seconds=None))
    sts = fake.objects["statefulsets"]["mw-" + "1" * 32]
    assert sts["spec"]["replicas"] == 1
    claim = sts["spec"]["volumeClaimTemplates"][0]
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
    pod = sts["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Always"
    paths = [m["mountPath"] for m in pod["containers"][0]["volumeMounts"]]
    assert paths == ["/etc/miragen/profile/agent.yaml", "/agent"]
    assert pod["initContainers"][0]["name"] == "state-dirs"
    driver.apply(_workload(workload_class="persistent", deadline_seconds=None,
                           generation=2, running=False))
    assert fake.objects["statefulsets"]["mw-" + "1" * 32]["spec"]["replicas"] == 0


def test_persistent_needs_storage():
    with pytest.raises(SpawnOperationFailed):
        _workload(
            workload_class="persistent",
            template=WorkloadTemplate(image="i", service_account_name="sa"),
        )


def test_job_status_reads_the_terminated_container():
    fake = FakeKubernetes()
    driver = _driver(fake)
    name = "mw-" + "1" * 32
    driver.apply(_workload())
    fake.objects["jobs"][name]["status"] = {"failed": 1}
    fake.add_pod(name, phase="Failed", state={"terminated": {
        "exitCode": 137, "reason": "OOMKilled",
        "startedAt": "2026-10-23T10:00:00Z", "finishedAt": "2026-10-23T10:05:00Z"}})
    status = driver.status(NS, name, "ephemeral")
    assert status.phase == "failed" and status.terminal
    assert status.exit_code == 137 and status.oom_killed is True
    assert status.generation == 1
    assert status.pod_uid == "pod-uid-1"


def test_endpoint_and_internal_token_are_read_back():
    fake = FakeKubernetes()
    driver = _driver(fake)
    name = "mw-" + "1" * 32
    driver.apply(_workload())
    assert driver.endpoint(NS, name) is None
    fake.add_pod(name, phase="Running", state={"running": {"startedAt": "x"}})
    assert driver.endpoint(NS, name) == "http://10.0.0.7:8000"
    assert driver.internal_token(NS, name, "ephemeral") == "internal-1"


def test_list_managed_finds_both_classes():
    fake = FakeKubernetes()
    driver = _driver(fake)
    driver.apply(_workload())
    driver.apply(_workload(name="mw-" + "2" * 32, workload_class="persistent",
                           deadline_seconds=None))
    found = {(w.name, w.workload_class) for w in driver.list_managed(NS)}
    assert found == {("mw-" + "1" * 32, "ephemeral"), ("mw-" + "2" * 32, "persistent")}
