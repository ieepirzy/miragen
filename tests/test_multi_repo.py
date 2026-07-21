"""Multi-repository workspace preparation and harvest (issue #33 Phase D) —
per-repo clone/checkout with commit resolution, ephemeral-credential hygiene,
per-repo baselines and diff bundles, resume without bindings, and the launch
binding requirement.

Origins are real local git repositories; the Codex SDK stays stubbed.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import miragen.app  # noqa: F401

app_module = sys.modules["miragen.app"]
from miragen.app import app
from miragen.edf import EDFValidationError, validate_edf
from miragen.executor.base import RepositoryCheckout, _sanitize_url
from miragen.runs import RunStore

from tests.test_edf import minimal_edf
from tests.test_executor import StubThread, _executor_profile, default_events, make_executor
from tests.test_launch_api import wait_terminal


def _run(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def make_origin(tmp_path, name, *, tag=None):
    """A local origin repo with one commit on `main`; returns (path, head_sha)."""
    origin = tmp_path / "origins" / name
    origin.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    (origin / f"{name}.txt").write_text(f"hello from {name}\n")
    _run(origin, "add", "-A")
    _run(origin, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    if tag:
        _run(origin, "tag", tag)
    sha = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return origin, sha


def two_checkouts(tmp_path):
    app_origin, app_sha = make_origin(tmp_path, "app")
    lib_origin, lib_sha = make_origin(tmp_path, "lib", tag="v1")
    checkouts = [
        RepositoryCheckout(
            name="app", ref="refs/heads/main", mount_path="app",
            writable=True, clone_url=str(app_origin),
        ),
        RepositoryCheckout(
            name="lib", ref="refs/tags/v1", mount_path="vendor/lib",
            writable=False, clone_url=str(lib_origin),
        ),
    ]
    return checkouts, {"app": app_sha, "lib": lib_sha}


# ── Executor-level preparation + harvest ─────────────────────────────────────


async def test_prepares_repos_resolves_commits_and_harvests_writable_only(tmp_path):
    profile = _executor_profile()
    checkouts, shas = two_checkouts(tmp_path)
    ws_holder = {}

    def touch():
        ws = ws_holder["ws"]
        (ws / "app" / "changed.py").write_text("print('edited')\n")
        (ws / "vendor" / "lib" / "sneaky.py").write_text("print('should not harvest')\n")

    executor = make_executor(profile, tmp_path, thread=StubThread(default_events(), touch=touch))
    ws_holder["ws"] = Path(profile.executor.workspace_root) / "mr-run1"

    result = await executor.run_job("edit the app", "mr-run1", repositories=checkouts)
    assert result.status == "succeeded"
    assert {r["name"]: r["commit"] for r in result.repositories} == shas

    ws = ws_holder["ws"]
    # multi-repo layout: root is NOT a git repo; each mount is its own
    assert not (ws / ".git").exists()
    assert (ws / "app" / ".git").exists() and (ws / "vendor" / "lib" / ".git").exists()

    bundle = Path(result.diff_path).read_text()
    assert "# === miragen repository: app (mount: app) ===" in bundle
    assert "changed.py" in bundle
    # non-writable mounts are reference material — never harvested
    assert "sneaky.py" not in bundle
    assert "lib" not in [
        line.split("repository: ")[1].split(" ")[0]
        for line in bundle.splitlines() if line.startswith("# === miragen repository")
    ]
    per_repo = ws / ".miragen" / "diffs" / "app.patch"
    assert "changed.py" in per_repo.read_text()

    events = executor.read_events("mr-run1", limit=1000)
    prepared = [e for e in events if e["type"] == "lifecycle.repo.prepared"]
    assert [(e["name"], e["commit"], e["writable"]) for e in prepared] == [
        ("app", shas["app"], True), ("lib", shas["lib"], False),
    ]
    assert all(e["duration_ms"] >= 0 for e in prepared)


async def test_resume_reuses_prepared_workspace_without_bindings(tmp_path):
    profile = _executor_profile()
    checkouts, shas = two_checkouts(tmp_path)
    executor = make_executor(profile, tmp_path)

    first = await executor.run_job("start", "mr-resume", repositories=checkouts)
    assert first.status == "succeeded"

    # bindings are ephemeral: resume gets binding-less checkouts and must
    # re-read recorded state rather than re-clone
    bare = [
        RepositoryCheckout(name=c.name, ref=c.ref, mount_path=c.mount_path, writable=c.writable)
        for c in checkouts
    ]
    second = await executor.run_job(
        "continue", "mr-resume", thread_id=first.thread_id,
        first_turn=False, repositories=bare,
    )
    assert second.status == "succeeded"
    assert {r["name"]: r["commit"] for r in second.repositories} == shas


async def test_unprepared_repo_without_binding_fails_clearly(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    bare = [RepositoryCheckout(name="app", ref="refs/heads/main", mount_path="app")]
    result = await executor.run_job("go", "mr-nobind", repositories=bare)
    assert result.status == "failed" and result.exit_reason == "crash"
    assert "no binding is available" in result.error


async def test_clone_failure_redacts_credentialed_binding_url(tmp_path):
    executor = make_executor(_executor_profile(), tmp_path)
    secret_url = str(tmp_path / "does-not-exist")
    checkouts = [
        RepositoryCheckout(
            name="app", ref="refs/heads/main", mount_path="app",
            writable=True, clone_url=secret_url,
        ),
    ]
    result = await executor.run_job("go", "mr-badurl", repositories=checkouts)
    assert result.status == "failed"
    assert secret_url not in result.error
    assert "<clone-url:app>" in result.error
    # the events file is durable — the URL must not be there either
    events_text = (executor.runs_root / "mr-badurl.events.jsonl").read_text()
    assert secret_url not in events_text


async def test_clone_failure_scrubs_credential_from_git_config(tmp_path):
    """P1 regression: a failed fetch must not leave the token-bearing binding
    URL in the kept (resumable) workspace's .git/config."""
    executor = make_executor(_executor_profile(), tmp_path)
    creds_url = "https://x-token:s3cr3t@localhost:1/nope.git"  # refused → fetch fails
    checkouts = [
        RepositoryCheckout(
            name="app", ref="refs/heads/main", mount_path="app",
            writable=True, clone_url=creds_url,
        ),
    ]
    result = await executor.run_job("go", "mr-failscrub", repositories=checkouts)
    assert result.status == "failed"
    config = (
        Path(executor.spec.workspace_root) / "mr-failscrub" / "app" / ".git" / "config"
    ).read_text()
    assert "s3cr3t" not in config and "@" not in config


