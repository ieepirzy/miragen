"""The miragend HTTP surface: auth, error mapping, and the schedule routes,
over a real LifecycleCore wired to fakes (see test_daemon_core)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from miragen.daemon.api import DAEMON_CAPABILITIES, create_app
from miragen.daemon.core import LifecycleCore
from miragen.daemon.schedules import ScheduleStore

from tests.test_daemon_core import (
    VALID_YAML,
    FakeDocker,
    FakeNotFound,
    RecordingRunner,
)


def _make_core(tmp_path, **kw):
    docker_client = FakeDocker()
    runner = RecordingRunner()
    runner.on_up = lambda name: docker_client.add(name)
    core = LifecycleCore(
        tmp_path,
        docker_client,
        base_image="ghcr.io/example/miragen:test",
        environ={},
        runner=runner,
        not_found=FakeNotFound,
        **kw,
    )
    return core


class FakeJob:
    def __init__(self, job_id, args, run_date):
        self.id = job_id
        self.args = args
        self.next_run_time = run_date


class FakeScheduler:
    def __init__(self):
        self.jobs: dict[str, FakeJob] = {}
        self.started = False

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        self.started = False

    def add_job(self, func, trigger, args, id, replace_existing, misfire_grace_time):
        self.jobs[id] = FakeJob(id, args, trigger.run_date)

    def get_jobs(self):
        return list(self.jobs.values())

    def remove_job(self, job_id):
        from apscheduler.jobstores.base import JobLookupError

        if job_id not in self.jobs:
            raise JobLookupError(job_id)
        del self.jobs[job_id]


@pytest.fixture
def client(tmp_path):
    docker_client = FakeDocker()
    runner = RecordingRunner()
    runner.on_up = lambda name: docker_client.add(name)
    core = LifecycleCore(
        tmp_path,
        docker_client,
        base_image="ghcr.io/example/miragen:test",
        environ={},
        runner=runner,
        not_found=FakeNotFound,
    )
    schedules = ScheduleStore(FakeScheduler())
    app = create_app(core, schedules, token="daemon-token")
    test_client = TestClient(app)
    test_client.headers["Authorization"] = "Bearer daemon-token"
    return test_client


def _yaml(name="alpha"):
    return VALID_YAML.format(name=name)


# ── health & auth ────────────────────────────────────────────────────────────


def test_health_is_unguarded_and_advertises_capabilities(client):
    resp = client.get("/health", headers={"Authorization": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "miragend"
    assert body["capabilities"] == list(DAEMON_CAPABILITIES)


def test_missing_or_wrong_token_is_401(client):
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}):
        resp = client.get("/agents", headers=headers)
        assert resp.status_code == 401
        assert resp.json()["code"] == "unauthorized"


def test_empty_token_disables_the_guard(tmp_path):
    docker_client = FakeDocker()
    runner = RecordingRunner()
    core = LifecycleCore(
        tmp_path,
        docker_client,
        base_image="img",
        environ={},
        runner=runner,
        not_found=FakeNotFound,
    )
    unguarded = TestClient(create_app(core, token=""))
    assert unguarded.get("/agents").status_code == 200


# ── non-ASCII tokens (hmac.compare_digest raises on non-ASCII str) ─────────
#
# hmac.compare_digest(a, b) raises TypeError when either str operand
# contains a non-ASCII character. A plain TestClient re-encodes header
# values as ASCII/latin-1 before they ever reach the app, so it can't
# reproduce a raw non-ASCII Authorization header — httpx's AsyncClient over
# ASGITransport is used instead so real non-ASCII bytes make it through.


async def test_non_ascii_authorization_header_is_401_not_500(tmp_path):
    core = _make_core(tmp_path)
    app = create_app(core, token="daemon-token")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(
            "/agents",
            headers=[(b"authorization", "Bearer café-token".encode("utf-8"))],
        )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_non_ascii_configured_token_does_not_crash(tmp_path):
    core = _make_core(tmp_path)
    app = create_app(core, token="tökén-with-ünicode")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(
            "/agents", headers=[(b"authorization", b"Bearer wrong")]
        )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_matching_non_ascii_token_authenticates(tmp_path):
    core = _make_core(tmp_path)
    token = "tökén-with-ünicode"
    app = create_app(core, token=token)
    transport = ASGITransport(app=app)
    # Starlette decodes raw header bytes as latin-1 (ISO-8859-1, per the HTTP
    # spec), so send the header pre-encoded the same way — that's what makes
    # request.headers.get(...) come back byte-identical to `token` here (all
    # of its characters happen to be in the latin-1 range).
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(
            "/agents",
            headers=[(b"authorization", f"Bearer {token}".encode("latin-1"))],
        )
    assert resp.status_code == 200


# ── lifecycle over HTTP ──────────────────────────────────────────────────────


def test_agent_crud_flow(client):
    resp = client.post("/agents", json={"name": "alpha", "yaml_source": _yaml()})
    assert resp.status_code == 201
    assert resp.json() == {"name": "alpha", "status": "running"}

    listing = client.get("/agents").json()
    assert listing["count"] == 1
    assert listing["agents"][0]["endpoint"] == "http://alpha:8000"

    detail = client.get("/agents/alpha").json()
    assert "mode: interactive" in detail["yaml"]

    assert client.post("/agents/alpha/restart").status_code == 200
    assert client.post("/agents/alpha/stop").status_code == 200
    assert client.get("/agents/alpha/logs", params={"tail": 5}).status_code == 200

    resp = client.delete("/agents/alpha")
    assert resp.json() == {"name": "alpha", "deleted": True}
    assert client.get("/agents").json()["count"] == 0


def test_error_mapping(client):
    assert client.get("/agents/ghost").status_code == 404
    assert client.get("/agents/ghost").json()["code"] == "agent_not_found"

    client.post("/agents", json={"name": "alpha", "yaml_source": _yaml()})
    dup = client.post("/agents", json={"name": "alpha", "yaml_source": _yaml()})
    assert dup.status_code == 409
    assert dup.json()["code"] == "agent_exists"

    bad = client.post(
        "/agents", json={"name": "beta", "yaml_source": "mode: interactive\n"}
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "validation_failed"

    mismatch = client.post(
        "/agents", json={"name": "beta", "yaml_source": _yaml("gamma")}
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "name_mismatch"


def test_validate_endpoint(client):
    good = client.post("/validate", json={"yaml_source": _yaml()})
    assert good.status_code == 200
    assert good.json()["valid"] is True
    assert good.json()["profile"]["name"] == "alpha"

    bad = client.post("/validate", json={"yaml_source": "nope: 1\n"})
    assert bad.status_code == 422


def test_tools_and_files_over_http(client):
    client.post("/agents", json={"name": "alpha", "yaml_source": _yaml()})

    source = (
        "@register\n"
        "async def greet(ctx) -> str:\n"
        '    """Say hello."""\n'
        '    return "hello"\n'
    )
    resp = client.post(
        "/agents/alpha/tools", json={"tool_name": "greet", "source": source}
    )
    assert resp.status_code == 201
    assert client.get("/agents/alpha/tools").json()["count"] == 1
    assert "Say hello." in client.get("/agents/alpha/tools/greet").json()["source"]

    resp = client.put(
        "/agents/alpha/files", json={"path": "notes.md", "content": "hi"}
    )
    assert resp.status_code == 200
    read = client.get("/agents/alpha/files", params={"path": "notes.md"})
    assert read.json()["content"] == "hi"


# ── schedules ────────────────────────────────────────────────────────────────


def test_schedule_set_list_cancel(client):
    client.post("/agents", json={"name": "alpha", "yaml_source": _yaml()})

    resp = client.post(
        "/schedules",
        json={"agent": "alpha", "prompt": "wake up", "delay_seconds": 60},
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]
    assert job_id.startswith("retrigger-alpha-")

    # Same agent + same fire second must be a distinct job, not a silent
    # replacement of the first prompt.
    twin = client.post(
        "/schedules",
        json={"agent": "alpha", "prompt": "wake up again", "delay_seconds": 60},
    )
    assert twin.status_code == 201
    assert twin.json()["job_id"] != job_id
    assert client.get("/schedules").json()["count"] == 2
    client.delete(f"/schedules/{twin.json()['job_id']}")

    listing = client.get("/schedules", params={"agent": "alpha"}).json()
    assert listing["count"] == 1
    assert listing["retriggers"][0]["prompt_preview"] == "wake up"

    assert client.delete(f"/schedules/{job_id}").status_code == 200
    assert client.get("/schedules").json()["count"] == 0

    missing = client.delete(f"/schedules/{job_id}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "job_not_found"


def test_schedule_validation_errors(client):
    both = client.post(
        "/schedules",
        json={
            "agent": "alpha",
            "prompt": "x",
            "delay_seconds": 5,
            "at": "2030-01-01T00:00:00+00:00",
        },
    )
    assert both.status_code == 400

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    resp = client.post("/schedules", json={"agent": "alpha", "prompt": "x", "at": past})
    assert resp.status_code == 400
    assert "past" in resp.json()["detail"]
