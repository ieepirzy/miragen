"""LifecycleCore against a fake Docker client and a recording compose runner —
no socket, no subprocesses."""

import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from miragen.daemon.core import (
    AgentExists,
    AgentLimitExceeded,
    AgentNotFound,
    ArchiveInvalid,
    ArchiveNotFound,
    ContainerNotFound,
    ContainerOperationFailed,
    EditConflict,
    InvalidAgentName,
    InvalidPath,
    LifecycleCore,
    NameMismatch,
    RestartFailed,
    ToolNotFound,
    ToolSourceInvalid,
    ValidationFailed,
    WorkspaceFileNotFound,
    validate_profile_text,
)

VALID_YAML = """\
name: {name}
mode: interactive
triggers:
  - type: http
spec:
  model: "test:whatever"
  instructions: "be helpful"
"""


class FakeNotFound(Exception):
    pass


class FakeContainer:
    def __init__(self, name: str, registry: dict):
        self.name = name
        self.status = "running"
        self._registry = registry
        self.restarted = 0

    def stop(self):
        self.status = "exited"

    def restart(self):
        self.restarted += 1

    def remove(self):
        self._registry.pop(self.name, None)

    def logs(self, tail=50, stream=False):
        return f"log line for {self.name} (tail={tail})".encode()


class FakeDocker:
    def __init__(self):
        self.container_map: dict[str, FakeContainer] = {}
        self.networks_created: list[str] = []

        outer = self

        class _Containers:
            def get(self, name):
                if name not in outer.container_map:
                    raise FakeNotFound(name)
                return outer.container_map[name]

        class _Networks:
            def get(self, name):
                if name not in outer.networks_created:
                    raise FakeNotFound(name)
                return name

            def create(self, name, **kw):
                outer.networks_created.append(name)

        self.containers = _Containers()
        self.networks = _Networks()

    def add(self, name: str) -> FakeContainer:
        c = FakeContainer(name, self.container_map)
        self.container_map[name] = c
        return c


class RecordingRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.returncode = 0
        self.stderr = ""

    def __call__(self, cmd, capture_output=True, text=True, cwd=None):
        self.calls.append(list(cmd))
        # Emulate `docker compose up -d <name>` creating the container.
        if self.returncode == 0 and cmd[:4] == ["docker", "compose", "up", "-d"]:
            self.on_up(cmd[4])
        return SimpleNamespace(
            returncode=self.returncode, stdout="", stderr=self.stderr
        )

    def on_up(self, name):  # overridden per-fixture
        pass


@pytest.fixture
def docker_client():
    return FakeDocker()


@pytest.fixture
def runner(docker_client):
    r = RecordingRunner()
    r.on_up = lambda name: docker_client.add(name)
    return r


@pytest.fixture
def core(tmp_path, docker_client, runner):
    return LifecycleCore(
        tmp_path,
        docker_client,
        base_image="ghcr.io/example/miragen:test",
        internal_token="shared-secret",
        environ={"ANTHROPIC_API_KEY_FILE": "/run/secrets/anthropic_key"},
        runner=runner,
        not_found=FakeNotFound,
    )


def _yaml(name="alpha"):
    return VALID_YAML.format(name=name)


# ── validation ───────────────────────────────────────────────────────────────


def test_validate_profile_text_returns_summary():
    summary = validate_profile_text(_yaml("alpha"))
    assert summary["name"] == "alpha"
    assert summary["mode"] == "interactive"
    assert summary["triggers"] == ["http"]
    assert summary["model"] == "test:whatever"


def test_validate_profile_text_rejects_unknown_fields():
    with pytest.raises(ValidationFailed) as exc:
        validate_profile_text(_yaml("alpha") + "definitely_not_a_field: 1\n")
    assert "unknown field" in str(exc.value)


# ── naming ───────────────────────────────────────────────────────────────────


