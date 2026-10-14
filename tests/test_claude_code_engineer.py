"""The Claude Code harness's postures: the locked default is byte-identical
to before the engineer posture existed (golden captured on the unmodified
harness), and the engineer posture's options, profile validation, worktree
lifecycle, git/gh wiring, subagent depth hook and ask_human request shape.

Everything here is bounded: git runs against local temp repositories
(file:// URLs, subprocess timeouts), HTTP against an httpx MockTransport."""

from __future__ import annotations

import json
import os
import subprocess
from collections import deque
from pathlib import Path

import pytest

sdk = pytest.importorskip("claude_agent_sdk")

import httpx  # noqa: E402

from miragen.harness import build_model_harness  # noqa: E402
from miragen.harness.claude_code import (  # noqa: E402
    ClaudeCodeHarness, ClaudeCodeHarnessError, ClaudeCodeSettings,
)
from miragen.harness.engineer import (  # noqa: E402
    GITCONFIG_FILE, AskHumanFailed, SubagentDepthPolicy, WorkspaceError, _inside,
    _provision_sync, _work_item_ref, build_ask_human_tool, git_env, remove_workdir,
    write_git_files,
)
from miragen.harness.gateway import ToolGateway  # noqa: E402
from miragen.memory.claude_code import SCRUBBED_ENV  # noqa: E402
from miragen.models import (  # noqa: E402
    AgentProfile, AskHumanMiraDesignSpec, EngineerRepository, HarnessSpec, passthrough_refused,
)
from tests import claude_code_snapshot as snap  # noqa: E402
from tests.test_claude_code_harness import FakeClient, turn  # noqa: E402

GOLDEN = Path(__file__).parent / "fixtures" / "claude_code_locked_options.json"
GIT_TIMEOUT = 30


# ── helpers ─────────────────────────────────────────────────────────────────

def engineer_profile(**harness) -> AgentProfile:
    return snap.profile({}, harness={"posture": "engineer", **harness})


def git(*args, cwd=None) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "HOME": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          check=True, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL).stdout.strip()


@pytest.fixture
def origin(tmp_path) -> Path:
    """A local 'remote' repository with one commit on main."""
    repo = tmp_path / "origin" / "lane-repo"
    repo.mkdir(parents=True)
    git("init", "--quiet", "-b", "main", str(repo))
    (repo / "README.md").write_text("hello\n")
    git("add", "README.md", cwd=repo)
    git("commit", "--quiet", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("GIT_", "GH_")) or key in SCRUBBED_ENV:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-sub")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-api")
    monkeypatch.setenv("UPSTREAM_SECRET", "s3cret")


class Env:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.home = tmp_path / "claude-home"
        self.harnesses: list[ClaudeCodeHarness] = []
        FakeClient.instances = []

    def harness(self, prof) -> ClaudeCodeHarness:
        settings = ClaudeCodeSettings(claude_home=self.home, workdirs=self.tmp_path / "work",
                                      gateway_url="http://127.0.0.1:9/mcp/gateway/",
                                      cli_path="/usr/bin/env")
        h = ClaudeCodeHarness(prof, ToolGateway(
            prof, native_capabilities=ClaudeCodeHarness.native_capabilities), settings,
            client_factory=lambda o: FakeClient(o, self.home, lambda c, p: _echo(c, p)))
        self.harnesses.append(h)
        return h


def _echo(client, prompt):
    yield sdk.AssistantMessage(content=[sdk.TextBlock(text="ok")], model="sonnet")
    yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id=client.session_id, result="ok",
                            usage={"input_tokens": 1, "output_tokens": 1})


@pytest.fixture
async def env(tmp_path, hermetic):
    e = Env(tmp_path)
    yield e
    for h in e.harnesses:
        await h.aclose()


# ── the locked default ──────────────────────────────────────────────────────

def test_locked_posture_options_are_byte_identical_to_before():
    golden = GOLDEN.read_text()
    now = json.dumps(snap.locked_snapshots(), indent=1, sort_keys=True) + "\n"
    assert now == golden


def test_an_explicit_locked_posture_launches_exactly_like_no_block(tmp_path, hermetic):
    def options(prof, root):
        settings = ClaudeCodeSettings(claude_home=root / "home", workdirs=root / "work",
                                      gateway_url="http://127.0.0.1:9/mcp/gateway/",
                                      cli_path="/usr/bin/env")
        h = ClaudeCodeHarness(prof, ToolGateway(prof), settings)
        o = h._options("chat", root / "work" / "chat", resume=None, new_id="x", stderr=deque())
        return snap.render(h, o, root)

    plain = options(snap.profile({}), tmp_path / "a")
    locked = options(snap.profile({}, harness={"posture": "locked"}), tmp_path / "b")
    assert plain == locked