def test_unsafe_repository_name_rejected_by_edf():
    edf = minimal_edf()
    edf["spec"]["workspace"]["repositories"] = [{
        "name": "owner/repo",  # a '/' would escape the .miragen/diffs dir
        "source": {"provider": "github", "connectionRef": "c1"},
        "ref": "refs/heads/main", "mountPath": "app",
    }]
    with pytest.raises(EDFValidationError):
        validate_edf(edf)


async def test_unsafe_repository_name_rejected_at_prepare(tmp_path):
    """Defense in depth below the EDF layer: a RepositoryCheckout with an
    unsafe name fails at setup, not mid-harvest."""
    executor = make_executor(_executor_profile(), tmp_path)
    checkouts = [
        RepositoryCheckout(name="../escape", ref="r", mount_path="app", writable=True, clone_url="x"),
    ]
    result = await executor.run_job("go", "mr-badname", repositories=checkouts)
    assert result.status == "failed"
    assert "unsafe repository name" in result.error


def test_sanitize_url_strips_userinfo():
    assert _sanitize_url("https://x-token:s3cret@github.com/a/b.git") == "https://github.com/a/b.git"
    assert _sanitize_url("https://github.com/a/b.git") == "https://github.com/a/b.git"
    assert _sanitize_url("/local/path/repo") == "/local/path/repo"