def test_invalid_names_rejected(core):
    for bad in ("UPPER", "-leading", "a" * 64, "has space", "../etc"):
        with pytest.raises(InvalidAgentName):
            core.check_name(bad)


def test_reserved_names_rejected(core):
    for reserved in ("miragend", "miragen-mcp"):
        with pytest.raises(InvalidAgentName):
            core.check_name(reserved)


# ── create / registry ────────────────────────────────────────────────────────


def test_create_agent_writes_workspace_compose_and_starts(core, runner, tmp_path):
    core.create_agent("alpha", _yaml("alpha"))

    assert (tmp_path / "agents" / "alpha" / "agent.yaml").exists()
    assert "register" in (tmp_path / "agents" / "alpha" / "tools.py").read_text()
    assert ["docker", "compose", "up", "-d", "alpha"] in runner.calls

    import yaml as pyyaml

    compose = pyyaml.safe_load((tmp_path / "compose.yml").read_text())
    service = compose["services"]["alpha"]
    assert service["image"] == "ghcr.io/example/miragen:test"
    assert service["networks"] == ["miragen-net"]
    # The internal token is forwarded so the agent's /run guard is armed.
    assert service["environment"]["MIRAGEN_INTERNAL_TOKEN"] == "shared-secret"
    assert service["environment"]["ANTHROPIC_API_KEY_FILE"] == "/run/secrets/anthropic_key"
    assert compose["secrets"] == {"anthropic_key": {"external": True}}


def test_create_agent_duplicate_rejected(core):
    core.create_agent("alpha", _yaml("alpha"))
    with pytest.raises(AgentExists):
        core.create_agent("alpha", _yaml("alpha"))


def test_create_agent_name_mismatch_rejected_and_nothing_written(core, tmp_path):
    with pytest.raises(NameMismatch):
        core.create_agent("alpha", _yaml("beta"))
    assert not (tmp_path / "agents" / "alpha").exists()


def test_create_agent_invalid_yaml_rejected(core, tmp_path):
    with pytest.raises(ValidationFailed):
        core.create_agent("alpha", "mode: interactive\n")
    assert not (tmp_path / "agents" / "alpha").exists()


def test_create_agent_start_failure_rolls_back(core, runner, tmp_path):
    runner.returncode = 1
    runner.stderr = "no such image"
    with pytest.raises(ContainerOperationFailed):
        core.create_agent("alpha", _yaml("alpha"))
    assert not (tmp_path / "agents" / "alpha").exists()
    import yaml as pyyaml

    compose = pyyaml.safe_load((tmp_path / "compose.yml").read_text())
    assert "alpha" not in compose.get("services", {})


def test_create_agent_concurrent_race_respects_cap(tmp_path):
    # Regression for the TOCTOU race: MIRAGEND_MAX_AGENTS=1 with several
    # threads racing create_agent for *different* names must still let
    # exactly one through — the pre-lock version let every thread pass the
    # cap check before any of them had created a directory.
    docker_client = FakeDocker()
    runner = RecordingRunner()
    runner.on_up = lambda name: docker_client.add(name)
    core = LifecycleCore(
        tmp_path,
        docker_client,
        base_image="ghcr.io/example/miragen:test",
        environ={"MIRAGEND_MAX_AGENTS": "1"},
        runner=runner,
        not_found=FakeNotFound,
    )

    n = 6
    results: list[str] = [""] * n
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            core.create_agent(f"racer{i}", _yaml(f"racer{i}"))
            results[i] = "ok"
        except AgentLimitExceeded:
            results[i] = "limited"
        except Exception as exc:  # pragma: no cover - would fail the test below
            results[i] = f"error: {exc!r}"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1, results
    assert results.count("limited") == n - 1, results

    created = [p for p in (tmp_path / "agents").iterdir() if p.is_dir()]
    assert len(created) == 1


