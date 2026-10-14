"""The Claude Code harness's engineer posture (``spec.harness.posture:
engineer``; mirarun ADR-041 §4–§6, docs/design/harnesses.md "Engineer
posture"): the pieces that are not launch options.

* **Workspaces.** One shared bare clone per lane repository under
  ``<claude_home>/repos/<name>.git`` (``git init --bare`` + an ``origin``
  remote, so the clone has only remote-tracking refs and the agents' own
  branches never collide with copied heads), and one worktree per instance
  under ``<workdirs>/<instance>/<name>``, detached at the remote's default
  branch. An existing worktree is never touched again: it holds the agent's
  work. Removing an instance removes its worktrees and prunes their metadata
  from the shared clones; that path needs no profile (it runs from
  ``purge_instance``, which other harnesses call with env-only settings).
* **Git and gh through the broker.** A generated global gitconfig
  (``GIT_CONFIG_GLOBAL``): ``url.https://<broker>/.insteadOf
  https://github.com/`` and a credential helper scoped to the broker that
  prints the credential from a secret file at call time. Nothing secret is
  written into any repository's config or onto a command line.
* **Subagent depth.** Claude Code's subagents run inside the same ``claude``
  process, so an environment variable cannot carry their depth. The policy
  is SDK hooks instead: ``PreToolUse`` on ``Agent``/``Task`` carries an
  ``agent_id`` only when the caller is itself a subagent, and
  ``SubagentStart`` records each subagent's depth. Unknown callers are
  treated as at the limit (fail closed).
* **ask_human.** A gateway runtime tool that files a MiraDesign question and
  returns at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from miragen.models import AgentProfile, AskHumanMiraDesignSpec, EngineerRepository, HarnessSpec

logger = logging.getLogger("miragen.harness.engineer")

REPOS_DIR = "repos"
GITCONFIG_FILE = "miragen-gitconfig"
CREDENTIAL_HELPER_FILE = "miragen-git-credential"
BROKER_CREDENTIAL_ENV = "MIRAGEN_BROKER_CREDENTIAL_FILE"
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
SUBAGENT_MATCHER = "Agent|Task"
GIT_TIMEOUT_S = 600.0


class WorkspaceError(RuntimeError):
    pass


def harness_spec(profile: AgentProfile) -> HarnessSpec | None:
    return profile.spec.harness if profile.spec is not None else None


def is_engineer(profile: AgentProfile) -> bool:
    spec = harness_spec(profile)
    return spec is not None and spec.posture == "engineer"


# ── git configuration ───────────────────────────────────────────────────────

_HELPER = """#!/bin/sh
# miragen: git credential helper for the GitHub broker. Prints the broker
# credential from the file named by $MIRAGEN_BROKER_CREDENTIAL_FILE; the
# credential is never stored by git (no store/cache helper is configured).
[ "$1" = get ] || exit 0
f="${MIRAGEN_BROKER_CREDENTIAL_FILE:-}"
[ -n "$f" ] && [ -r "$f" ] || exit 0
printf 'username=x-access-token\\npassword=%s\\n' "$(cat "$f")"
"""


def _gitconfig_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_git_files(claude_home: Path, profile: AgentProfile) -> Path:
    """Write the container-level gitconfig (and the broker credential
    helper) under ``claude_home``; returns the gitconfig path. Idempotent."""
    spec = harness_spec(profile)
    broker = spec.git_broker if spec is not None else None
    name = (broker.commit_name if broker else None) or profile.name
    email = (broker.commit_email if broker else None) or f"{profile.name}@agents.miragen.invalid"
    lines = [
        "# Written by miragen (engineer posture). No secrets live here.",
        "[user]", f"\tname = {_gitconfig_value(name)}", f"\temail = {_gitconfig_value(email)}",
        "[init]", "\tdefaultBranch = main",
        "[advice]", "\tdetachedHead = false",
        "[push]", "\tautoSetupRemote = true",
    ]
    if broker is not None:
        helper = claude_home / CREDENTIAL_HELPER_FILE
        helper.write_text(_HELPER)
        helper.chmod(0o700)
        base = f"https://{broker.host}"
        lines += [
            # Scoped to the broker: no other host is ever handed the credential.
            f'[credential "{base}"]', "\thelper = ", f"\thelper = {_gitconfig_value(str(helper))}",
            f'[url "{base}/"]',
            "\tinsteadOf = https://github.com/",
            "\tinsteadOf = git@github.com:",
            "\tinsteadOf = ssh://git@github.com/",
        ]
    path = claude_home / GITCONFIG_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


def git_env(claude_home: Path, profile: AgentProfile) -> dict[str, str]:
    """Environment git and gh need, for the claude child and for miragen's
    own clone/fetch. Values are paths and host names, plus GH_ENTERPRISE_TOKEN
    (gh reads its token only from the environment or its hosts file)."""
    env = {
        "GIT_CONFIG_GLOBAL": str(claude_home / GITCONFIG_FILE),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    spec = harness_spec(profile)
    broker = spec.git_broker if spec is not None else None
    if broker is not None:
        env[BROKER_CREDENTIAL_ENV] = broker.credential_file
        env["GH_HOST"] = broker.host
        env["GH_PROMPT_DISABLED"] = "1"
        env["GH_NO_UPDATE_NOTIFIER"] = "1"
        try:
            env["GH_ENTERPRISE_TOKEN"] = Path(broker.credential_file).read_text().strip()
        except OSError as exc:
            logger.warning("engineer posture: %s unreadable (%s); gh will be unauthenticated",
                           broker.credential_file, exc)
        if broker.ca_bundle:
            env["GIT_SSL_CAINFO"] = broker.ca_bundle
            env["SSL_CERT_FILE"] = broker.ca_bundle
    return env


# ── workspaces ──────────────────────────────────────────────────────────────

def _git(args: list[str], *, env: dict[str, str], cwd: Path | None = None,
         timeout: float = GIT_TIMEOUT_S) -> str:
    base = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
    try:
        done = subprocess.run(["git", *args], cwd=cwd, env={**base, **env}, text=True,
                              capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"git {args[0]} timed out after {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise WorkspaceError("git is not installed in this image") from exc
    if done.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:2])} failed: {done.stderr.strip()[-500:]}")
    return done.stdout.strip()


def mirror_path(claude_home: Path, repo: EngineerRepository) -> Path:
    return claude_home / REPOS_DIR / f"{repo.dir_name}.git"


def _ensure_mirror(path: Path, url: str, env: dict[str, str]) -> str:
    """Create or fetch the shared bare clone; returns the remote default
    branch as a remote-tracking ref (e.g. ``origin/main``)."""
    if not (path / "HEAD").is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            _git(["init", "--bare", "--quiet", str(tmp)], env=env)
            _git(["remote", "add", "origin", url], env=env, cwd=tmp)
            _git(["fetch", "--quiet", "origin"], env=env, cwd=tmp)
            os.rename(tmp, path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        _git(["fetch", "--prune", "--quiet", "origin"], env=env, cwd=path)
    _git(["remote", "set-head", "origin", "--auto"], env=env, cwd=path)
    return _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], env=env, cwd=path)


def _provision_sync(claude_home: Path, instance_dir: Path, repos: list[EngineerRepository],
                    env: dict[str, str]) -> Path:
    instance_dir.mkdir(parents=True, exist_ok=True)
    for repo in repos:
        tree = instance_dir / repo.dir_name
        if (tree / ".git").exists():
            continue  # the agent's work in progress: never reset
        mirror = mirror_path(claude_home, repo)
        default = _ensure_mirror(mirror, repo.url, env)
        _git(["worktree", "prune"], env=env, cwd=mirror)
        if tree.exists():
            shutil.rmtree(tree)  # a half-made worktree from a failed attempt
        try:
            _git(["worktree", "add", "--detach", "--quiet", str(tree), default],
                 env=env, cwd=mirror)
        except WorkspaceError:
            shutil.rmtree(tree, ignore_errors=True)
            with contextlib.suppress(WorkspaceError):
                _git(["worktree", "prune"], env=env, cwd=mirror)
            raise
    return instance_dir / repos[0].dir_name if len(repos) == 1 else instance_dir


def workspace_cwd(instance_dir: Path, profile: AgentProfile) -> Path:
    """The session's working directory for an instance (no git involved)."""
    spec = harness_spec(profile)
    repos = list(spec.repositories) if spec is not None else []
    return instance_dir / repos[0].dir_name if len(repos) == 1 else instance_dir