# ── profile validation ──────────────────────────────────────────────────────

def test_the_harness_block_implies_contract_4_and_is_not_dumped_when_absent():
    assert snap.profile({}).required_contract() == 1
    assert "harness" not in snap.profile({}).model_dump()["spec"]
    assert engineer_profile().required_contract() == 4
    assert snap.profile({}, harness={}).required_contract() == 4
    with pytest.raises(ValueError, match="v4"):
        AgentProfile.model_validate({**engineer_profile().model_dump(exclude_none=True),
                                     "apiVersion": "miragen/v3"})


@pytest.mark.parametrize("harness, model, extra, message", [
    ({"posture": "engineer"}, "grok-build:grok-4.6", {}, "claude-code"),
    ({"repositories": [{"url": "https://b/o/r.git"}]}, None, {}, "only apply to posture"),
    ({"subagent_depth": 2}, None, {}, "only apply to posture"),
    # Userinfo assembled at runtime so secret scanners don't read it as a leaked URI.
    ({"posture": "engineer", "repositories": [{"url": "https://" + "user:pw" + "@b/o/r.git"}]},
     None, {}, "credentials"),
    ({"posture": "engineer", "repositories": [{"url": "ssh://git@github.com/o/r"}]}, None, {},
     "https"),
    ({"posture": "engineer", "repositories": [{"url": "https://b/a/r.git"},
                                              {"url": "https://b/c/r"}]}, None, {}, "share"),
    ({"posture": "engineer", "env_passthrough": ["ANTHROPIC_API_KEY"]}, None, {}, "may not"),
    ({"posture": "engineer", "env_passthrough": ["GH_ENTERPRISE_TOKEN"]}, None, {}, "may not"),
    ({"posture": "engineer", "subagent_depth": -1}, None, {}, "greater than"),
    ({"posture": "engineer", "ask_human": {"url": "https://md", "project": "p"}}, None, {},
     "exactly one"),
    ({"posture": "engineer"}, None, {"agent_type": "external"}, None),
])
def test_invalid_harness_blocks_are_refused(harness, model, extra, message):
    data = snap.profile({}).model_dump(exclude_none=True)
    data["spec"]["harness"] = harness
    if model:
        data["spec"]["model"] = model
    data.update(extra)
    with pytest.raises(ValueError, match=message):
        AgentProfile.model_validate(data)


@pytest.mark.parametrize("name", [
    *SCRUBBED_ENV, "MIRAGEN_INTERNAL_TOKEN", "MIRAGEN_WORKER", "MIRAGEN_BROKER_CREDENTIAL_FILE",
    "MIRAGEN_GATEWAY_TOKEN", "MIRAGEN_ENV_KEEP", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_ASKPASS", "GIT_SSL_CAINFO", "GIT_CONFIG_GLOBAL", "GH_HOST", "GH_ENTERPRISE_TOKEN",
    "SSL_CERT_FILE", "OPENAI_API_KEY", "LD_PRELOAD", "LD_LIBRARY_PATH", "NODE_OPTIONS",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "HOME", "PATH", "GITHUB_TOKEN",
    "SOME_SECRET", "HTTPS_PROXY",
])
def test_passthrough_refuses_secrets_and_harness_configuration(name):
    assert passthrough_refused(name)
    with pytest.raises(ValueError, match="may not be passed"):
        HarnessSpec(posture="engineer", env_passthrough=[name])


def test_passthrough_allows_plain_plugin_configuration():
    assert HarnessSpec(posture="engineer",
                       env_passthrough=["MIRADESIGN_URL", "PROJECT_KEY"]).env_passthrough


@pytest.mark.parametrize("url", [
    "https://b/o/..", "https://b/o/...git", "https://b/o/.", "https://b/o/.git",
    "https://b/o/%2e%2e", "https://b/o/.hidden", "file:///tmp/o/..", "https://b/",
])
def test_urls_whose_derived_directory_is_not_a_plain_name_are_refused(url):
    with pytest.raises(ValueError):
        EngineerRepository(url=url)


