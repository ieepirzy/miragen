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


class FakeImage:
    def __init__(self, labels=None, repo_digests=None):
        self.labels = labels or {}
        self.attrs = {"RepoDigests": repo_digests or []}


# The default fake image supports both profile contracts and has a repo
# digest — the healthy production shape. Tests for the contract gate and
# digest pinning swap it out.
DEFAULT_IMAGE = "ghcr.io/example/miragen:test"
DEFAULT_DIGEST = "ghcr.io/example/miragen@sha256:" + "ab" * 32


class FakeDocker:
    def __init__(self):
        self.container_map: dict[str, FakeContainer] = {}
        self.networks_created: list[str] = []
        self.image_map: dict[str, FakeImage] = {
            DEFAULT_IMAGE: FakeImage(
                labels={"io.miragen.profile-contracts": "1 2"},
                repo_digests=[DEFAULT_DIGEST],
            )
        }
        self.pulled: list[str] = []

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

        class _Images:
            def get(self, ref):
                if ref not in outer.image_map:
                    raise FakeNotFound(ref)
                return outer.image_map[ref]

            def pull(self, ref):
                outer.pulled.append(ref)
                if ref not in outer.image_map:
                    raise FakeNotFound(ref)

        self.containers = _Containers()
        self.networks = _Networks()
        self.images = _Images()

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
        sleep=lambda _s: None,
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
    # Since #75 the service pins the digest the contract check ran against,
    # not the re-taggable tag (test_compose_service_pins_the_validated_digest
    # covers the mechanism).
    assert service["image"] == DEFAULT_DIGEST
    assert service["networks"] == ["miragen-net"]
    # The internal token is forwarded so the agent's /run guard is armed.
    assert service["environment"]["MIRAGEN_INTERNAL_TOKEN"] == "shared-secret"
    assert service["environment"]["ANTHROPIC_API_KEY_FILE"] == "/run/secrets/anthropic_key"
    assert compose["secrets"] == {"anthropic_key": {"external": True}}


def test_env_passthrough_forwards_named_variables_only(tmp_path):
    """MIRAGEND_AGENT_ENV_PASSTHROUGH forwards exactly the operator-named,
    set-non-empty variables — subscription OAuth env tokens foremost, which
    the *_API_KEY suffix filter cannot see. Unlisted variables must not leak
    into agent containers, and listing a variable the daemon does not hold
    must forward nothing rather than an empty string."""
    docker_client = FakeDocker()
    runner = RecordingRunner()
    runner.on_up = lambda name: docker_client.add(name)
    core = LifecycleCore(
        tmp_path,
        docker_client,
        base_image="ghcr.io/example/miragen:test",
        environ={
            "MIRAGEND_AGENT_ENV_PASSTHROUGH": "CLAUDE_CODE_OAUTH_TOKEN, UNSET_ONE,,EMPTY_ONE",
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-example",
            "EMPTY_ONE": "",
            "UNLISTED_SECRET": "must-not-leak",
        },
        runner=runner,
        not_found=FakeNotFound,
        sleep=lambda _s: None,
    )
    core.create_agent("alpha", _yaml("alpha"))

    import yaml as pyyaml

    env = pyyaml.safe_load((tmp_path / "compose.yml").read_text())["services"]["alpha"][
        "environment"
    ]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-example"
    assert "UNSET_ONE" not in env
    assert "EMPTY_ONE" not in env
    assert "UNLISTED_SECRET" not in env


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
        sleep=lambda _s: None,
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
        sleep=lambda _s: None,
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
        sleep=lambda _s: None,
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
        sleep=lambda _s: None,
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
            sleep=lambda _s: None,
        ).import_agent("gamma", "exports/not-a-tarball.tar.gz")


# ── atomic creation: profile and tools checked together ──────────────────────

from miragen.daemon.core import InvalidInput, parse_registered_handlers  # noqa: E402

