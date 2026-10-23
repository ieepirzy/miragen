"""The spoke's own configuration: where its hub is, and what it will run.

This file is mounted into the spoke by the cluster's GitOps. It is the
local allowlist of ADR-045 §4: the hub can only ask a spoke for a namespace,
a workload class and a tenant this file allows, and only with the images
and templates this file names. Nothing here ever comes from the hub.

    hub:
      url: https://hub.example            # the MiraRun hub, reached outbound
      token_file: /var/run/spoke/token    # the spoke's credential, read once
    poll:
      wait_seconds: 25                    # the hub's long-poll on each claim (<= 25)
      interval_seconds: 5
      max_backoff_seconds: 60
      claim_limit: 8
    images:                               # every image a template may use
      - registry.example/miragen@sha256:<digest>
    classes:                              # one pod template per class
      ephemeral:
        image: registry.example/miragen@sha256:<digest>
        service_account_name: agent-sandbox
        runtime_class_name: gvisor
        node_selector: {pool: agent}
        tolerations: [{key: class, operator: Equal, value: agent, effect: NoSchedule}]
        requests: {cpu: "1", memory: 2Gi}
        limits: {cpu: "2", memory: 4Gi}
      persistent:
        ...                               # the same, plus storage_size
    namespaces:                           # what each namespace may run, and for whom
      agents-tenant-a:
        classes: [ephemeral]
        organizations: [<tenant uuid>]
    network_label_keys:                   # hub labels the spoke will set on a pod
      - access.mirarun.io/loimi
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from miragen.daemon.spawn.kubernetes_workloads import WORKLOAD_CLASSES, WorkloadTemplate

_DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_LABEL_KEY = re.compile(r"^([a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")


class SpokeConfigInvalid(Exception):
    """The spoke configuration is missing or unsafe."""


@dataclass(frozen=True)
class NamespacePolicy:
    classes: frozenset[str]
    organizations: frozenset[UUID]


@dataclass(frozen=True)
class SpokeConfig:
    hub_url: str
    token_file: Path
    classes: dict[str, WorkloadTemplate]
    namespaces: dict[str, NamespacePolicy]
    images: frozenset[str]
    network_label_keys: frozenset[str] = frozenset()
    interval_seconds: float = 5.0
    max_backoff_seconds: float = 60.0
    claim_limit: int = 8
    wait_seconds: int = 25
    agent_timeout_seconds: float = 30.0
    extra: dict[str, Any] = field(default_factory=dict)

    def token(self) -> str:
        try:
            value = self.token_file.read_text().strip()
        except OSError as exc:
            raise SpokeConfigInvalid(f"cannot read the spoke token file: {exc}") from exc
        if not value:
            raise SpokeConfigInvalid("the spoke token file is empty")
        return value

    def allows(self, namespace: str, workload_class: str, organization_id: UUID) -> str | None:
        """None when allowed; otherwise why not, for the `refused` report."""
        policy = self.namespaces.get(namespace)
        if policy is None:
            return "namespace is not served by this spoke"
        if workload_class not in policy.classes:
            return f"class '{workload_class}' is not allowed in this namespace"
        if organization_id not in policy.organizations:
            return "this namespace does not serve the intent's organization"
        if workload_class not in self.classes:
            return f"no template for class '{workload_class}'"
        return None


def load_config(path: str | Path) -> SpokeConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SpokeConfigInvalid(f"cannot read spoke config: {exc}") from exc
    if not isinstance(raw, dict):
        raise SpokeConfigInvalid("spoke config must be a mapping")
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> SpokeConfig:
    hub = _mapping(raw, "hub")
    url = str(hub.get("url") or "")
    if not url.startswith("https://") and not url.startswith("http://"):
        raise SpokeConfigInvalid("hub.url must be an http(s) URL")
    token_file = hub.get("token_file")
    if not token_file:
        raise SpokeConfigInvalid("hub.token_file is required; the token is never inline")

    images = frozenset(str(i) for i in raw.get("images") or ())
    if not images:
        raise SpokeConfigInvalid("images: at least one allowed image is required")

    classes: dict[str, WorkloadTemplate] = {}
    for name, body in _mapping(raw, "classes").items():
        if name not in WORKLOAD_CLASSES:
            raise SpokeConfigInvalid(f"classes.{name}: unknown workload class")
        if not isinstance(body, dict):
            raise SpokeConfigInvalid(f"classes.{name} must be a mapping")
        image = str(body.get("image") or "")
        if image not in images:
            raise SpokeConfigInvalid(f"classes.{name}.image is not in images")
        if not body.get("service_account_name"):
            raise SpokeConfigInvalid(f"classes.{name}.service_account_name is required")
        if name == "persistent" and not body.get("storage_size"):
            raise SpokeConfigInvalid("classes.persistent.storage_size is required")
        known = set(WorkloadTemplate.__dataclass_fields__)
        unknown = set(body) - known
        if unknown:
            raise SpokeConfigInvalid(f"classes.{name}: unknown keys {sorted(unknown)}")
        classes[name] = WorkloadTemplate(**body)

    namespaces: dict[str, NamespacePolicy] = {}
    for name, body in _mapping(raw, "namespaces").items():
        if not _DNS1123.fullmatch(name):
            raise SpokeConfigInvalid(f"namespaces.{name}: not a Kubernetes name")
        allowed = frozenset(body.get("classes") or ())
        if not allowed or not allowed <= set(WORKLOAD_CLASSES):
            raise SpokeConfigInvalid(f"namespaces.{name}.classes must name workload classes")
        try:
            organizations = frozenset(UUID(str(o)) for o in body.get("organizations") or ())
        except ValueError as exc:
            raise SpokeConfigInvalid(f"namespaces.{name}.organizations: {exc}") from exc
        if not organizations:
            raise SpokeConfigInvalid(f"namespaces.{name}.organizations must not be empty")
        namespaces[name] = NamespacePolicy(classes=allowed, organizations=organizations)
    if not namespaces:
        raise SpokeConfigInvalid("namespaces: at least one is required")

    label_keys = frozenset(str(k) for k in raw.get("network_label_keys") or ())
    for key in label_keys:
        if not _LABEL_KEY.fullmatch(key):
            raise SpokeConfigInvalid(f"network_label_keys: '{key}' is not a label key")

    poll = raw.get("poll") or {}
    return SpokeConfig(
        hub_url=url.rstrip("/"),
        token_file=Path(token_file),
        classes=classes,
        namespaces=namespaces,
        images=images,
        network_label_keys=label_keys,
        interval_seconds=float(poll.get("interval_seconds", 5)),
        max_backoff_seconds=float(poll.get("max_backoff_seconds", 60)),
        claim_limit=max(1, min(int(poll.get("claim_limit", 8)), 32)),
        wait_seconds=max(0, min(int(poll.get("wait_seconds", 25)), 25)),
        agent_timeout_seconds=float(raw.get("agent_timeout_seconds", 30)),
    )


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise SpokeConfigInvalid(f"{key} must be a mapping")
    return value
