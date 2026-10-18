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

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import logging

import yaml

from miragen.daemon.spawn.base import (
    ServiceSpec,
    SpawnOperationFailed,
    SpawnUnitNotFound,
    UnitLifecycle,
)


logger = logging.getLogger(__name__)

# The network every agent joins unless the deployment names others
# (MIRAGEND_AGENT_NETWORKS). miragend creates it when it is missing.
DEFAULT_AGENT_NETWORK = "miragen-net"


# Where secret files sit inside an isolated agent container.
SECRET_FILES_DIR = "/run/miragen-secrets"
# Label on per-agent private networks; its value is the agent name.
AGENT_NETWORK_LABEL = "io.miragen.agent-network"
# Host bridge names of per-agent networks start with this, so one host rule
# (`-i lna+`) covers every lane bridge, including ones created later.
LANE_BRIDGE_PREFIX = "lna"
DEFAULT_TMPFS = (
    "/tmp:size=512m,mode=1777",
    "/run/engineer:uid=1000,gid=1000,mode=0700,size=16m",
)


@dataclass(frozen=True)
class AgentIsolation:
    """MIRAGEND_ISOLATED_AGENTS (the agents host, mirarun ADR-041 §8).

    - Each agent gets its own internal network, shared only with `peers`
      (container name -> network aliases there): the gateway, as the
      agent's API front and as `github-broker`. Agents never share a
      network with miragend or with each other (the configured networks,
      e.g. an egress network, must disable inter-container traffic).
    - Credentials are files under SECRET_FILES_DIR, not environment values.
    - Units are hardened: no-new-privileges, every capability dropped, a pid
      limit, and a read-only root filesystem with `tmpfs` scratch mounts.
    """

    peers: tuple[tuple[str, tuple[str, ...]], ...] = ()
    pids_limit: int = 512
    read_only: bool = True
    tmpfs: tuple[str, ...] = DEFAULT_TMPFS


def private_network(name: str) -> str:
    return f"miragen-agent-{name}"


def private_bridge(name: str) -> str:
    # Linux interface names are at most 15 characters.
    return LANE_BRIDGE_PREFIX + hashlib.sha256(name.encode()).hexdigest()[:10]


class AgentNetworkMissing(SpawnOperationFailed):
    """A network named in MIRAGEND_AGENT_NETWORKS does not exist. miragend
    never creates those: their options (internal, inter-container traffic)
    are the isolation, so the deployment that owns them declares them."""