def test_urls_with_a_query_are_refused():
    with pytest.raises(ValueError, match="query"):
        EngineerRepository(url="https://b/o/r.git?x=/../")


def test_an_explicit_name_rescues_an_unusable_url_tail():
    assert EngineerRepository(url="https://b/o/.git", name="repo").dir_name == "repo"


@pytest.mark.parametrize("harness", [
    None,
    {"posture": "locked"},
    {},
    {"posture": "engineer", "subagent_depth": 2, "plugin_dirs": ["/p"],
     "env_passthrough": ["MIRADESIGN_URL"], "repositories": [{"url": "https://b/o/r.git"}],
     "git_broker": {"host": "b", "credential_file": "/run/secrets/c"},
     "ask_human": {"url": "https://md", "project": "p", "token_file": "/run/secrets/t"}},
])
@pytest.mark.parametrize("mode", ["python", "json"])
def test_profiles_survive_a_serialization_round_trip(harness, mode):
    prof = snap.profile({}, **({"harness": harness} if harness is not None else {}))
    again = AgentProfile.model_validate(prof.model_dump(mode=mode))
    assert again == prof and again.required_contract() == prof.required_contract()


# ── engineer launch options ─────────────────────────────────────────────────

async def test_engineer_options_open_the_tool_set_and_load_settings(env, monkeypatch, tmp_path):
    cred = tmp_path / "broker-cred"
    cred.write_text("broker-secret-1\n")
    monkeypatch.setenv("MIRADESIGN_URL", "https://design.example")
    prof = engineer_profile(
        plugin_dirs=["/opt/plugins/miradesign"], env_passthrough=["MIRADESIGN_URL", "UNSET_ONE"],
        git_broker={"host": "broker.internal:8443", "credential_file": str(cred),
                    "ca_bundle": "/etc/ssl/broker-bundle.pem"})
    h = env.harness(prof)
    await h.run(turn("hi"))
    o = FakeClient.instances[0].options
    view = snap.render(h, o, tmp_path)
    argv = view["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--tools") + 1] == "default"
    # User settings only: a worktree's .claude/settings.json is never loaded.
    assert "--setting-sources=user" in argv
    assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": False}
    assert "--strict-mcp-config" not in argv
    assert "--append-system-prompt" in argv and "You are Mira." in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["gw"]["headers"] == {
        "Authorization": "Bearer ${MIRAGEN_GATEWAY_TOKEN}"}
    assert o.can_use_tool is None and o.skills == "all"
    assert o.plugins == [{"type": "local", "path": "/opt/plugins/miradesign"}]
    assert view["hooks"]["PreToolUse"] == [{"matcher": "Agent|Task", "hooks": 1}]
    assert set(view["hooks"]) == {"PreCompact", "PreToolUse", "PostToolUse",
                                  "PostToolUseFailure", "SubagentStart", "SubagentStop"}
    # git/gh wiring, the passthrough, and still subscription only.
    e = o.env
    assert e["GIT_CONFIG_GLOBAL"] == str(env.home / GITCONFIG_FILE)
    assert e["GH_HOST"] == "broker.internal:8443" and e["GH_ENTERPRISE_TOKEN"] == "broker-secret-1"
    assert e["GIT_SSL_CAINFO"] == e["SSL_CERT_FILE"] == "/etc/ssl/broker-bundle.pem"
    assert e["MIRADESIGN_URL"] == "https://design.example" and "UNSET_ONE" not in e
    assert "MIRAGEN_WORKER" not in e  # no miragen memory: the plugin's hooks capture
    keep = e["MIRAGEN_ENV_KEEP"].split()
    assert {"GH_ENTERPRISE_TOKEN", "GIT_CONFIG_GLOBAL", "MIRADESIGN_URL"} <= set(keep)
    assert not set(keep) & set(SCRUBBED_ENV) and "UPSTREAM_SECRET" not in keep
    assert e["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-sub"
    # No secret on the command line.
    assert not any("broker-secret-1" in arg or "sk-sub" in arg for arg in argv)
    # The wrapper drops everything outside the allowlist.
    out = subprocess.run([o.cli_path], env={**e, "PATH": "/usr/bin:/bin",
                                            "ANTHROPIC_API_KEY": "sk-api", "UPSTREAM_SECRET": "x"},
                         capture_output=True, text=True, check=True, timeout=GIT_TIMEOUT).stdout
    names = {line.split("=", 1)[0] for line in out.splitlines()}
    assert "ANTHROPIC_API_KEY" not in names and "UPSTREAM_SECRET" not in names
    assert {"GH_ENTERPRISE_TOKEN", "MIRADESIGN_URL", "GIT_CONFIG_GLOBAL"} <= names
    # The harness's own files are read-only (against accidental rewrites).
    for name, mode in (("miragen-claude", 0o500), (GITCONFIG_FILE, 0o400),
                       ("miragen-git-credential", 0o500)):
        assert (env.home / name).stat().st_mode & 0o777 == mode, name


async def test_a_second_prepare_rewrites_the_read_only_files(env):
    prof = engineer_profile(git_broker={"host": "b", "credential_file": "/nonexistent"})
    env.harness(prof)
    env.harness(prof)  # would raise PermissionError on a 0o500 wrapper
    assert (env.home / "miragen-claude").stat().st_mode & 0o777 == 0o500


async def test_passthrough_never_overrides_what_the_harness_sets(env, monkeypatch, tmp_path):
    cred = tmp_path / "cred"
    cred.write_text("broker-secret-9\n")
    monkeypatch.setenv("GH_HOST", "evil.example")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/evil")
    prof = engineer_profile(git_broker={"host": "broker.internal", "credential_file": str(cred)})
    # Bypass validation (which refuses these names) to test the spawn rule itself.
    spec = prof.spec.harness
    object.__setattr__(spec, "env_passthrough", ["GH_HOST", "GIT_CONFIG_GLOBAL"])
    h = env.harness(prof)
    await h.run(turn("hi"))
    e = FakeClient.instances[0].options.env
    assert e["GH_HOST"] == "broker.internal"
    assert e["GIT_CONFIG_GLOBAL"] == str(env.home / GITCONFIG_FILE)


async def test_engineer_with_miragen_memory_keeps_the_plugin_quiet(env):
    data = engineer_profile().model_dump(exclude_none=True)
    data["memory"] = {"backend": "bridge", "project": "lane"}
    h = env.harness(AgentProfile.model_validate(data))
    await h.run(turn("hi"))
    assert FakeClient.instances[0].options.env["MIRAGEN_WORKER"] == "1"


# ── git configuration ───────────────────────────────────────────────────────

def test_gitconfig_routes_github_to_the_broker_and_scopes_the_credential(tmp_path, hermetic):
    cred = tmp_path / "cred"
    cred.write_text("broker-secret-2\n")
    prof = engineer_profile(git_broker={"host": "broker.internal", "credential_file": str(cred),
                                        "commit_name": "Lane Bot"})
    home = tmp_path / "home"
    home.mkdir()
    config = write_git_files(home, prof)
    text = config.read_text()
    assert "broker-secret-2" not in text
    assert git("config", "--file", str(config), "--get-all",
               "url.https://broker.internal/.insteadof").splitlines() == [
        "https://github.com/", "git@github.com:", "ssh://git@github.com/"]
    assert git("config", "--file", str(config), "user.name") == "Lane Bot"
    env = {**git_env(home, prof), "PATH": os.environ["PATH"], "HOME": str(tmp_path)}

    def fill(host: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "credential", "fill"], env=env, text=True,
                              input=f"protocol=https\nhost={host}\n\n", capture_output=True,
                              timeout=GIT_TIMEOUT)

    got = fill("broker.internal")
    assert got.returncode == 0 and "password=broker-secret-2" in got.stdout
    other = fill("github.com")  # never handed the broker credential (and no prompt)
    assert "broker-secret-2" not in other.stdout and other.returncode != 0