def test_list_agents_reports_status_and_endpoint(core, docker_client):
    core.create_agent("alpha", _yaml("alpha"))
    agents = core.list_agents()
    assert len(agents) == 1
    assert agents[0]["name"] == "alpha"
    assert agents[0]["status"] == "running"
    assert agents[0]["mode"] == "interactive"
    assert agents[0]["endpoint"] == "http://alpha:8000"

    docker_client.container_map["alpha"].stop()
    assert core.list_agents()[0]["status"] == "exited"


def test_get_agent_detail(core):
    core.create_agent("alpha", _yaml("alpha"))
    detail = core.get_agent("alpha")
    assert detail["name"] == "alpha"
    assert "mode: interactive" in detail["yaml"]
    assert detail["has_tools"] is True


def test_get_agent_missing(core):
    with pytest.raises(AgentNotFound):
        core.get_agent("ghost")


# ── lifecycle ────────────────────────────────────────────────────────────────


def test_stop_restart_delete(core, docker_client, tmp_path):
    core.create_agent("alpha", _yaml("alpha"))
    container = docker_client.container_map["alpha"]

    core.restart_agent("alpha")
    assert container.restarted == 1

    core.stop_agent("alpha")
    assert container.status == "exited"

    core.delete_agent("alpha")
    assert "alpha" not in docker_client.container_map
    assert not (tmp_path / "agents" / "alpha").exists()
    import yaml as pyyaml

    compose = pyyaml.safe_load((tmp_path / "compose.yml").read_text())
    assert "alpha" not in compose.get("services", {})


def test_restart_missing_container(core, tmp_path):
    # Workspace exists but the container never started.
    (tmp_path / "agents" / "ghost").mkdir(parents=True)
    with pytest.raises(ContainerNotFound):
        core.restart_agent("ghost")


def test_docker_operations_gated_on_workspace_ownership(core, docker_client):
    # A container whose name fits the agent grammar but has NO workspace in
    # the daemon (e.g. the host's own postgres) must be untouchable — Docker
    # resolves by container name, not by who created the container.
    interloper = docker_client.add("postgres")
    for op in (core.restart_agent, core.stop_agent, core.delete_agent):
        with pytest.raises(AgentNotFound):
            op("postgres")
    with pytest.raises(AgentNotFound):
        core.agent_logs("postgres")
    with pytest.raises(AgentNotFound):
        core.start_agent("postgres")
    assert interloper.status == "running"
    assert "postgres" in docker_client.container_map


def test_delete_missing_container_still_cleans_workspace(core, tmp_path):
    # Workspace exists but the container never started.
    d = tmp_path / "agents" / "orphan"
    d.mkdir(parents=True)
    core.delete_agent("orphan")
    assert not d.exists()


def test_agent_logs(core):
    core.create_agent("alpha", _yaml("alpha"))
    assert "log line for alpha" in core.agent_logs("alpha", tail=10)


# ── tools ────────────────────────────────────────────────────────────────────


TOOL_SRC = '''@register
async def greet(ctx, who: str) -> str:
    """Say hello."""
    return f"hello {who}"
'''


def test_register_list_source_edit_delete_tool(core, docker_client, tmp_path):
    core.create_agent("alpha", _yaml("alpha"))
    core.register_tool("alpha", "greet", TOOL_SRC)

    tools = core.list_tools("alpha")
    assert [t["name"] for t in tools] == ["greet"]
    assert "Say hello." in core.tool_source("alpha", "greet")

    import yaml as pyyaml

    profile = pyyaml.safe_load(
        (tmp_path / "agents" / "alpha" / "agent.yaml").read_text()
    )
    assert profile["tools"] == ["greet"]
    assert docker_client.container_map["alpha"].restarted == 1

    core.edit_tool("alpha", "greet", "hello {who}", "hi {who}")
    assert "hi {who}" in core.tool_source("alpha", "greet")

    core.delete_tool("alpha", "greet")
    assert core.list_tools("alpha") == []
    profile = pyyaml.safe_load(
        (tmp_path / "agents" / "alpha" / "agent.yaml").read_text()
    )
    assert profile["tools"] == []