async def provision_workspace(claude_home: Path, instance_dir: Path, profile: AgentProfile,
                              locks: dict[str, asyncio.Lock]) -> Path:
    """Make sure every lane repository has a worktree for this instance;
    returns the session's working directory (the worktree when the lane has
    one repository, else the instance directory holding one per repo).

    ``locks`` (one per shared clone, owned by the harness): two instances
    spawning at once must not create or fetch the same clone concurrently."""
    spec = harness_spec(profile)
    repos = list(spec.repositories) if spec is not None else []
    if not repos:
        instance_dir.mkdir(parents=True, exist_ok=True)
        return instance_dir
    env = git_env(claude_home, profile)
    async with contextlib.AsyncExitStack() as stack:
        for repo in sorted(repos, key=lambda r: r.dir_name):  # fixed order: no lock cycles
            key = str(mirror_path(claude_home, repo))
            await stack.enter_async_context(locks.setdefault(key, asyncio.Lock()))
        return await asyncio.to_thread(_provision_sync, claude_home, instance_dir, repos, env)


def _worktree_mirrors(instance_dir: Path) -> set[Path]:
    """The shared clones the worktrees in ``instance_dir`` belong to, read off
    their ``.git`` files (``gitdir: <mirror>/worktrees/<id>``)."""
    mirrors: set[Path] = set()
    for candidate in [instance_dir, *(p for p in instance_dir.iterdir() if p.is_dir())]:
        dotgit = candidate / ".git"
        if not dotgit.is_file():
            continue
        match = re.match(r"gitdir:\s*(.+)", dotgit.read_text().strip())
        if match:
            gitdir = (candidate / match.group(1)).resolve()  # relative with useRelativePaths
            mirror = gitdir.parent.parent
            if gitdir.parent.name == "worktrees" and (mirror / "HEAD").is_file():
                mirrors.add(mirror)
    return mirrors