# ── worktree lifecycle ──────────────────────────────────────────────────────

async def test_each_instance_gets_its_own_worktree_and_delete_removes_it(env, origin, tmp_path):
    cred = tmp_path / "cred"
    cred.write_text("broker-secret-3\n")
    prof = engineer_profile(repositories=[{"url": origin.as_uri()}],
                            git_broker={"host": "broker.internal", "credential_file": str(cred)})
    h = env.harness(prof)
    await h.run(turn("one", instance="a"))
    await h.run(turn("two", instance="b", run_id="r2"))
    tree_a = env.tmp_path / "work" / "a" / "lane-repo"
    tree_b = env.tmp_path / "work" / "b" / "lane-repo"
    assert [c.options.cwd for c in FakeClient.instances] == [str(tree_a), str(tree_b)]
    head = git("rev-parse", "HEAD", cwd=origin)
    for tree in (tree_a, tree_b):
        assert (tree / "README.md").read_text() == "hello\n"
        assert git("rev-parse", "HEAD", cwd=tree) == head
        assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree) == "HEAD"  # detached
    mirror = env.home / "repos" / "lane-repo.git"
    assert git("rev-parse", "--is-bare-repository", cwd=mirror) == "true"
    assert git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=mirror) == "origin/main"
    # No credential anywhere in git's own files.
    for root in (mirror, tree_a):
        for path in root.rglob("*"):
            if path.is_file():
                assert b"broker-secret-3" not in path.read_bytes(), path

    # The agent's work survives a process restart (resume) untouched.
    (tree_a / "work.txt").write_text("in progress\n")
    git("checkout", "--quiet", "-b", "agent/1-thing", cwd=tree_a)
    await h.aclose()
    h2 = env.harness(prof)
    await h2.run(turn("three", instance="a", run_id="r3"))
    assert (tree_a / "work.txt").read_text() == "in progress\n"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree_a) == "agent/1-thing"

    removed = await h2.forget("a")
    assert "workdir" in removed and not (env.tmp_path / "work" / "a").exists()
    listed = git("worktree", "list", "--porcelain", cwd=mirror)
    assert str(tree_a) not in listed and str(tree_b) in listed
    # The branch the agent made is still in the shared clone (pushed or not).
    assert git("rev-parse", "--verify", "--quiet", "refs/heads/agent/1-thing", cwd=mirror)