ALIASED_TOOL_SRC = '''@register("fetch_forecast")
async def get_forecast(ctx, city: str) -> str:
    """Return the 7-day forecast for a city."""
    return "sunny"
'''


def test_aliased_tool_managed_by_registered_name(core):
    # @register("alias") tools are advertised under the alias, so GET/PATCH/
    # DELETE must resolve the alias too — not just the function identifier.
    core.create_agent("alpha", _yaml("alpha"))
    core.register_tool("alpha", "fetch_forecast", ALIASED_TOOL_SRC)

    assert "async def get_forecast" in core.tool_source("alpha", "fetch_forecast")
    core.edit_tool("alpha", "fetch_forecast", '"sunny"', '"rainy"')
    assert '"rainy"' in core.tool_source("alpha", "fetch_forecast")
    core.delete_tool("alpha", "fetch_forecast")
    assert core.list_tools("alpha") == []


def test_register_tool_bad_source(core):
    core.create_agent("alpha", _yaml("alpha"))
    with pytest.raises(ToolSourceInvalid):
        core.register_tool("alpha", "greet", "def not_async(): pass")


def test_register_tool_restart_failure_rolls_back(core, docker_client, tmp_path):
    core.create_agent("alpha", _yaml("alpha"))
    original = (tmp_path / "agents" / "alpha" / "tools.py").read_text()
    del docker_client.container_map["alpha"]  # restart will fail

    with pytest.raises(RestartFailed):
        core.register_tool("alpha", "greet", TOOL_SRC)
    assert (tmp_path / "agents" / "alpha" / "tools.py").read_text() == original


def test_edit_tool_conflicts(core):
    core.create_agent("alpha", _yaml("alpha"))
    core.register_tool("alpha", "greet", TOOL_SRC)
    with pytest.raises(EditConflict):
        core.edit_tool("alpha", "greet", "not present", "x")
    with pytest.raises(ToolNotFound):
        core.edit_tool("alpha", "ghost", "a", "b")


# ── files ────────────────────────────────────────────────────────────────────


def test_file_roundtrip_and_traversal_guard(core, tmp_path):
    core.create_agent("alpha", _yaml("alpha"))
    core.write_file("alpha", "data/notes.md", "remember this")
    assert core.read_file("alpha", "data/notes.md") == "remember this"

    core.edit_file("alpha", "data/notes.md", "remember", "forget")
    assert core.read_file("alpha", "data/notes.md") == "forget this"

    for bad in ("../secrets", "/etc/passwd", "a/../../b"):
        with pytest.raises(InvalidPath):
            core.read_file("alpha", bad)

    with pytest.raises(WorkspaceFileNotFound):
        core.read_file("alpha", "missing.txt")


# ── export / import ──────────────────────────────────────────────────────────


def test_export_then_import_roundtrip(core, tmp_path):
    core.create_agent("alpha", _yaml("alpha"))
    core.write_file("alpha", "data/notes.md", "carry me over")
    # Run history must not travel.
    core.write_file("alpha", "history.json", "{}")
    (tmp_path / "agents" / "alpha" / "runs").mkdir()
    (tmp_path / "agents" / "alpha" / "runs" / "r1.json").write_text("{}")

    result = core.export_agent("alpha")
    assert "data/notes.md" in result["included"]
    assert all("history.json" != s["path"] for s in result["included"] if isinstance(s, dict))
    assert any(s["path"] == "history.json" for s in result["skipped"])

    archive = Path(result["archive_path"])
    assert archive.exists()
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "alpha/data/notes.md" in names
    assert not any("runs/" in n for n in names)

    core.import_agent("beta", result["archive_path"])
    assert core.read_file("beta", "data/notes.md") == "carry me over"
    # Name was rewritten during import.
    assert "name: beta" in core.get_agent("beta")["yaml"]