def remove_workdir(workdir: Path) -> bool:
    """Delete an instance's working directory, including any worktrees in
    it, and prune their records from the shared clones. Returns whether
    there was anything to delete."""
    if not workdir.is_dir():
        return False
    mirrors = _worktree_mirrors(workdir)
    shutil.rmtree(workdir)
    for mirror in mirrors:
        try:
            _git(["worktree", "prune"], env={"GIT_CONFIG_NOSYSTEM": "1"}, cwd=mirror, timeout=60)
        except WorkspaceError as exc:
            logger.warning("engineer posture: could not prune worktrees of %s: %s", mirror, exc)
    return True


# ── subagent depth ──────────────────────────────────────────────────────────

class SubagentDepthPolicy:
    """Refuses an Agent/Task call that would nest subagents deeper than
    ``limit``. One per ``claude`` process (one per instance).

    Depth of the caller: 0 on the main thread (no ``agent_id`` in the hook
    input); for a subagent, what ``SubagentStart`` recorded. The CLI does not
    say which Agent call started a subagent, so a starting subagent is given
    the deepest depth among the spawns still in flight: with parallel spawns
    from different depths this overestimates (refuses more), never less. A
    caller whose depth is unknown is treated as at the limit."""

    def __init__(self, limit: int):
        self.limit = limit
        self._depth: dict[str, int] = {}     # agent_id -> depth
        self._pending: dict[str, int] = {}   # Agent tool_use_id -> the child's depth

    def _caller_depth(self, data: dict) -> int | None:
        agent_id = data.get("agent_id")
        return 0 if not agent_id else self._depth.get(agent_id)

    async def pre_tool_use(self, data: dict, tool_use_id: str | None, _context: Any) -> dict:
        if data.get("tool_name") not in SUBAGENT_TOOLS:
            return {}
        caller = self._caller_depth(data)
        child = None if caller is None else caller + 1
        if child is None or child > self.limit:
            reason = (f"Subagent depth limit reached ({self.limit}): "
                      + ("this profile allows no subagents." if self.limit == 0 else
                         "a subagent may not start further subagents here.")
                      + " Do the work in this agent instead.")
            logger.info("claude-code harness: refused %s at depth %s (limit %d)",
                        data.get("tool_name"), caller, self.limit)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                           "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}
        key = tool_use_id or data.get("tool_use_id")
        if key:
            self._pending[key] = child
        return {}

    async def post_tool_use(self, data: dict, tool_use_id: str | None, _context: Any) -> dict:
        if data.get("tool_name") in SUBAGENT_TOOLS:
            self._pending.pop(tool_use_id or data.get("tool_use_id") or "", None)
        return {}

    async def subagent_start(self, data: dict, _tool_use_id: str | None, _context: Any) -> dict:
        agent_id = data.get("agent_id")
        if agent_id:
            self._depth[agent_id] = max(self._pending.values(), default=self.limit)
        return {}

    async def subagent_stop(self, data: dict, _tool_use_id: str | None, _context: Any) -> dict:
        self._depth.pop(data.get("agent_id") or "", None)
        return {}

    def hooks(self) -> dict[str, list]:
        from claude_agent_sdk import HookMatcher

        return {
            "PreToolUse": [HookMatcher(matcher=SUBAGENT_MATCHER, hooks=[self.pre_tool_use])],
            "PostToolUse": [HookMatcher(matcher=SUBAGENT_MATCHER, hooks=[self.post_tool_use])],
            "PostToolUseFailure": [HookMatcher(matcher=SUBAGENT_MATCHER,
                                               hooks=[self.post_tool_use])],
            "SubagentStart": [HookMatcher(hooks=[self.subagent_start])],
            "SubagentStop": [HookMatcher(hooks=[self.subagent_stop])],
        }