async def test_a_new_instance_starts_at_the_latest_default_branch(env, origin):
    prof = engineer_profile(repositories=[{"url": origin.as_uri(), "name": "app"}])
    h = env.harness(prof)
    await h.run(turn("one", instance="a"))
    (origin / "NEW.md").write_text("new\n")
    git("add", "NEW.md", cwd=origin)
    git("commit", "--quiet", "-m", "second", cwd=origin)
    await h.run(turn("two", instance="b", run_id="r2"))
    assert (env.tmp_path / "work" / "b" / "app" / "NEW.md").exists()
    assert not (env.tmp_path / "work" / "a" / "app" / "NEW.md").exists()


async def test_several_repositories_share_one_instance_directory(env, origin, tmp_path):
    second = tmp_path / "origin" / "other"
    second.mkdir()
    git("init", "--quiet", "-b", "trunk", str(second))
    (second / "x").write_text("x")
    git("add", "x", cwd=second)
    git("commit", "--quiet", "-m", "x", cwd=second)
    prof = engineer_profile(repositories=[{"url": origin.as_uri()}, {"url": second.as_uri()}])
    h = env.harness(prof)
    await h.run(turn("one", instance="a"))
    root = env.tmp_path / "work" / "a"
    assert FakeClient.instances[0].options.cwd == str(root)
    assert (root / "lane-repo" / "README.md").exists() and (root / "other" / "x").exists()


async def test_ephemeral_engineer_runs_leave_no_worktree_behind(env, origin):
    h = env.harness(engineer_profile(repositories=[{"url": origin.as_uri()}]))
    await h.run(turn("once", use_history=False, run_id="eph"))
    assert not (env.tmp_path / "work" / "run-eph").exists()
    mirror = env.home / "repos" / "lane-repo.git"
    assert git("worktree", "list", "--porcelain", cwd=mirror).count("worktree ") == 1  # bare only


async def test_an_unreachable_repository_fails_the_turn_cleanly(env, tmp_path):
    h = env.harness(engineer_profile(repositories=[{"url": (tmp_path / "nope").as_uri()}]))
    with pytest.raises(ClaudeCodeHarnessError, match="workspace"):
        await h.run(turn("one", instance="a"))
    assert not FakeClient.instances
    assert not list((env.home / "repos").glob("*")) if (env.home / "repos").exists() else True


def test_a_workspace_path_outside_the_instance_directory_is_refused(tmp_path):
    inst = tmp_path / "work" / "a"
    inst.mkdir(parents=True)
    for bad in ("..", ".", "", "x/../..", "../b"):
        with pytest.raises(WorkspaceError, match="outside"):
            _inside(inst, bad)
    # A worktree replaced by a symlink pointing elsewhere is refused too.
    (tmp_path / "elsewhere").mkdir()
    (inst / "repo").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(WorkspaceError, match="outside"):
        _inside(inst, "repo")


