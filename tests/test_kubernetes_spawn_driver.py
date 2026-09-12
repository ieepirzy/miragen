"""KubernetesSpawnDriver against a fake API server (httpx.MockTransport) —
no cluster, mirroring test_daemon_core.py's FakeDocker/RecordingRunner
pattern for the compose driver."""

import json
from pathlib import Path

import httpx
import pytest

from miragen.daemon.spawn.base import (
    ServiceSpec,
    SpawnOperationFailed,
    SpawnUnitNotFound,
)
from miragen.daemon.spawn.kubernetes import KubernetesSpawnDriver, _k8s_memory

NAMESPACE = "miragen-agents"


class FakeKubernetesAPI:
    def __init__(self) -> None:
        self.configmaps: dict[str, dict] = {}
        self.pods: dict[str, dict] = {}

    def _cm_path(self, name: str = "") -> str:
        return f"/api/v1/namespaces/{NAMESPACE}/configmaps" + (f"/{name}" if name else "")

    def _pod_path(self, name: str = "") -> str:
        return f"/api/v1/namespaces/{NAMESPACE}/pods" + (f"/{name}" if name else "")

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == self._cm_path():
            if method == "POST":
                body = json.loads(request.content)
                name = body["metadata"]["name"]
                if name in self.configmaps:
                    return httpx.Response(409, json={"reason": "AlreadyExists"})
                self.configmaps[name] = body
                return httpx.Response(201, json=body)

        if path.startswith(self._cm_path() + "/"):
            name = path.rsplit("/", 1)[-1]
            if method == "GET":
                if name not in self.configmaps:
                    return httpx.Response(404, json={"reason": "NotFound"})
                return httpx.Response(200, json=self.configmaps[name])
            if method == "DELETE":
                existed = self.configmaps.pop(name, None) is not None
                return httpx.Response(200 if existed else 404, json={})

        if path == self._pod_path():
            if method == "POST":
                body = json.loads(request.content)
                name = body["metadata"]["name"]
                body.setdefault("status", {"containerStatuses": []})
                self.pods[name] = body
                return httpx.Response(201, json=body)

        if path.startswith(self._pod_path() + "/"):
            rest = path[len(self._pod_path() + "/") :]
            if rest.endswith("/log"):
                name = rest[: -len("/log")]
                pod = self.pods.get(name)
                if pod is None:
                    return httpx.Response(404, json={"reason": "NotFound"})
                return httpx.Response(200, text=f"log line for {name}")
            name = rest
            if method == "GET":
                if name not in self.pods:
                    return httpx.Response(404, json={"reason": "NotFound"})
                return httpx.Response(200, json=self.pods[name])
            if method == "DELETE":
                existed = self.pods.pop(name, None) is not None
                return httpx.Response(200 if existed else 404, json={})

        return httpx.Response(404, json={"reason": "NotFound"})

    # -- test helpers ---------------------------------------------------------

    def set_container_state(self, name: str, state: dict) -> None:
        self.pods[name]["status"] = {"containerStatuses": [{"state": state}]}

    def set_pod_ip(self, name: str, ip: str) -> None:
        self.pods[name].setdefault("status", {})["podIP"] = ip


@pytest.fixture
def fake_api():
    return FakeKubernetesAPI()


@pytest.fixture
def driver(fake_api):
    transport = httpx.MockTransport(fake_api.handler)
    return KubernetesSpawnDriver(
        base_url="https://kubernetes.default.svc",
        namespace=NAMESPACE,
        token="fake-token",
        transport=transport,
    )


def _spec(tmp_path: Path, **overrides) -> ServiceSpec:
    workspace = tmp_path / "agents" / overrides.get("_name", "alpha")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "agent.yaml").write_text("name: alpha\n")
    (workspace / "tools.py").write_text("# tools\n")
    return ServiceSpec(
        image=overrides.get("image", "ghcr.io/example/miragen:test"),
        environment=overrides.get("environment", {"AGENT_PROFILE": "agent.yaml"}),
        labels=overrides.get("labels", {"io.miragen.agent": "true"}),
        cpu_limit=overrides.get("cpu_limit", "1.0"),
        mem_limit=overrides.get("mem_limit", "512m"),
        workspace_dir=workspace,
        secret_names=overrides.get("secret_names", ()),
    )


class TestMemoryTranslation:
    def test_docker_style_suffixes_map_to_kubernetes_binary_units(self):
        assert _k8s_memory("512m") == "512Mi"
        assert _k8s_memory("1g") == "1Gi"
        assert _k8s_memory("2048k") == "2048Ki"
        assert _k8s_memory("1024b") == "1024"

    def test_unrecognized_values_pass_through(self):
        assert _k8s_memory("512Mi") == "512Mi"
        assert _k8s_memory("") == ""


