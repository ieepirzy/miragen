"""The headless Claude Code memory runner (docs/design/memory-effectiveness.md
P1.0), against a fake `claude` binary: isolation flags, the credential scrub,
the recursion guard and — above all — that no failure ever reads as an empty
result."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import stat
import sys
import textwrap
import time

import pytest
from pydantic import BaseModel

from miragen.cli import next_backoff
from miragen.memory import claude_code
from miragen.memory.claude_code import (
    ClaudeCodeError,
    ClaudeCodeRunner,
    child_env,
    is_claude_code_model,
    parse_result,
)
from miragen.memory.extraction import (
    ExtractionResult,
    SupportCheck,
    build_model_checker,
    build_model_extractor,
)
from miragen.memory.selection import SelectionResult, build_model_selector
from miragen_hook import client as hook_client

FAKE = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys, time
    record = {{
        "argv": sys.argv[1:], "cwd": os.getcwd(), "cwd_entries": os.listdir("."),
        "stdin": sys.stdin.read(), "env": dict(os.environ), "start": time.time(),
    }}
    mode = os.environ.get("FAKE_MODE", "ok")
    if os.environ.get("FAKE_PIDFILE"):
        with open(os.environ["FAKE_PIDFILE"], "w") as fh:
            fh.write(str(os.getpid()))
    if mode == "sleep":
        time.sleep(float(os.environ.get("FAKE_SLEEP", "5")))
    record["end"] = time.time()
    log = os.environ["FAKE_LOG"]
    with open(log, "a") as fh:
        fh.write(json.dumps(record) + "\\n")
    if mode == "exit":
        print("rate limited", file=sys.stderr)
        sys.exit(1)
    if mode == "limit":
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                          "result": "You've hit your session limit · resets 9:30pm",
                          "stats": {{"killed": {{"parent": 0}}}}}}))
        sys.exit(1)
    out = {{"type": "result", "subtype": "success", "is_error": False,
           "structured_output": json.loads(os.environ.get("FAKE_OUTPUT", "{{}}"))}}
    if mode == "is_error":
        out.update(is_error=True, subtype="error_max_turns", result="usage limit")
    if mode == "missing":
        out.pop("structured_output")
    if mode == "garbage":
        print("not json")
        sys.exit(0)
    print(json.dumps(out))
    """
)


class Echo(BaseModel):
    ok: bool