TOOLS_YAML = """\
name: {name}
mode: interactive
triggers:
  - type: http
tools:
  - fetch_page
spec:
  model: "test:whatever"
  instructions: "be helpful"
"""

HANDLER_YAML = """\
name: {name}
mode: autonomous
triggers:
  - type: cron
    schedule: "0 6 * * 1"
on_complete:
  notify: telegram
spec:
  model: "test:whatever"
  instructions: "be helpful"
"""

GOOD_TOOLS = '''\
from miragen import register


@register
async def fetch_page(ctx, url: str) -> str:
    """Fetch a page."""
    return url
'''

GOOD_HANDLER = '''\
from miragen import register_handler


@register_handler("telegram")
async def notify(agent, output):
    return None
'''


class TestParseRegisteredHandlers:
    def test_finds_named_handler(self):
        assert parse_registered_handlers(GOOD_HANDLER) == ["telegram"]

    def test_finds_sync_handler_too(self):
        src = 'from miragen import register_handler\n\n@register_handler("miradb")\ndef h(a, o):\n    pass\n'
        assert parse_registered_handlers(src) == ["miradb"]

    def test_ignores_undecorated_and_unparseable(self):
        assert parse_registered_handlers("def nope(): pass") == []
        assert parse_registered_handlers("def (((") == []


class TestCreateAgentRequiresSatisfiableTools:
    """A profile and its tools are checked together, before anything starts.

    Without this the daemon answered 201 for an agent that then failed on every
    boot — _inject_tools raises on unknown tool names, and the lifespan raises
    on unregistered on_complete handlers, while create_agent wrote an empty
    placeholder tools.py and started the container regardless.
    """

    def test_profile_with_tools_and_no_source_is_refused(self, core, tmp_path):
        with pytest.raises(InvalidInput, match="fetch_page"):
            core.create_agent("alpha", TOOLS_YAML.format(name="alpha"))
        assert not (tmp_path / "agents" / "alpha").exists()

    def test_profile_with_handler_and_no_source_is_refused(self, core, tmp_path):
        with pytest.raises(InvalidInput, match="telegram"):
            core.create_agent("alpha", HANDLER_YAML.format(name="alpha"))
        assert not (tmp_path / "agents" / "alpha").exists()

    def test_matching_tools_source_creates_and_is_written(self, core, tmp_path):
        core.create_agent("alpha", TOOLS_YAML.format(name="alpha"), GOOD_TOOLS)
        written = (tmp_path / "agents" / "alpha" / "tools.py").read_text()
        assert "async def fetch_page" in written

    def test_matching_handler_source_creates(self, core, tmp_path):
        core.create_agent("alpha", HANDLER_YAML.format(name="alpha"), GOOD_HANDLER)
        assert (tmp_path / "agents" / "alpha" / "tools.py").read_text() == GOOD_HANDLER

    def test_partial_source_names_what_is_missing(self, core):
        with pytest.raises(InvalidInput) as exc:
            core.create_agent("alpha", TOOLS_YAML.format(name="alpha"), GOOD_HANDLER)
        message = str(exc.value)
        assert "fetch_page" in message and "telegram" in message

    def test_plain_profile_still_gets_the_placeholder(self, core, tmp_path):
        core.create_agent("alpha", _yaml("alpha"))
        written = (tmp_path / "agents" / "alpha" / "tools.py").read_text()
        assert written == "from miragen import register\n\n# Tools for alpha\n"

    def test_post_to_alone_needs_no_handler(self, core):
        yaml_source = HANDLER_YAML.format(name="alpha").replace(
            "  notify: telegram", "  post_to: https://example.invalid/hook"
        )
        core.create_agent("alpha", yaml_source)

    def test_summary_reports_referenced_handlers(self):
        summary = validate_profile_text(HANDLER_YAML.format(name="alpha"))
        assert summary["handlers"] == ["telegram"]


# ── profile↔runtime contract (#75) ───────────────────────────────────────────