def test_import_missing_archive(core):
    with pytest.raises(ArchiveNotFound):
        core.import_agent("beta", "exports/nope.tar.gz")


def test_import_archive_path_escape_rejected(core):
    with pytest.raises(InvalidPath):
        core.import_agent("beta", "/etc/passwd")


def test_import_agent_concurrent_race_respects_cap(tmp_path):
    # Same TOCTOU race as create_agent, but through import_agent's
    # shutil.move commit step.
    docker_client = FakeDocker()
    runner = RecordingRunner()
    runner.on_up = lambda name: docker_client.add(name)

    setup_core = LifecycleCore(
        tmp_path,
        docker_client,
        base_image="ghcr.io/example/miragen:test",
        environ={"MIRAGEND_MAX_AGENTS": "100"},
        runner=runner,
        not_found=FakeNotFound,
    )
    setup_core.create_agent("source", _yaml("source"))
    archive_path = setup_core.export_agent("source")["archive_path"]
    setup_core.delete_agent("source")

    race_core = LifecycleCore(
        tmp_path,
        docker_client,
        base_image="ghcr.io/example/miragen:test",
        environ={"MIRAGEND_MAX_AGENTS": "1"},
        runner=runner,
        not_found=FakeNotFound,
    )

    n = 6
    results: list[str] = [""] * n
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            race_core.import_agent(f"imported{i}", archive_path)
            results[i] = "ok"
        except AgentLimitExceeded:
            results[i] = "limited"
        except Exception as exc:  # pragma: no cover - would fail the test below
            results[i] = f"error: {exc!r}"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1, results
    assert results.count("limited") == n - 1, results

    created = [p for p in (tmp_path / "agents").iterdir() if p.is_dir()]
    assert len(created) == 1


def test_import_agent_rejects_before_extraction_when_at_cap(tmp_path):
    # The cap check must run — and reject — before the uploaded archive is
    # ever opened/extracted. Point at a real file that is deliberately not
    # a valid gzip tarball: if import_agent got as far as tarfile.open, it
    # would fail loudly with ArchiveInvalid instead of the expected
    # AgentLimitExceeded, and it would have created a staging directory
    # under exports/ first. Asserting AgentLimitExceeded (not
    # ArchiveInvalid) and that no staging dir was left behind proves
    # extraction never happened.
    docker_client = FakeDocker()
    runner = RecordingRunner()
    runner.on_up = lambda name: docker_client.add(name)
    core = LifecycleCore(
        tmp_path,
        docker_client,
        base_image="ghcr.io/example/miragen:test",
        environ={"MIRAGEND_MAX_AGENTS": "1"},
        runner=runner,
        not_found=FakeNotFound,
    )
    core.create_agent("alpha", _yaml("alpha"))  # already at the cap (max=1)

    exports_dir = tmp_path / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    bogus = exports_dir / "not-a-tarball.tar.gz"
    bogus.write_bytes(b"this is not gzip data and would blow up tarfile.open")

    before = set(exports_dir.iterdir())

    with pytest.raises(AgentLimitExceeded):
        core.import_agent("beta", "exports/not-a-tarball.tar.gz")

    after = set(exports_dir.iterdir())
    # Nothing new (no staging dir) appeared under exports/ — extraction
    # (tempfile.mkdtemp + tarfile.open + extractall) never ran.
    assert after == before

    with pytest.raises(ArchiveInvalid):
        # Sanity check: opening the same bogus archive for real (cap not
        # in the way) does surface ArchiveInvalid, confirming the file
        # would indeed "fail loudly" had extraction been attempted above.
        LifecycleCore(
            tmp_path,
            docker_client,
            base_image="ghcr.io/example/miragen:test",
            environ={"MIRAGEND_MAX_AGENTS": "100"},
            runner=runner,
            not_found=FakeNotFound,
        ).import_agent("gamma", "exports/not-a-tarball.tar.gz")
