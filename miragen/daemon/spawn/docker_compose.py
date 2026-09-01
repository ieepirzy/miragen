"""Docker Compose spawn driver — the substrate miragend has always used.

Extracted from `miragen.daemon.core.LifecycleCore` behavior-preservingly:
every method here is the same logic that used to live directly on
`LifecycleCore`, unchanged except for the exception vocabulary (driver
errors are `SpawnUnitNotFound`/`SpawnOperationFailed`, translated back to
`ContainerNotFound`/`ContainerOperationFailed` at LifecycleCore's call
sites) and reading `ServiceSpec` instead of building the compose service
dict inline.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from miragen.daemon.spawn.base import (
    ServiceSpec,
    SpawnOperationFailed,
    SpawnUnitNotFound,
)


class DockerComposeSpawnDriver:
    def __init__(
        self,
        workspace: Path,
        docker_client,
        *,
        runner: Callable = subprocess.run,
        not_found: type[Exception],
        sleep: Callable = time.sleep,
    ) -> None:
        self.workspace = Path(workspace)
        self.compose_file = self.workspace / "compose.yml"
        self._docker = docker_client
        self._run = runner
        self._sleep = sleep
        self._not_found = not_found

    # -- compose.yml (this driver's persisted service definitions) ----------

    def _read_yaml(self, path: Path) -> dict:
        with open(path) as f:
            return yaml.safe_load(f) or {}

    def _write_yaml(self, path: Path, data: dict) -> None:
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def _compose_load(self) -> dict:
        if self.compose_file.exists():
            return self._read_yaml(self.compose_file)
        return {
            "secrets": {},
            "services": {},
            "networks": {"miragen-net": {"external": True}},
        }

    # -- SpawnDriver ----------------------------------------------------------

    def ensure_ready(self) -> None:
        try:
            self._docker.networks.get("miragen-net")
        except self._not_found:
            self._docker.networks.create("miragen-net", driver="bridge", attachable=True)

    def define_service(self, name: str, spec: ServiceSpec) -> None:
        data = self._compose_load()
        data["networks"] = {"miragen-net": {"external": True}}
        data.setdefault("secrets", {}).update(
            {s: {"external": True} for s in spec.secret_names}
        )
        data.setdefault("services", {})[name] = {
            "image": spec.image,
            "container_name": name,
            "restart": "unless-stopped",
            "secrets": list(spec.secret_names),
            "environment": dict(spec.environment),
            "volumes": [f"./agents/{name}:/agent"],
            "networks": ["miragen-net"],
            "cpus": spec.cpu_limit,
            "mem_limit": spec.mem_limit,
            "labels": dict(spec.labels),
        }
        self._write_yaml(self.compose_file, data)

    def up(self, name: str) -> None:
        result = self._run(
            ["docker", "compose", "up", "-d", name],
            capture_output=True,
            text=True,
            cwd=self.workspace,
        )
        if result.returncode != 0:
            raise SpawnOperationFailed(
                f"container failed to start: {result.stderr.strip()}"
            )

    def remove_service(self, name: str) -> None:
        if not self.compose_file.exists():
            return
        data = self._compose_load()
        data.get("services", {}).pop(name, None)
        self._write_yaml(self.compose_file, data)

    def status(self, name: str) -> str:
        try:
            return self._docker.containers.get(name).status
        except self._not_found:
            return "not found"
        except Exception as exc:
            return f"error: {exc}"

    def logs(self, name: str, *, tail: int) -> str:
        try:
            logs = self._docker.containers.get(name).logs(
                tail=min(max(tail, 1), 1000), stream=False
            )
        except self._not_found:
            raise SpawnUnitNotFound(name) from None
        except Exception as exc:
            raise SpawnOperationFailed(str(exc)) from exc
        return logs.decode("utf-8", errors="replace")

    def restart(self, name: str) -> None:
        try:
            self._docker.containers.get(name).restart()
        except self._not_found:
            raise SpawnUnitNotFound(name) from None
        except Exception as exc:
            raise SpawnOperationFailed(str(exc)) from exc

    def stop(self, name: str) -> None:
        try:
            self._docker.containers.get(name).stop()
        except self._not_found:
            raise SpawnUnitNotFound(name) from None
        except Exception as exc:
            raise SpawnOperationFailed(str(exc)) from exc

    def teardown(self, name: str) -> None:
        try:
            container = self._docker.containers.get(name)
            container.stop()
            container.remove()
        except self._not_found:
            pass
        except Exception as exc:
            raise SpawnOperationFailed(str(exc)) from exc

    def current_image(self, name: str) -> str | None:
        if not self.compose_file.exists():
            return None
        service = (self._compose_load().get("services") or {}).get(name) or {}
        return service.get("image")

    def endpoint(self, name: str) -> str:
        # Reachable by any container on miragen-net (container-name DNS).
        return f"http://{name}:8000"