@pytest.fixture
def fake(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text(FAKE.format(python=sys.executable))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("MIRAGEN_CLAUDE_BIN", str(binary))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_OUTPUT", json.dumps({"ok": True}))
    monkeypatch.setattr(claude_code, "_semaphore", None)

    def calls() -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return calls


def run(coro):
    return asyncio.run(coro)


def test_a_call_is_isolated_and_reads_its_input_from_stdin(fake):
    result = run(ClaudeCodeRunner("claude-code:haiku").run("INSTR", "the prompt", Echo))
    assert result == Echo(ok=True)
    (call,) = fake()
    argv = call["argv"]
    assert argv[:3] == ["-p", "--model", "haiku"]
    for flag, value in (("--tools", ""), ("--setting-sources", ""),
                        ("--system-prompt", "INSTR"), ("--output-format", "json")):
        assert argv[argv.index(flag) + 1] == value
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert "--bare" not in argv, "--bare never reads OAuth: it cannot use a subscription"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == Echo.model_json_schema()
    assert call["stdin"] == "the prompt"
    assert call["cwd_entries"] == [], "empty working dir: no CLAUDE.md, no auto-memory"
    assert not os.path.exists(call["cwd"]), "the working dir is cleaned up"


def test_api_credentials_are_scrubbed_and_the_worker_is_marked(fake, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-metered")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription")
    run(ClaudeCodeRunner("claude-code:haiku").run("i", "p", Echo))
    env = fake()[0]["env"]
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription"
    assert env["MIRAGEN_WORKER"] == "1"


def test_child_env_never_mutates_the_parent():
    parent = {"ANTHROPIC_API_KEY": "k", "PATH": "/bin"}
    child = child_env(parent)
    assert parent == {"ANTHROPIC_API_KEY": "k", "PATH": "/bin"}
    assert child == {"PATH": "/bin", "MIRAGEN_WORKER": "1"}


@pytest.mark.parametrize("mode", ["exit", "is_error", "missing", "garbage"])
def test_every_failure_raises_and_never_reads_as_empty(fake, monkeypatch, mode):
    # A schema-valid EMPTY selection: only the failure mode can make it raise,
    # so an error read as "nothing selected" would pass silently here.
    monkeypatch.setenv("FAKE_OUTPUT", json.dumps({"selections": []}))
    monkeypatch.setenv("FAKE_MODE", mode)
    with pytest.raises(ClaudeCodeError):
        run(ClaudeCodeRunner("claude-code:haiku").run("i", "p", SelectionResult))


def test_output_that_breaks_the_schema_raises(fake, monkeypatch):
    monkeypatch.setenv("FAKE_OUTPUT", json.dumps({"selections": [{"record_id": "x"}]}))
    with pytest.raises(ClaudeCodeError, match="schema"):
        run(ClaudeCodeRunner("claude-code:haiku").run("i", "p", SelectionResult))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def pidfile(tmp_path, monkeypatch):
    path = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_PIDFILE", str(path))
    monkeypatch.setenv("FAKE_MODE", "sleep")
    monkeypatch.setenv("FAKE_SLEEP", "30")
    return path


def _wait_for_file(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not path.exists() or not path.read_text():
        assert time.monotonic() < deadline, "the fake never started"
        time.sleep(0.02)
    return int(path.read_text())


def test_a_hung_call_is_killed_at_the_timeout(fake, pidfile):
    started = time.monotonic()
    with pytest.raises(ClaudeCodeError, match="timed out"):
        run(ClaudeCodeRunner("claude-code:haiku", timeout=1.0).run("i", "p", Echo))
    assert time.monotonic() - started < 5
    assert not _alive(_wait_for_file(pidfile)), "the child outlived its timeout"


def test_a_cancelled_call_kills_its_child(fake, pidfile):
    """The daemon wraps recall in its own wait_for: when that fires, the
    runner is CANCELLED, not timed out. The child must die with it."""
    runner = ClaudeCodeRunner("claude-code:haiku", timeout=60)

    async def cancelled():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(runner.run("i", "p", Echo), timeout=1.0)

    run(cancelled())
    assert not _alive(_wait_for_file(pidfile)), "a cancelled call leaked its claude process"


def test_a_missing_binary_raises(monkeypatch):
    monkeypatch.setenv("MIRAGEN_CLAUDE_BIN", "/nonexistent/claude")
    with pytest.raises(ClaudeCodeError, match="cannot start"):
        run(ClaudeCodeRunner("claude-code:haiku").run("i", "p", Echo))


def test_concurrency_is_capped(fake, monkeypatch):
    monkeypatch.setenv("MIRAGEN_CLAUDE_CODE_CONCURRENCY", "1")
    monkeypatch.setenv("FAKE_MODE", "sleep")
    monkeypatch.setenv("FAKE_SLEEP", "0.3")
    runner = ClaudeCodeRunner("claude-code:haiku")

    async def three():
        await asyncio.gather(*(runner.run("i", "p", Echo) for _ in range(3)))

    run(three())
    spans = sorted((c["start"], c["end"]) for c in fake())
    assert len(spans) == 3
    assert all(later[0] >= earlier[1] for earlier, later in itertools.pairwise(spans)), spans


def test_parse_result_accepts_a_plain_success():
    out = json.dumps({"subtype": "success", "is_error": False,
                      "structured_output": {"ok": False}})
    assert parse_result(out, 0, "", Echo) == Echo(ok=False)


def test_model_strings_route_to_the_runner(fake, monkeypatch):
    assert is_claude_code_model("claude-code:haiku")
    assert not is_claude_code_model("anthropic:claude-haiku-4-5") and not is_claude_code_model(None)
    with pytest.raises(ValueError):
        ClaudeCodeRunner("claude-code:")

    monkeypatch.setenv("FAKE_OUTPUT", json.dumps({"proposals": []}))
    assert run(build_model_extractor("claude-code:haiku")("text", "session_episode")) == (
        ExtractionResult()
    )
    assert "[source kind: session_episode]\ntext" == fake()[-1]["stdin"]

    monkeypatch.setenv("FAKE_OUTPUT", json.dumps({"supported": True, "reason": "ok"}))
    assert run(build_model_checker("claude-code:haiku")("s", "q")).supported is True

    monkeypatch.setenv("FAKE_OUTPUT", json.dumps({"selections": []}))
    cards = [{"record_id": "r1", "type": "observation", "payload": {"text": "t"}}]
    assert run(build_model_selector("claude-code:haiku")("req", cards)) == SelectionResult()
    assert "record_id=r1" in fake()[-1]["stdin"]
    assert isinstance(SupportCheck(supported=False), SupportCheck)


def test_the_hook_adapter_never_captures_a_memory_worker_call():
    """Recursion guard: a `claude -p` miragen started must not become a
    session → episode → extraction → another call."""
    posted = []

    def opener(*args, **kwargs):  # pragma: no cover - must not be reached
        posted.append(args)
        raise AssertionError("the worker's hook must not reach the daemon")

    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "hi"}
    out = hook_client.run(
        "claude-code", payload, daemon_url="http://127.0.0.1:9", token=None,
        environ={"MIRAGEN_WORKER": "1"}, opener=opener, pid=1,
    )
    assert out is None and posted == []
    assert hook_client.is_memory_worker({"MIRAGEN_WORKER": "1"})
    assert not hook_client.is_memory_worker({})


def test_worker_backs_off_only_while_every_job_fails():
    failed = [{"status": "failed"}]
    assert next_backoff(0, [], interval=30, ceiling=900) == 0
    assert next_backoff(0, failed, interval=30, ceiling=900) == 30
    assert next_backoff(30, failed, interval=30, ceiling=900) == 60
    assert next_backoff(600, failed, interval=30, ceiling=900) == 900
    assert next_backoff(900, failed + [{"status": "done"}], interval=30, ceiling=900) == 0


def test_a_usage_limit_says_so(fake, monkeypatch):
    from miragen.memory.claude_code import ClaudeCodeLimited

    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(ClaudeCodeLimited, match="session limit"):
        run(ClaudeCodeRunner("claude-code:haiku").run("i", "p", Echo))


def test_the_worker_provisions_its_principal_once_and_keeps_the_token(tmp_path):
    import stat as _stat

    from miragen.cli import ensure_worker_token
    from miragen.memory.client import MemoryClient
    from miragen.memory.ephemeral import EphemeralMemoryService
    from miragen.models import MemoryScopesSpec, MemorySpec

    service = EphemeralMemoryService()
    spec = MemorySpec(scopes=MemoryScopesSpec(read=[], propose=["x"], default_write="x"))
    factory = lambda token: MemoryClient(  # noqa: E731
        spec, transport=service.transport(), base_url="http://loimi.test", token=token)
    token_file = tmp_path / "state" / "worker.token"
    env = {"LOIMI_OPERATOR_TOKEN": service.operator_token}

    assert run(ensure_worker_token(spec, "mira-worker", token_file, environ=env,
                                   client_factory=factory)) == "created"
    assert "mira-worker" in service.principals
    assert _stat.S_IMODE(token_file.stat().st_mode) == 0o600
    token = token_file.read_text().strip()
    assert env[spec.credential_env] == token

    again = {"LOIMI_OPERATOR_TOKEN": service.operator_token}
    assert run(ensure_worker_token(spec, "mira-worker", token_file, environ=again,
                                   client_factory=factory)) == "from file"
    assert again[spec.credential_env] == token

    token_file.unlink()
    assert run(ensure_worker_token(spec, "mira-worker", token_file, environ={
        "LOIMI_OPERATOR_TOKEN": service.operator_token}, client_factory=factory)) == "minted"
    with pytest.raises(RuntimeError, match="LOIMI_OPERATOR_TOKEN"):
        run(ensure_worker_token(spec, "w2", tmp_path / "none", environ={}, client_factory=factory))