async def test_prepared_clone_config_carries_no_userinfo(tmp_path):
    """After preparation, the remote URL resting in .git/config is the
    sanitized one — a credentialed binding must not persist there."""
    profile = _executor_profile()
    checkouts, _ = two_checkouts(tmp_path)
    executor = make_executor(profile, tmp_path)
    result = await executor.run_job("go", "mr-config", repositories=checkouts)
    assert result.status == "succeeded"
    config = (Path(profile.executor.workspace_root) / "mr-config" / "app" / ".git" / "config").read_text()
    assert "@" not in config.split("[remote", 1)[1]


# ── EDF validation: reserved mount ───────────────────────────────────────────


def test_reserved_miragen_mount_path_rejected():
    edf = minimal_edf()
    edf["spec"]["workspace"]["repositories"] = [{
        "name": "evil",
        "source": {"provider": "github", "connectionRef": "c1"},
        "ref": "refs/heads/main",
        "mountPath": ".miragen",
    }]
    with pytest.raises(EDFValidationError, match="reserved"):
        validate_edf(edf)


# ── App-level: launch with bindings, missing bindings, per-repo diff ─────────


@pytest.fixture(autouse=True)
def reset_app_state():
    yield
    app_module._profile = None
    app_module._agent = None
    app_module._run_store = None
    app_module._executor = None


@pytest.fixture
async def executor_client(tmp_path):
    profile = _executor_profile()
    executor = make_executor(profile, tmp_path)
    app_module._profile = profile
    app_module._executor = executor
    app_module._run_store = RunStore(root=tmp_path / "runs", retention=50)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def repo_edf():
    edf = minimal_edf()
    edf["metadata"]["name"] = "codex-worker"
    edf["spec"]["workspace"]["repositories"] = [
        {
            "name": "app",
            "source": {"provider": "github", "connectionRef": "conn_app"},
            "ref": "refs/heads/main",
            "mountPath": "app",
            "writable": True,
        },
        {
            "name": "lib",
            "source": {"provider": "github", "connectionRef": "conn_lib"},
            "ref": "refs/tags/v1",
            "mountPath": "vendor/lib",
            "writable": False,
        },
    ]
    return edf


async def test_launch_with_bindings_records_commits_everywhere(executor_client, tmp_path):
    app_origin, app_sha = make_origin(tmp_path, "app")
    lib_origin, lib_sha = make_origin(tmp_path, "lib", tag="v1")

    resp = await executor_client.post("/executor-runs", json={
        "prompt": "work",
        "idempotency_key": "bind-1",
        "edf": repo_edf(),
        "context": {"repositories": {
            "conn_app": {"clone_url": str(app_origin)},
            "conn_lib": {"clone_url": str(lib_origin)},
        }},
    })
    assert resp.status_code == 202, resp.text
    record = await wait_terminal(resp.json()["run_id"])
    assert record.status == "succeeded"

    # commit revisions on the run record (issue #33 Phase B item)...
    assert {r.name: r.commit for r in record.repositories} == {"app": app_sha, "lib": lib_sha}
    # ...and filled into the snapshot's repository plan (Phase D item)
    snapshot = (await executor_client.get(f"/runs/{record.run_id}/snapshot")).json()
    assert {e["name"]: e["commit"] for e in snapshot["repository_plan"]} == {
        "app": app_sha, "lib": lib_sha,
    }
    # bindings never persist: local origin paths are this test's "credential"
    import json as _json
    assert str(app_origin) not in _json.dumps(snapshot)
    assert str(app_origin) not in record.model_dump_json()

    # per-repository diff endpoint
    resp = await executor_client.get(f"/runs/{record.run_id}/diff", params={"repository": "app"})
    assert resp.status_code == 200
    resp = await executor_client.get(f"/runs/{record.run_id}/diff", params={"repository": "lib"})
    assert resp.status_code == 404  # not writable → no per-repo diff
    assert "app" in resp.json()["detail"]["writable_repositories"]


async def test_launch_refuses_missing_bindings(executor_client):
    resp = await executor_client.post("/executor-runs", json={
        "prompt": "work",
        "idempotency_key": "bind-missing",
        "edf": repo_edf(),
        "context": {"repositories": {"conn_app": {"clone_url": "/somewhere"}}},
    })
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "missing repository bindings"
    assert detail["missing_connection_refs"] == ["conn_lib"]
    assert app_module._run_store.list() == []