# ── ask_human ───────────────────────────────────────────────────────────────

class AskHumanFailed(RuntimeError):
    pass


def _token(spec: AskHumanMiraDesignSpec, environ: dict[str, str]) -> str:
    if spec.token_env:
        token = environ.get(spec.token_env, "").strip()
        if not token:
            raise AskHumanFailed(f"MiraDesign token env {spec.token_env} is not set")
        return token
    try:
        token = Path(spec.token_file or "").read_text().strip()
    except OSError as exc:
        raise AskHumanFailed(f"MiraDesign token file unreadable: {exc}") from exc
    if not token:
        raise AskHumanFailed("MiraDesign token file is empty")
    return token


def _work_item_ref(value: str, default_project: str) -> tuple[str, int]:
    match = re.fullmatch(r"\s*(?:([A-Za-z0-9_.-]+))?#?(\d+)\s*", value)
    if not match:
        raise AskHumanFailed("work_item is a number, '#12' or 'project#12'")
    return match.group(1) or default_project, int(match.group(2))


def build_ask_human_tool(
    spec: AskHumanMiraDesignSpec,
    agent: str,
    current: Callable[[], tuple[str | None, str | None]],
    *,
    environ: dict[str, str] | None = None,
    transport: Any = None,
    timeout_s: float = 20.0,
) -> Callable:
    """The ``ask_human`` gateway tool: files a MiraDesign question (REST, the
    same records the MiraDesign MCP ``post(kind=question)`` makes) and
    returns at once. ``current`` gives the gateway call's (run_id, instance)."""
    import httpx

    base = str(spec.url).rstrip("/") + "/v1"

    async def ask_human(question: str, work_item: str | None = None,
                        subject: str | None = None) -> str:
        """Ask a person (through MiraDesign) for a decision only they can make.

        Use it for decisions outside your authority or when you are blocked,
        not for things you can find out yourself. It returns at once: do NOT
        wait or poll for the answer. Carry on with work that does not depend on
        it, or stop the turn; the answer arrives later as a new turn.

        question: the question, self-contained (what you need, the options,
            your recommendation).
        work_item: the MiraDesign work item this is about ('12', '#12' or
            'project#12'), when there is one — the question goes on its thread.
        subject: a short title for a new thread (when there is no work item).
        """
        if not question or not question.strip():
            raise AskHumanFailed("`question` is required")
        run_id, instance = current()
        headers = {"Authorization": f"Bearer {_token(spec, environ if environ is not None else dict(os.environ))}"}
        meta = {"source": "miragen.ask_human", "agent": agent, "instance": instance,
                "run_id": run_id, "work_item": work_item}
        async with httpx.AsyncClient(base_url=base, headers=headers, timeout=timeout_s,
                                     transport=transport) as client:
            async def call(method: str, path: str, **kw) -> dict:
                try:
                    response = await client.request(method, path, **kw)
                except httpx.HTTPError as exc:
                    raise AskHumanFailed(f"MiraDesign unreachable: {exc}") from exc
                if response.status_code >= 400:
                    raise AskHumanFailed(
                        f"MiraDesign refused ({response.status_code}): {response.text[:300]}")
                return response.json()

            thread_id = None
            item = None
            if work_item:
                project, number = _work_item_ref(work_item, spec.project)
                item = await call("GET", f"/projects/{project}/work-items/{number}")
                thread_id = item.get("thread_id")
            else:
                project = spec.project
            if not thread_id:
                body = {"subject": (subject or question.strip().splitlines()[0])[:120],
                        "kind": "question", "participants": list(spec.to)}
                if item is not None and item.get("id"):
                    body |= {"target_type": "work_item", "target_id": item["id"]}
                thread_id = (await call("POST", f"/projects/{project}/threads", json=body))["id"]
            key = hashlib.sha256("\x1f".join(
                [agent, instance or "", run_id or "", str(thread_id), question]).encode()
            ).hexdigest()[:40]
            message = await call(
                "POST", f"/threads/{thread_id}/messages",
                json={"kind": "question", "body": question, "mentions": list(spec.to),
                      "meta": meta},
                headers={"Idempotency-Key": f"miragen-ask-{key}"})
        to = ", ".join("@" + h for h in spec.to)
        return (f"ok: question {str(message.get('id', ''))[:8]} filed in MiraDesign thread "
                f"{str(thread_id)[:8]} for {to}. Do not wait or poll for the answer: carry on "
                "with work that does not depend on it, or end the turn. The answer arrives "
                "as a later turn.")

    return ask_human