EXECUTOR_YAML = """\
name: {name}
mode: interactive
triggers:
  - type: http
executor:
  executor: claude-code
  instructions: "review things"
"""


def test_create_refused_when_image_lacks_required_contract(
    core, docker_client, runner, tmp_path
):
    # An executor-tier profile (contract 2) against an image declaring only
    # contract 1: the motivating failure of #75, now refused at create with
    # the image named — not a pydantic traceback in a crash-looping
    # container.
    docker_client.image_map[DEFAULT_IMAGE] = FakeImage(
        labels={"io.miragen.profile-contracts": "1"}
    )
    from miragen.daemon.core import ImageContractUnsupported

    with pytest.raises(ImageContractUnsupported) as exc:
        core.create_agent("alpha", EXECUTOR_YAML.format(name="alpha"))
    assert "requires contract 2" in str(exc.value)
    assert DEFAULT_IMAGE in str(exc.value)
    # Nothing half-created: no workspace, no compose service, no container.
    assert not (tmp_path / "agents" / "alpha").exists()
    assert all(cmd[:4] != ["docker", "compose", "up", "-d"] for cmd in runner.calls)
    assert "alpha" not in docker_client.container_map


def test_unlabeled_image_reads_as_contract_1_only(core, docker_client):
    # An image that predates contract labeling is a pre-executor-tier
    # runtime: model-tier profiles pass, executor-tier profiles are refused.
    docker_client.image_map[DEFAULT_IMAGE] = FakeImage(labels={})
    from miragen.daemon.core import ImageContractUnsupported

    core.create_agent("modeltier", _yaml("modeltier"))  # contract 1: fine
    with pytest.raises(ImageContractUnsupported):
        core.create_agent("exectier", EXECUTOR_YAML.format(name="exectier"))


def test_absent_image_is_pulled_before_judgement(core, docker_client):
    # The contract must be judged against what will run; an image not on
    # the host is pulled first, not judged blind or waved through.
    image = docker_client.image_map.pop(DEFAULT_IMAGE)

    def pull(ref):
        docker_client.image_map[ref] = image

    docker_client.images.pull = pull
    core.create_agent("alpha", EXECUTOR_YAML.format(name="alpha"))
    assert "alpha" in docker_client.container_map


def test_compose_service_pins_the_validated_digest(core, tmp_path):
    import yaml as _yaml_mod

    core.create_agent("alpha", _yaml("alpha"))
    data = _yaml_mod.safe_load((tmp_path / "compose.yml").read_text())
    service = data["services"]["alpha"]
    # What was validated is what runs: the digest, not the re-taggable tag.
    assert service["image"] == DEFAULT_DIGEST
    assert service["labels"]["io.miragen.managed-by"] == "miragend"


def test_digestless_image_falls_back_to_the_tag(core, docker_client, tmp_path):
    import yaml as _yaml_mod

    docker_client.image_map[DEFAULT_IMAGE] = FakeImage(
        labels={"io.miragen.profile-contracts": "1 2"}, repo_digests=[]
    )
    core.create_agent("alpha", _yaml("alpha"))
    data = _yaml_mod.safe_load((tmp_path / "compose.yml").read_text())
    assert data["services"]["alpha"]["image"] == DEFAULT_IMAGE


def test_boot_failure_rolls_back_and_carries_the_logs(
    core, docker_client, runner, tmp_path
):
    # The container starts, the runtime dies, the restart policy flips it
    # to 'restarting': create fails with the log excerpt and leaves nothing
    # behind — no workspace, no compose service, no crash-looping container.
    def on_up(name):
        container = docker_client.add(name)
        container.status = "restarting"

    runner.on_up = on_up
    from miragen.daemon.core import AgentBootFailed

    with pytest.raises(AgentBootFailed) as exc:
        core.create_agent("alpha", _yaml("alpha"))
    assert "failed first boot" in str(exc.value)
    assert "log line for alpha" in str(exc.value)
    assert not (tmp_path / "agents" / "alpha").exists()
    assert "alpha" not in docker_client.container_map
    import yaml as _yaml_mod

    compose = _yaml_mod.safe_load((tmp_path / "compose.yml").read_text()) or {}
    assert "alpha" not in (compose.get("services") or {})


