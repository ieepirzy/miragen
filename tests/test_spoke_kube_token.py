"""miragen#224: the spoke keeps its rotating ServiceAccount token current,
and exits when the Kubernetes API has failed every call for too long."""

from __future__ import annotations

import os
import pathlib

import certifi
import httpx
import pytest

from miragen.daemon.spawn import kubernetes_workloads
from miragen.daemon.spawn.base import SpawnOperationFailed
from miragen.daemon.spawn.kubernetes_workloads import KubernetesWorkloadDriver
from miragen.daemon.spawn.serviceaccount import ServiceAccountTokenAuth
from miragen.daemon.spoke.loop import KubeOutage
from tests.test_spoke_pull_mode import World


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _rotate(path, token: str, *, keep_mtime: bool = False) -> None:
    before = path.stat()
    path.write_text(token)
    if keep_mtime:
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _api(accepted: set[str], seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        seen.append(bearer)
        if bearer not in accepted:
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(200, json={"items": []})
    return handler


def _driver(path, accepted, seen, clock=None) -> KubernetesWorkloadDriver:
    clock = clock or Clock()
    return KubernetesWorkloadDriver(
        base_url="https://k8s.invalid",
        auth=ServiceAccountTokenAuth(path, monotonic=clock),
        transport=httpx.MockTransport(_api(accepted, seen)),
        monotonic=clock,
    )


def test_a_rotated_token_file_is_used_on_the_next_call(tmp_path):
    path = tmp_path / "token"
    path.write_text("old\n")
    seen: list[str] = []
    driver = _driver(path, {"old", "new"}, seen)
    driver.list_managed("ns")
    before = len(seen)
    _rotate(path, "new")
    driver.list_managed("ns")
    assert set(seen[:before]) == {"old"} and set(seen[before:]) == {"new"}


def test_a_401_rereads_the_file_and_retries_once(tmp_path):
    # The file changed but its mtime did not, so only the 401 reveals it.
    path = tmp_path / "token"
    path.write_text("old")
    seen: list[str] = []
    driver = _driver(path, {"new"}, seen)
    _rotate(path, "new", keep_mtime=True)
    driver.list_managed("ns")
    assert seen[:2] == ["old", "new"]
    assert driver.failing_for() == 0.0


def test_a_401_with_an_unchanged_token_is_not_retried(tmp_path):
    path = tmp_path / "token"
    path.write_text("revoked")
    seen: list[str] = []
    with pytest.raises(SpawnOperationFailed):
        _driver(path, set(), seen).list_managed("ns")
    assert seen == ["revoked"]


def test_the_file_is_reread_every_minute_even_with_the_same_mtime(tmp_path):
    path = tmp_path / "token"
    path.write_text("old")
    clock = Clock()
    seen: list[str] = []
    driver = _driver(path, {"old", "new"}, seen, clock)
    _rotate(path, "new", keep_mtime=True)
    driver.list_managed("ns")
    clock.now = 61.0
    driver.list_managed("ns")
    assert seen[0] == "old" and seen[-1] == "new"


def test_incluster_config_reads_the_token_through_the_rotating_auth(tmp_path, monkeypatch):
    # The regression of miragen#224: from_incluster_config baked the token
    # read at start-up into a static header.
    (tmp_path / "token").write_text("t")
    (tmp_path / "ca.crt").write_text(pathlib.Path(certifi.where()).read_text())
    monkeypatch.setattr(kubernetes_workloads, "_INCLUSTER_DIR", tmp_path)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    driver = KubernetesWorkloadDriver.from_incluster_config()
    assert isinstance(driver._client.auth, ServiceAccountTokenAuth)
    assert "authorization" not in driver._client.headers


def test_the_outage_clock_starts_at_the_first_failure_and_clears_on_an_answer(tmp_path):
    path = tmp_path / "token"
    path.write_text("t")
    clock = Clock()
    accepted: set[str] = set()
    driver = _driver(path, accepted, [], clock)
    for _ in range(2):
        with pytest.raises(SpawnOperationFailed):
            driver.list_managed("ns")
        clock.now += 100
    assert driver.failing_for() == 200.0
    accepted.add("t")
    driver.list_managed("ns")
    assert driver.failing_for() == 0.0


def _paced(clock: Clock, limit: int, each=lambda: None):
    """should_stop for run_forever: every pass costs a minute (a long-poll
    or a backoff), and the loop is cut off after `limit` passes."""
    passes = iter(range(limit + 1))

    def should_stop() -> bool:
        clock.now += 60
        each()
        return next(passes) >= limit
    return should_stop


def test_the_spoke_exits_once_the_api_has_failed_past_the_bound():
    world = World(poll={"kube_outage_exit_seconds": 300})
    world.running()
    clock = Clock()
    world.spoke.driver._monotonic = clock
    world.k8s_fail.add("/")  # every API call fails from here on
    with pytest.raises(KubeOutage):
        world.spoke.run_forever(should_stop=_paced(clock, 50), sleep=lambda _: None)
    assert 300 <= clock.now <= 420


def test_a_spoke_whose_api_recovers_does_not_exit():
    world = World(poll={"kube_outage_exit_seconds": 300})
    world.running()
    clock = Clock()
    world.spoke.driver._monotonic = clock
    world.k8s_fail.add("/")

    def heal() -> None:
        if clock.now >= 240:
            world.k8s_fail.clear()

    world.spoke.run_forever(should_stop=_paced(clock, 12, heal), sleep=lambda _: None)
    assert world.spoke.driver.failing_for() == 0.0