def test_an_unsafe_name_never_deletes_a_sibling_instance(tmp_path, origin, hermetic):
    """The reviewer's reproduction: a URL whose tail is '..' made the
    'half-made worktree' rmtree hit the workdirs directory. Validation
    refuses it now; the provisioning check holds even without validation."""
    sibling = tmp_path / "work" / "b"
    sibling.mkdir(parents=True)
    (sibling / "keep.txt").write_text("other instance's work")
    repo = EngineerRepository.model_construct(url=origin.as_uri(), name="..")
    with pytest.raises(WorkspaceError):
        _provision_sync(tmp_path / "home", tmp_path / "work" / "a", [repo], {})
    assert (sibling / "keep.txt").read_text() == "other instance's work"


def test_os_errors_while_provisioning_are_workspace_errors(tmp_path, origin):
    blocker = tmp_path / "work"
    blocker.write_text("a file where the workdirs directory should be")
    repo = EngineerRepository(url=origin.as_uri())
    with pytest.raises(WorkspaceError, match="Error"):
        _provision_sync(tmp_path / "home", blocker / "a", [repo], {})


def test_best_effort_cleanup_keeps_going_past_errors(tmp_path):
    work = tmp_path / "w"
    locked = work / "locked"
    locked.mkdir(parents=True)
    (locked / "f").write_text("x")
    (work / "other").write_text("y")
    locked.chmod(0o500)
    try:
        with pytest.raises(OSError):
            remove_workdir(work)
        assert remove_workdir(work, ignore_errors=True)  # no exception
    finally:
        locked.chmod(0o700)


# ── subagent depth ──────────────────────────────────────────────────────────

def _call(tool="Agent", agent_id=None, tool_use_id="t1"):
    data = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {},
            "tool_use_id": tool_use_id, "session_id": "s", "transcript_path": "", "cwd": "/"}
    if agent_id:
        data["agent_id"] = agent_id
    return data


def _denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


async def test_default_depth_allows_subagents_but_not_nested_ones():
    p = SubagentDepthPolicy(1)
    assert not _denied(await p.pre_tool_use(_call(tool_use_id="t1"), "t1", None))
    await p.subagent_start({"agent_id": "a1", "agent_type": "general-purpose"}, None, None)
    assert _denied(await p.pre_tool_use(_call(agent_id="a1", tool_use_id="t2"), "t2", None))
    assert _denied(await p.pre_tool_use(_call(tool="Task", agent_id="a1"), "t3", None))
    # Non-subagent tools inside a subagent are not this hook's business.
    assert await p.pre_tool_use(_call(tool="Bash", agent_id="a1"), "t4", None) == {}
    # A caller the policy never saw start is treated as at the limit.
    assert _denied(await p.pre_tool_use(_call(agent_id="ghost"), "t5", None))
    await p.post_tool_use(_call(tool_use_id="t1"), "t1", None)
    await p.subagent_stop({"agent_id": "a1"}, None, None)
    assert not _denied(await p.pre_tool_use(_call(tool_use_id="t6"), "t6", None))


async def test_depth_zero_refuses_every_subagent_and_two_allows_one_nesting():
    assert _denied(await SubagentDepthPolicy(0).pre_tool_use(_call(), "t1", None))
    p = SubagentDepthPolicy(2)
    assert not _denied(await p.pre_tool_use(_call(tool_use_id="t1"), "t1", None))
    await p.subagent_start({"agent_id": "a1"}, None, None)
    assert not _denied(await p.pre_tool_use(_call(agent_id="a1", tool_use_id="t2"), "t2", None))
    await p.subagent_start({"agent_id": "a2"}, None, None)  # deepest pending spawn: depth 2
    assert _denied(await p.pre_tool_use(_call(agent_id="a2", tool_use_id="t3"), "t3", None))


async def test_a_subagent_context_without_an_agent_id_is_unknown():
    p = SubagentDepthPolicy(3)
    data = _call()
    data["agent_type"] = "general-purpose"  # inside a subagent, but its id is missing
    assert _denied(await p.pre_tool_use(data, "t1", None))


async def test_a_subagent_starting_with_no_spawn_in_flight_is_at_the_limit():
    p = SubagentDepthPolicy(3)
    await p.subagent_start({"agent_id": "orphan"}, None, None)
    assert _denied(await p.pre_tool_use(_call(agent_id="orphan"), "t1", None))


# ── ask_human ───────────────────────────────────────────────────────────────