def test_update_refused_when_new_profile_exceeds_image_contract(
    core, docker_client
):
    # Updating a running model-tier agent to an executor profile on a
    # contract-1 image must refuse BEFORE disturbing the running agent.
    core.create_agent("alpha", _yaml("alpha"))
    docker_client.image_map[DEFAULT_IMAGE] = FakeImage(
        labels={"io.miragen.profile-contracts": "1"}
    )
    from miragen.daemon.core import ImageContractUnsupported

    container = docker_client.container_map["alpha"]
    with pytest.raises(ImageContractUnsupported):
        core.update_agent_config("alpha", EXECUTOR_YAML.format(name="alpha"))
    assert container.restarted == 0  # the running agent was never touched


def test_validate_summary_reports_the_required_contract():
    assert validate_profile_text(_yaml("alpha"))["profile_contract"] == 1
    assert (
        validate_profile_text(EXECUTOR_YAML.format(name="alpha"))["profile_contract"]
        == 2
    )


def test_stale_cached_tag_is_refreshed_before_refusing(core, docker_client):
    # The real-world shape of #75's failure: the registry has a capable
    # runtime, the host's cached `latest` is last month's. Refusing an
    # operator who is already pointed at a good tag helps nobody — the
    # daemon re-pulls the mutable reference and proceeds.
    docker_client.image_map[DEFAULT_IMAGE] = FakeImage(
        labels={"io.miragen.profile-contracts": "1"}
    )

    def pull(ref):
        docker_client.pulled.append(ref)
        docker_client.image_map[ref] = FakeImage(
            labels={"io.miragen.profile-contracts": "1 2"},
            repo_digests=[DEFAULT_DIGEST],
        )

    docker_client.images.pull = pull

    core.create_agent("alpha", EXECUTOR_YAML.format(name="alpha"))
    assert docker_client.pulled == [DEFAULT_IMAGE]
    assert "alpha" in docker_client.container_map


def test_refusal_survives_the_refresh_when_the_registry_agrees(
    core, docker_client, tmp_path
):
    # Refreshed and still incapable: that is a real answer about the
    # registry, and the error says so rather than sending the operator to
    # re-pull something the daemon already pulled.
    docker_client.image_map[DEFAULT_IMAGE] = FakeImage(
        labels={"io.miragen.profile-contracts": "1"}
    )
    docker_client.images.pull = lambda ref: docker_client.pulled.append(ref)
    from miragen.daemon.core import ImageContractUnsupported

    with pytest.raises(ImageContractUnsupported) as exc:
        core.create_agent("alpha", EXECUTOR_YAML.format(name="alpha"))
    assert docker_client.pulled == [DEFAULT_IMAGE]  # tried exactly once
    assert "registry's current answer" in str(exc.value)
    assert not (tmp_path / "agents" / "alpha").exists()


def test_digest_pinned_base_image_is_never_re_pulled(tmp_path, docker_client, runner):
    # A digest cannot be re-pointed upstream, so a refresh could only
    # return the same bytes — re-pulling would be pure latency.
    digest_ref = DEFAULT_DIGEST
    docker_client.image_map[digest_ref] = FakeImage(
        labels={"io.miragen.profile-contracts": "1"}
    )
    docker_client.images.pull = lambda ref: docker_client.pulled.append(ref)
    pinned = LifecycleCore(
        tmp_path,
        docker_client,
        base_image=digest_ref,
        runner=runner,
        not_found=FakeNotFound,
        sleep=lambda _s: None,
    )
    from miragen.daemon.core import ImageContractUnsupported

    with pytest.raises(ImageContractUnsupported):
        pinned.create_agent("alpha", EXECUTOR_YAML.format(name="alpha"))
    assert docker_client.pulled == []
