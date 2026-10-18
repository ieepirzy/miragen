"""miragend never follows a link an agent planted in its own workspace
(review round 2, B1), and never writes through one."""

import os

import pytest
import yaml
from fastapi.testclient import TestClient

from miragen.daemon import safe_fs
from miragen.daemon.api import create_app
from miragen.daemon.core import InvalidPath, LifecycleCore
from tests.test_daemon_core import (
    DEFAULT_DIGEST,
    VALID_YAML,
    FakeDocker,
    FakeImage,
    FakeNotFound,
    RecordingRunner,
)

SECRET = "the-lanes-master-token"


def _core(tmp_path):
    docker_client = FakeDocker()
    docker_client.image_map[DEFAULT_DIGEST] = FakeImage(
        labels={"io.miragen.profile-contracts": "1 2"}, repo_digests=[DEFAULT_DIGEST])
    runner = RecordingRunner()
    runner.on_up = lambda name: docker_client.add(name)
    return LifecycleCore(
        tmp_path / "ws",
        docker_client,
        base_image="ghcr.io/example/miragen:test",
        internal_token="shared-secret",
        environ={},
        runner=runner,
        not_found=FakeNotFound,
        sleep=lambda _s: None,
    )


@pytest.fixture
def setup(tmp_path):
    core = _core(tmp_path)
    core.create_agent("lane-x", VALID_YAML.format(name="lane-x"))
    secret = tmp_path / "agents-host-secrets" / "miragen_internal_token"
    secret.parent.mkdir()
    secret.write_text(SECRET)
    return core, tmp_path / "ws" / "agents" / "lane-x", secret


def test_the_round_2_probe_a_planted_profile_link_leaks_nothing(setup):
    # `ln -sf /run/agents-host-secrets/miragen_internal_token /agent/agent.yaml`
    core, workspace, secret = setup
    (workspace / "agent.yaml").unlink()
    (workspace / "agent.yaml").symlink_to(secret)

    client = TestClient(create_app(core, token="t"))
    client.headers["Authorization"] = "Bearer t"
    body = client.get("/agents/lane-x").json()
    assert SECRET not in body["yaml"]
    # It is the daemon's own last-good copy.
    assert yaml.safe_load(body["yaml"])["name"] == "lane-x"
    assert SECRET not in str(client.get("/agents").json())
    assert client.get("/agents/lane-x/files", params={"path": "agent.yaml"}).status_code == 400


def test_get_agent_serves_the_daemons_copy_not_the_workspace_file(setup):
    core, workspace, _ = setup
    (workspace / "agent.yaml").write_text("name: lane-x\nrewritten: by the agent\n")
    assert "rewritten" not in core.get_agent("lane-x")["yaml"]


def test_updates_and_rollbacks_never_write_through_a_link(setup):
    core, workspace, secret = setup
    compose = core.workspace / "compose.yml"
    before = compose.read_text()
    (workspace / "agent.yaml").unlink()
    (workspace / "agent.yaml").symlink_to(compose)
    core.update_agent_config("lane-x", VALID_YAML.format(name="lane-x").replace(
        "be helpful", "be brief"))
    assert compose.read_text() == before
    assert not (workspace / "agent.yaml").is_symlink()
    assert "be brief" in (workspace / "agent.yaml").read_text()
    core._roll_back_config("lane-x", VALID_YAML.format(name="lane-x"), None,
                           recreate=False, image_ref=None)
    assert secret.read_text() == SECRET


@pytest.mark.parametrize("op", ["register", "edit", "delete", "list", "source"])
def test_tool_operations_never_follow_a_planted_tools_link(setup, op):
    core, workspace, secret = setup
    (workspace / "tools.py").unlink()
    (workspace / "tools.py").symlink_to(secret)
    calls = {
        "register": lambda: core.register_tool(
            "lane-x", "t", "from miragen import register\n@register\nasync def t():\n    return 1\n"),
        "edit": lambda: core.edit_tool("lane-x", "t", "a", "b"),
        "delete": lambda: core.delete_tool("lane-x", "t"),
        "list": lambda: core.list_tools("lane-x"),
        "source": lambda: core.tool_source("lane-x", "t"),
    }
    try:
        result = calls[op]()
    except Exception as exc:  # refused one way or another
        assert SECRET not in str(exc)
    else:
        assert SECRET not in str(result)
    assert secret.read_text() == SECRET


def test_a_directory_link_in_the_path_is_refused(setup, tmp_path):
    core, workspace, secret = setup
    (workspace / "sub").symlink_to(secret.parent)
    with pytest.raises(InvalidPath):
        core.read_file("lane-x", "sub/miragen_internal_token")
    with pytest.raises(InvalidPath):
        core.write_file("lane-x", "sub/miragen_internal_token", "x")
    assert secret.read_text() == SECRET


def test_file_api_writes_replace_a_link_instead_of_following_it(setup):
    core, workspace, secret = setup
    (workspace / "notes.txt").symlink_to(secret)
    core.write_file("lane-x", "notes.txt", "hello")
    assert secret.read_text() == SECRET
    assert (workspace / "notes.txt").read_text() == "hello"
    assert not (workspace / "notes.txt").is_symlink()


def test_labels_are_daemon_owned(setup):
    core, workspace, _ = setup
    core._write_agent_labels("lane-x", {"access.mirarun.io/x": "granted"})
    assert not (workspace / "labels.json").exists()
    (workspace / "labels.json").write_text('{"access.mirarun.io/evil": "granted"}')
    assert core._read_agent_labels("lane-x") == {"access.mirarun.io/x": "granted"}


def test_export_skips_links_and_never_archives_their_target(setup):
    core, workspace, secret = setup
    (workspace / "leak.txt").symlink_to(secret)
    result = core.export_agent("lane-x")
    import tarfile

    with tarfile.open(result["archive_path"]) as tar:
        for member in tar.getmembers():
            if member.isfile():
                assert SECRET.encode() not in tar.extractfile(member).read()


def test_the_check_and_the_use_are_the_same_fd(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "f").write_text("ok")
    fd = safe_fs.open_regular(base, "f")
    try:
        # Swap the name for a link after opening: the open fd still reads the
        # file that was checked.
        (base / "f").unlink()
        (base / "f").symlink_to(tmp_path)
        assert os.read(fd, 10) == b"ok"
    finally:
        os.close(fd)
    with pytest.raises(safe_fs.UnsafeAgentPath):
        safe_fs.read_text(base, "f")