class FakeMiraDesign:
    def __init__(self, *, thread_id="th-1234567890", fail=None, actors=("ilari", "eemeli"),
                 message_status=201):
        self.requests: list[httpx.Request] = []
        self.thread_id, self.fail, self.actors = thread_id, fail, set(actors)
        self.message_status = message_status
        self.threads: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.fail and self.fail in path:
            return httpx.Response(404, json={"error": "no such work item"})
        if request.method == "GET" and path.startswith("/v1/actors/"):
            handle = path.rsplit("/", 1)[-1]
            return (httpx.Response(200, json={"handle": handle}) if handle in self.actors
                    else httpx.Response(404, json={"error": "no actor"}))
        if request.method == "GET" and "/work-items/" in path:
            return httpx.Response(200, json={"id": "wi-1", "number": 12,
                                             "thread_id": self.thread_id})
        if request.method == "GET" and path.endswith("/threads"):
            return httpx.Response(200, json=self.threads)
        if request.method == "POST" and path.endswith("/threads"):
            body = json.loads(request.content)
            row = {"id": f"th-new-{len(self.threads):04d}", "kind": body["kind"],
                   "subject": body["subject"], "target_id": body.get("target_id")}
            self.threads.append(row)
            return httpx.Response(201, json=row)
        if path.endswith("/messages"):
            if self.message_status != 201:
                return httpx.Response(self.message_status, json={"error": "conflict"})
            return httpx.Response(201, json={"id": "msg-abcdef12", "thread_id": "x",
                                             "escalated": "sink"})
        return httpx.Response(500)

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def sent(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]


def _ask(fake, *, token_env="MD_TOKEN", environ=None, current=None, **spec):
    md = AskHumanMiraDesignSpec(url="https://design.example", project="lane",
                                token_env=token_env, **spec)
    return build_ask_human_tool(md, "eng", current or (lambda: ("run-1", "inst-a")),
                                environ={"MD_TOKEN": "md-secret"} if environ is None else environ,
                                transport=httpx.MockTransport(fake))


async def test_ask_human_on_a_work_item_posts_a_question_to_its_thread():
    fake = FakeMiraDesign()
    out = await _ask(fake)("Merge the schema change now or wait for review?", work_item="#12")
    assert fake.calls() == [("GET", "/v1/actors/ilari"),
                            ("GET", "/v1/projects/lane/work-items/12"),
                            ("POST", "/v1/threads/th-1234567890/messages")]
    assert {r.headers["authorization"] for r in fake.requests} == {"Bearer md-secret"}
    (post,) = fake.sent("POST", "/messages")
    assert post.headers["idempotency-key"].startswith("miragen-ask-")
    body = json.loads(post.content)
    assert body["kind"] == "question" and body["mentions"] == ["ilari"]
    assert body["body"] == "Merge the schema change now or wait for review?"
    assert body["meta"] == {"source": "miragen.ask_human", "agent": "eng", "instance": "inst-a",
                            "run_id": "run-1", "work_item": "#12"}
    assert out.startswith("ok: question msg-abcd") and "Do not wait" in out and "@ilari" in out


@pytest.mark.parametrize("ref, expected", [
    ("12", ("lane", 12)), ("#12", ("lane", 12)), ("7", ("lane", 7)),
    ("miragen#12", ("miragen", 12)), (" other#7 ", ("other", 7)),
])
def test_work_item_references(ref, expected):
    assert _work_item_ref(ref, "lane") == expected


@pytest.mark.parametrize("ref", ["miragen12", "twelve", "#", "a#b", "12#"])
def test_bad_work_item_references(ref):
    with pytest.raises(AskHumanFailed, match="work_item"):
        _work_item_ref(ref, "lane")


async def test_ask_human_bare_number_reaches_the_default_projects_item():
    fake = FakeMiraDesign()
    await _ask(fake)("Q?", work_item="12")
    assert ("GET", "/v1/projects/lane/work-items/12") in fake.calls()


async def test_ask_human_without_a_work_item_opens_a_question_thread():
    fake = FakeMiraDesign()
    await _ask(fake, to=["ilari", "eemeli"])("Which region?\nDetails…", subject=None)
    (create,) = fake.sent("POST", "/threads")
    assert create.url.path == "/v1/projects/lane/threads"
    assert json.loads(create.content) == {"subject": "Which region?", "kind": "question",
                                          "participants": ["ilari", "eemeli"]}
    (post,) = fake.sent("POST", "/messages")
    assert post.url.path == "/v1/threads/th-new-0000/messages"
    assert json.loads(post.content)["mentions"] == ["ilari", "eemeli"]
    assert create.headers["idempotency-key"] == post.headers["idempotency-key"]