class DockerComposeSpawnDriver:
    def __init__(
        self,
        workspace: Path,
        docker_client,
        *,
        runner: Callable = subprocess.run,
        not_found: type[Exception],
        sleep: Callable = time.sleep,
        networks: tuple[str, ...] = (DEFAULT_AGENT_NETWORK,),
        isolation: AgentIsolation | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.compose_file = self.workspace / "compose.yml"
        self._docker = docker_client
        self._run = runner
        self._sleep = sleep
        self._not_found = not_found
        # The networks each agent container joins, in order. The first is
        # the one miragend shares with its agents (container-name DNS for
        # endpoint()); the rest are the agent's own (e.g. an egress-only
        # network on the agents host, mirarun ADR-041).
        if not networks:
            raise ValueError("an agent must join at least one network")
        self.networks = tuple(networks)
        self.isolation = isolation

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
            "networks": self._networks_block(),
        }

    def _networks_block(self, data: dict | None = None) -> dict:
        block = {n: {"external": True} for n in self.networks}
        if self.isolation is not None and data is not None:
            for service in (data.get("services") or {}).values():
                for net in service.get("networks") or ():
                    block.setdefault(net, {"external": True})
        return block

    # -- isolation ------------------------------------------------------------

    def _secrets_dir(self, name: str) -> Path:
        # Outside agents/<name>: never under the agent's mount, the file API
        # or export.
        return self.workspace / "agent-secrets" / name

    def _write_secret_files(self, name: str, files: dict[str, str]) -> None:
        target = self._secrets_dir(name)
        shutil.rmtree(target, ignore_errors=True)  # a revoked grant leaves nothing
        target.mkdir(parents=True, mode=0o700)
        target.chmod(0o700)
        for var, value in files.items():
            path = target / var.lower()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(fd, "w") as handle:
                handle.write(value)

    def _read_secret_files(self, name: str, environment: dict[str, str]) -> dict[str, str]:
        files: dict[str, str] = {}
        for key, value in environment.items():
            if key.endswith("_FILE") and value.startswith(SECRET_FILES_DIR + "/"):
                try:
                    files[key[: -len("_FILE")]] = (
                        self._secrets_dir(name) / Path(value).name).read_text()
                except OSError:
                    continue
        return files

    def _ensure_private_network(self, name: str) -> str:
        network_name = private_network(name)
        try:
            network = self._docker.networks.get(network_name)
        except self._not_found:
            network = self._docker.networks.create(
                network_name,
                driver="bridge",
                internal=True,
                labels={AGENT_NETWORK_LABEL: name},
                options={"com.docker.network.bridge.name": private_bridge(name)},
            )
        self._connect_peers(network)
        return network_name

    def _connect_peers(self, network) -> None:
        for container, aliases in self.isolation.peers:
            try:
                network.connect(container, aliases=list(aliases))
            except Exception as exc:  # already connected, or the peer is not up
                if "already exists" not in str(exc) and "already attached" not in str(exc):
                    raise SpawnOperationFailed(
                        f"could not attach '{container}' to {network.name}: {exc}") from exc

    def reconnect_peers(self) -> None:
        """Re-attach the peers (the gateway) to every agent network. A
        recreated gateway container loses runtime network attachments."""
        if self.isolation is None:
            return
        for network in self._docker.networks.list(filters={"label": AGENT_NETWORK_LABEL}):
            self._connect_peers(network)

    def _remove_private_network(self, name: str) -> None:
        try:
            network = self._docker.networks.get(private_network(name))
        except self._not_found:
            return
        except Exception:
            return
        for container, _ in self.isolation.peers:
            try:
                network.disconnect(container, force=True)
            except Exception:
                pass
        try:
            network.remove()
        except Exception:
            pass

    # -- SpawnDriver ----------------------------------------------------------

    def ensure_ready(self) -> None:
        self._ensure_networks()
        try:
            self.reconnect_peers()
        except SpawnOperationFailed as exc:
            # The gateway may not be up yet at daemon start; every agent
            # start re-attaches it (up()), and that one is strict.
            logger.warning("could not re-attach agent network peers: %s", exc)

    def _ensure_networks(self) -> None:
        for network in self.networks:
            try:
                self._docker.networks.get(network)
            except self._not_found:
                if network != DEFAULT_AGENT_NETWORK:
                    raise AgentNetworkMissing(
                        f"agent network '{network}' (MIRAGEND_AGENT_NETWORKS) does not "
                        "exist; the deployment that owns it must create it first"
                    ) from None
                self._docker.networks.create(network, driver="bridge", attachable=True)

    def define_service(self, name: str, spec: ServiceSpec) -> None:
        if spec.secret_files and self.isolation is None:
            raise SpawnOperationFailed("secret files need MIRAGEND_ISOLATED_AGENTS")
        data = self._compose_load()
        data.setdefault("secrets", {}).update(
            {s: {"external": True} for s in spec.secret_names}
        )
        service = {
            "image": spec.image,
            "container_name": name,
            "restart": "unless-stopped",
            "secrets": list(spec.secret_names),
            "environment": dict(spec.environment),
            "volumes": [f"./agents/{name}:/agent"],
            "networks": list(self.networks),
            "cpus": spec.cpu_limit,
            "mem_limit": spec.mem_limit,
            "labels": dict(spec.labels),
        }
        if self.isolation is not None:
            service["networks"] = [self._ensure_private_network(name), *self.networks]
            self._write_secret_files(name, spec.secret_files)
            service["volumes"].append(f"./agent-secrets/{name}:{SECRET_FILES_DIR}:ro")
            service["security_opt"] = ["no-new-privileges:true"]
            service["cap_drop"] = ["ALL"]
            service["pids_limit"] = self.isolation.pids_limit
            if self.isolation.read_only:
                service["read_only"] = True
                service["tmpfs"] = list(self.isolation.tmpfs)
        data.setdefault("services", {})[name] = service
        data["networks"] = self._networks_block(data)
        self._write_yaml(self.compose_file, data)

    def up(self, name: str) -> None:
        if self.isolation is not None:
            # The gateway may have been recreated since the agent's network
            # was made; its attachment is what MiraRun and the broker use.
            self._ensure_private_network(name)
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
        if self.isolation is not None:
            data.get("networks", {}).pop(private_network(name), None)
            shutil.rmtree(self._secrets_dir(name), ignore_errors=True)
            self._remove_private_network(name)
        self._write_yaml(self.compose_file, data)

    def status(self, name: str) -> str:
        try:
            return self._docker.containers.get(name).status
        except self._not_found:
            return "not found"
        except Exception as exc:
            return f"error: {exc}"

    def lifecycle(self, name: str) -> UnitLifecycle | None:
        try:
            state = (self._docker.containers.get(name).attrs or {}).get("State")
        except Exception:
            # Display metadata only: a missing container or a Docker error
            # reports nothing rather than failing the agent listing.
            return None
        if not isinstance(state, dict):
            return None
        exit_code = state.get("ExitCode")
        oom_killed = state.get("OOMKilled")
        return UnitLifecycle(
            started_at=_docker_time(state.get("StartedAt")),
            finished_at=_docker_time(state.get("FinishedAt")),
            exit_code=exit_code if isinstance(exit_code, int) else None,
            oom_killed=oom_killed if isinstance(oom_killed, bool) else None,
        )

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

    def current_spec(self, name: str) -> ServiceSpec | None:
        if not self.compose_file.exists():
            return None
        service = (self._compose_load().get("services") or {}).get(name)
        if not isinstance(service, dict) or not service.get("image"):
            return None
        return ServiceSpec(
            image=service["image"],
            environment={str(k): str(v) for k, v in (service.get("environment") or {}).items()},
            labels=dict(service.get("labels") or {}),
            cpu_limit=str(service.get("cpus", "")),
            mem_limit=str(service.get("mem_limit", "")),
            workspace_dir=self.workspace / "agents" / name,
            secret_names=tuple(service.get("secrets") or ()),
            secret_files=self._read_secret_files(
                name, {str(k): str(v) for k, v in (service.get("environment") or {}).items()}),
        )

    def endpoint(self, name: str) -> str:
        # Reachable by any container on the first agent network
        # (container-name DNS), miragend included.
        return f"http://{name}:8000"


# Docker's zero time for a container that has never started or never stopped.
_DOCKER_ZERO_TIME = "0001-01-01T00:00:00Z"


def _docker_time(value) -> str | None:
    if not isinstance(value, str) or not value or value == _DOCKER_ZERO_TIME:
        return None
    return value
