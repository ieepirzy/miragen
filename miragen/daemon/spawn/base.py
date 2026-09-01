"""The spawn-substrate seam: `ServiceSpec` in, a running unit out.

A `SpawnDriver` is the only thing in miragend that knows how a managed
agent's process actually comes to exist — Docker Compose today, Kubernetes
alongside it. Everything substrate-agnostic (naming, profile/contract
validation, credential-env assembly, boot-watching, rollback ordering) stays
in `LifecycleCore`; a driver only ever sees a fully-resolved `ServiceSpec`
and the agent's `name`, never a raw profile or the daemon's environment.

Exceptions are driver-generic (`SpawnUnitNotFound`, `SpawnOperationFailed`)
so a driver never needs to import `miragen.daemon.core`'s exception
vocabulary — `LifecycleCore` translates these at its own call sites into the
HTTP-mapped errors its callers already expect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """Everything a driver needs to define one agent's compute unit.

    Fully resolved by `LifecycleCore` before it ever reaches a driver — no
    field here requires reading the daemon's environment, the agent's
    profile, or any substrate-specific convention.
    """

    image: str
    environment: dict[str, str]
    labels: dict[str, str]
    cpu_limit: str
    mem_limit: str
    workspace_dir: Path
    secret_names: tuple[str, ...] = field(default_factory=tuple)


class SpawnDriverError(Exception):
    """Base for all driver-raised errors."""


class SpawnUnitNotFound(SpawnDriverError):
    """No running unit exists for this agent name."""


class SpawnOperationFailed(SpawnDriverError):
    """The substrate rejected or failed an operation."""


class SpawnDriver(Protocol):
    """One implementation per compute substrate. See module docstring."""

    def ensure_ready(self) -> None:
        """Prepare whatever shared infrastructure the substrate needs
        (idempotent) — e.g. the Docker bridge network. A substrate whose
        shared infrastructure is provisioned out of band (Kubernetes:
        Namespace/NetworkPolicy via GitOps) may no-op."""
        ...

    def define_service(self, name: str, spec: ServiceSpec) -> None:
        """Persist `spec` as the unit's definition, without starting it."""
        ...

    def up(self, name: str) -> None:
        """Bring the already-defined unit for `name` to a running state."""
        ...

    def remove_service(self, name: str) -> None:
        """Forget `name`'s definition. Idempotent — a no-op if absent."""
        ...

    def status(self, name: str) -> str:
        """The unit's current status string, or 'not found'/'error: ...'."""
        ...

    def logs(self, name: str, *, tail: int) -> str:
        """The unit's last `tail` log lines. Raises SpawnUnitNotFound if
        no unit exists for `name`."""
        ...

    def restart(self, name: str) -> None:
        ...

    def stop(self, name: str) -> None:
        ...

    def teardown(self, name: str) -> None:
        """Stop and remove the running unit, if any. Idempotent — a no-op
        if no unit exists (distinct from remove_service: this only touches
        the running unit, not its definition)."""
        ...

    def current_image(self, name: str) -> str | None:
        """The image reference `name`'s definition is currently pinned to,
        or None if `name` has no definition yet."""
        ...

    def endpoint(self, name: str) -> str:
        """The base URL this daemon reaches the agent's own HTTP app at."""
        ...