class TestDefineAndUp:
    def test_define_service_writes_a_configmap_with_workspace_contents(
        self, driver, fake_api, tmp_path
    ):
        driver.define_service("alpha", _spec(tmp_path))
        cm = fake_api.configmaps["alpha"]
        assert cm["data"]["agent.yaml"] == "name: alpha\n"
        assert cm["data"]["tools.py"] == "# tools\n"
        record = json.loads(cm["data"]["spec.json"])
        assert record["image"] == "ghcr.io/example/miragen:test"

    def test_define_service_replaces_an_existing_configmap(
        self, driver, fake_api, tmp_path
    ):
        driver.define_service("alpha", _spec(tmp_path, image="first"))
        driver.define_service("alpha", _spec(tmp_path, image="second"))
        record = json.loads(fake_api.configmaps["alpha"]["data"]["spec.json"])
        assert record["image"] == "second"

    def test_define_service_rejects_a_name_invalid_for_kubernetes(self, driver, tmp_path):
        with pytest.raises(SpawnOperationFailed):
            driver.define_service("has_underscore", _spec(tmp_path))

    def test_up_creates_a_pod_from_the_stored_definition(self, driver, fake_api, tmp_path):
        driver.define_service("alpha", _spec(tmp_path))
        driver.up("alpha")
        pod = fake_api.pods["alpha"]
        container = pod["spec"]["containers"][0]
        assert container["image"] == "ghcr.io/example/miragen:test"
        assert container["resources"]["limits"]["memory"] == "512Mi"
        assert {"name": "AGENT_PROFILE", "value": "agent.yaml"} in container["env"]

    def test_up_without_a_prior_define_service_fails(self, driver):
        with pytest.raises(SpawnOperationFailed):
            driver.up("never-defined")

    def test_up_recreates_an_existing_pod(self, driver, fake_api, tmp_path):
        driver.define_service("alpha", _spec(tmp_path, image="first"))
        driver.up("alpha")
        driver.define_service("alpha", _spec(tmp_path, image="second"))
        driver.up("alpha")
        assert fake_api.pods["alpha"]["spec"]["containers"][0]["image"] == "second"


class TestStatus:
    def test_status_of_an_unknown_agent_is_not_found(self, driver):
        assert driver.status("ghost") == "not found"

    def test_status_before_kubelet_reports_container_state_is_pending(
        self, driver, fake_api, tmp_path
    ):
        driver.define_service("alpha", _spec(tmp_path))
        driver.up("alpha")
        assert driver.status("alpha") == "pending"

    def test_status_running(self, driver, fake_api, tmp_path):
        driver.define_service("alpha", _spec(tmp_path))
        driver.up("alpha")
        fake_api.set_container_state("alpha", {"running": {}})
        assert driver.status("alpha") == "running"

    def test_status_terminated_is_exited(self, driver, fake_api, tmp_path):
        driver.define_service("alpha", _spec(tmp_path))
        driver.up("alpha")
        fake_api.set_container_state("alpha", {"terminated": {"exitCode": 1}})
        assert driver.status("alpha") == "exited"

    def test_status_crash_loop_is_restarting(self, driver, fake_api, tmp_path):
        driver.define_service("alpha", _spec(tmp_path))
        driver.up("alpha")
        fake_api.set_container_state(
            "alpha", {"waiting": {"reason": "CrashLoopBackOff"}}
        )
        assert driver.status("alpha") == "restarting"


class TestLifecycle:
    def test_restart_of_a_missing_pod_raises_not_found(self, driver):
        with pytest.raises(SpawnUnitNotFound):
            driver.restart("ghost")

    def test_restart_recreates_the_pod(self, driver, fake_api, tmp_path):
        driver.define_service("alpha", _spec(tmp_path))
        driver.up("alpha")
        fake_api.set_container_state("alpha", {"running": {}})
        driver.restart("alpha")
        assert driver.status("alpha") == "pending"  # freshly recreated

    def test_stop_of_a_missing_pod_raises_not_found(self, driver):
        with pytest.raises(SpawnUnitNotFound):
            driver.stop("ghost")

    def test_stop_deletes_the_pod_but_keeps_the_configmap(self, driver, fake_api, tmp_path):
        driver.define_service("alpha", _spec(tmp_path))
        driver.up("alpha")
        driver.stop("alpha")
        assert "alpha" not in fake_api.pods
        assert "alpha" in fake_api.configmaps

    def test_teardown_is_idempotent(self, driver, fake_api, tmp_path):
        driver.teardown("ghost")  # never existed — no raise
        driver.define_service("alpha", _spec(tmp_path))
        driver.up("alpha")
        driver.teardown("alpha")
        driver.teardown("alpha")  # already gone — still no raise
        assert "alpha" not in fake_api.pods

    def test_remove_service_deletes_the_configmap(self, driver, fake_api, tmp_path):
        driver.define_service("alpha", _spec(tmp_path))
        driver.remove_service("alpha")
        assert "alpha" not in fake_api.configmaps
        driver.remove_service("alpha")  # idempotent


class TestLogsAndImageAndEndpoint:
    def test_logs_of_a_missing_pod_raises_not_found(self, driver):
        with pytest.raises(SpawnUnitNotFound):
            driver.logs("ghost", tail=10)

    def test_logs_returns_pod_log_text(self, driver, fake_api, tmp_path):
        driver.define_service("alpha", _spec(tmp_path))
        driver.up("alpha")
        assert "log line for alpha" in driver.logs("alpha", tail=10)

    def test_current_image_reads_back_the_stored_definition(self, driver, tmp_path):
        assert driver.current_image("ghost") is None
        driver.define_service("alpha", _spec(tmp_path, image="pinned@sha256:" + "a" * 64))
        assert driver.current_image("alpha") == "pinned@sha256:" + "a" * 64

    def test_endpoint_uses_the_live_pod_ip(self, driver, fake_api, tmp_path):
        driver.define_service("alpha", _spec(tmp_path))
        driver.up("alpha")
        fake_api.set_pod_ip("alpha", "10.0.0.7")
        assert driver.endpoint("alpha") == "http://10.0.0.7:8000"

    def test_endpoint_before_a_pod_has_an_ip_is_a_placeholder(self, driver):
        assert driver.endpoint("ghost").startswith("http://ghost")