async def test_a_retried_question_opens_no_second_thread():
    fake = FakeMiraDesign()
    ask = _ask(fake)
    await ask("Which region?")
    await ask("Which region?")             # same tool: the in-process memo
    await _ask(fake)("Which region?")      # a fresh tool (restart): the subject lookup
    assert len(fake.sent("POST", "/threads")) == 1
    assert len(fake.sent("GET", "/threads")) == 2  # the memo answered the second call
    keys = {r.headers["idempotency-key"] for r in fake.sent("POST", "/messages")}
    assert len(keys) == 1  # MiraDesign replays the message instead of posting it again


async def test_the_key_names_agent_instance_and_question_not_the_run():
    fake = FakeMiraDesign()
    await _ask(fake, current=lambda: ("run-1", "inst-a"))("Q?")
    await _ask(fake, current=lambda: ("run-2", "inst-a"))("Q?")
    await _ask(fake, current=lambda: ("run-1", "inst-b"))("Q?")
    keys = [r.headers["idempotency-key"] for r in fake.sent("POST", "/messages")]
    assert keys[0] == keys[1] != keys[2]


async def test_a_question_already_filed_by_an_earlier_run_is_reported_not_failed():
    out = await _ask(FakeMiraDesign(message_status=409))("Q?")
    assert out.startswith("ok: this question was already filed")


async def test_ask_human_targets_another_project_and_falls_back_when_an_item_has_no_thread():
    fake = FakeMiraDesign(thread_id=None)
    await _ask(fake)("Q?", work_item="other#7")
    assert fake.calls() == [
        ("GET", "/v1/actors/ilari"), ("GET", "/v1/projects/other/work-items/7"),
        ("GET", "/v1/projects/other/threads"), ("POST", "/v1/projects/other/threads"),
        ("POST", "/v1/threads/th-new-0000/messages")]
    (create,) = fake.sent("POST", "/threads")
    assert json.loads(create.content)["target_type"] == "work_item"
    assert json.loads(create.content)["target_id"] == "wi-1"


async def test_an_unknown_recipient_files_nothing():
    fake = FakeMiraDesign(actors=("ilari",))
    with pytest.raises(AskHumanFailed, match="no actor 'nobody'"):
        await _ask(fake, to=["ilari", "nobody"])("Q?", work_item="12")
    assert not [r for r in fake.requests if r.method == "POST"]


async def test_ask_human_failures_are_tool_errors():
    with pytest.raises(AskHumanFailed, match="404"):
        await _ask(FakeMiraDesign(fail="work-items"))("Q?", work_item="12")
    with pytest.raises(AskHumanFailed, match="not set"):
        await _ask(FakeMiraDesign(), environ={})("Q?")
    with pytest.raises(AskHumanFailed, match="required"):
        await _ask(FakeMiraDesign())("  ")
    with pytest.raises(AskHumanFailed, match="work_item"):
        await _ask(FakeMiraDesign())("Q?", work_item="twelve")


async def test_ask_human_through_the_gateway_names_the_calling_instance_and_run():
    from miragen.harness.gateway import current_call

    fake = FakeMiraDesign()
    md = AskHumanMiraDesignSpec(url="https://design.example", project="lane", token_env="MD")
    tool = build_ask_human_tool(md, "eng", current_call, environ={"MD": "md-secret"},
                                transport=httpx.MockTransport(fake))
    gw = ToolGateway(engineer_profile(), runtime_tools=[tool])
    with gw.turn("inst-a", "run-1"):
        result = await gw.call_tool("ask_human", {"question": "Q?"}, instance="inst-a")
    assert not result.isError, result
    meta = json.loads(fake.sent("POST", "/messages")[0].content)["meta"]
    assert meta["instance"] == "inst-a" and meta["run_id"] == "run-1"


def test_ask_human_reaches_the_gateway_only_for_engineers(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRAGEN_CLAUDE_HOME", str(tmp_path / "claude-home"))
    ask = {"url": "https://design.example", "project": "lane", "token_env": "MD_TOKEN"}
    _, gw = build_model_harness(engineer_profile(ask_human=ask), runs_root=tmp_path)
    assert "ask_human" in gw._local
    _, gw = build_model_harness(snap.profile({}), runs_root=tmp_path / "locked")
    assert "ask_human" not in gw._local
