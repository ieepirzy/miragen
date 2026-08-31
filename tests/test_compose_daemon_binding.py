"""The daemon's in-container binding is pinned, not operator-supplied.

miragend's compose service loads the operator's env file wholesale so the
daemon can forward credentials it was never told about by name. That load
is indiscriminate: any variable in the operator's `.env` reaches the
daemon's own environment — including `MIRAGEND_PORT`, which in `ports:`
means the HOST publish port but which the daemon reads as its BIND port.

Production failure this pins: an operator publishing the daemon on 8183
also moved the daemon off 8000 inside its container, so the port EXPOSE
and HEALTHCHECK name was dead (container reported unhealthy) and every
client dialing `miragend:8000` was refused — mirarun surfaced it as
`503 lifecycle daemon request failed`, three layers from the cause.
"""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).parent.parent / "compose.yaml"
CONTAINER_PORT = "8000"


def _miragend() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["miragend"]


def test_bind_port_is_pinned_and_cannot_come_from_the_env_file():
    service = _miragend()
    environment = service["environment"]
    # `environment` overrides `env_file`, so a literal here is what makes the
    # operator's own MIRAGEND_PORT unable to reach the daemon as a bind port.
    assert str(environment["MIRAGEND_PORT"]) == CONTAINER_PORT
    assert "$" not in str(environment["MIRAGEND_PORT"]), (
        "an interpolated value would take the operator's MIRAGEND_PORT again "
        "and reintroduce the collision this pin exists to prevent"
    )
    assert str(environment["MIRAGEND_HOST"]) == "0.0.0.0"


def test_published_mapping_targets_the_pinned_container_port():
    # The host side stays operator-controlled; only the container side is
    # fixed. A mapping onto anything but the pinned port would publish a
    # closed socket.
    published = _miragend()["ports"]
    assert any(str(entry).endswith(f":{CONTAINER_PORT}") for entry in published), (
        f"expected a mapping onto container port {CONTAINER_PORT}, got {published}"
    )


def test_env_file_load_is_still_wholesale():
    # The pin above must not be "fixed" by dropping the wholesale load —
    # that load is what lets an operator add a credential without editing
    # this file (#74). The two coexist: load everything, pin what is ours.
    paths = {entry["path"] for entry in _miragend()["env_file"]}
    assert {".env", "stack.env"} <= paths
