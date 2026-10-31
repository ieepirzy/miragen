"""A spoke agent's model token (MiraRun ADR-045 §5): held in memory and one
0600 file on an in-memory volume, read by Claude Code through an
apiKeyHelper, refreshed in place, never persisted or logged, and the only
model credential: no fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

import miragen.app as app_module
from miragen import model_token
from miragen.app import app
from miragen.harness.claude_code import ClaudeCodeHarnessError
from miragen.model_token import ModelToken, ModelTokenHolder, ModelTokenInvalid
from miragen.runs import RunStore
from tests.test_claude_code_harness import Env, FakeClient, profile, turn

TOKEN = "eyJ.workload-token.sig-4d1c"
GATEWAY = "https://gateway.invalid"


def _credential(token: str = TOKEN, minutes: int = 15) -> dict[str, str]:
    return {"env": "MODEL_TOKEN", "token": token,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()}


def _helper(holder: ModelTokenHolder) -> subprocess.CompletedProcess:
    command = holder.helper_command()
    assert command is not None
    return subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30,
                          check=False)


@pytest.fixture
def holder(tmp_path, monkeypatch):
    h = ModelTokenHolder(tmp_path / "model")
    monkeypatch.setattr(model_token, "_holder", h)
    yield h
    h.clear()


# -- the holder ---------------------------------------------------------------

def test_the_token_file_is_0600_and_the_helper_prints_only_the_token(holder):
    holder.set(ModelToken.parse(_credential()))
    mode = stat.S_IMODE(os.stat(holder.path).st_mode)
    assert mode == 0o600
    result = _helper(holder)
    assert result.returncode == 0 and result.stdout == TOKEN


def test_without_a_current_token_the_helper_fails(holder):
    assert _helper(holder).returncode != 0  # none yet
    holder.set(ModelToken.parse(_credential(minutes=-1)))
    result = _helper(holder)
    assert result.returncode != 0 and result.stdout == ""


def test_a_refresh_replaces_the_token_and_an_older_one_is_ignored(holder):
    holder.set(ModelToken.parse(_credential("first", minutes=10)))
    holder.set(ModelToken.parse(_credential("second", minutes=15)))
    holder.set(ModelToken.parse(_credential("stale", minutes=5)))
    assert _helper(holder).stdout == "second"


def test_clear_removes_it_from_memory_and_the_volume(holder):
    holder.set(ModelToken.parse(_credential()))
    holder.clear()
    assert holder.current() is None and not holder.path.exists()


def test_the_token_never_prints(holder):
    parsed = ModelToken.parse(_credential())
    assert TOKEN not in repr(parsed)
    with pytest.raises(ModelTokenInvalid) as refused:
        ModelToken.parse({**_credential(), "expires_at": "soon"})
    assert TOKEN not in str(refused.value)


# -- the Claude Code harness on a spoke ------------------------------------------

@pytest.fixture
async def cc(tmp_path):
    e = Env(tmp_path)
    yield e
    for h in e.harnesses:
        await h.aclose()


@pytest.fixture
def gateway(monkeypatch, holder):
    monkeypatch.setenv("MIRAGEN_MODEL_GATEWAY_URL", GATEWAY)
    monkeypatch.setenv("MIRAGEN_MODEL_CONNECTION", "anthropic-main")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "metered-key")
    return holder


async def test_the_harness_calls_the_gateway_with_the_helper_and_nothing_else(cc, gateway):
    gateway.set(ModelToken.parse(_credential()))
    h = cc.harness()
    await h.run(turn("hi"))
    options = FakeClient.instances[0].options
    child = options.env
    assert child["ANTHROPIC_BASE_URL"] == f"{GATEWAY}/anthropic/anthropic-main"
    assert child["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] == "60000"
    settings = json.loads(options.settings)
    # The helper the child runs really yields the current token.
    helped = await asyncio.to_thread(subprocess.run, settings["apiKeyHelper"], shell=True,
                                     capture_output=True, text=True, timeout=30, check=False)
    assert helped.stdout == TOKEN
    # No fallback: no subscription token, no API key, not even in the allowlist.
    keep = child["MIRAGEN_ENV_KEEP"].split()
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        assert name not in child and name not in keep
    # The token itself is nowhere in the launch: not env, not settings.
    assert TOKEN not in json.dumps(child) and TOKEN not in options.settings


async def test_without_a_connection_there_are_no_model_calls(cc, gateway, monkeypatch):
    monkeypatch.delenv("MIRAGEN_MODEL_CONNECTION")
    h = cc.harness()
    with pytest.raises(ClaudeCodeHarnessError, match="no model connection"):
        h._env("chat")


async def test_a_saved_login_refuses_the_gateway_harness(cc, gateway):
    cc.home.mkdir(parents=True, exist_ok=True)
    (cc.home / ".credentials.json").write_text("{}")
    h = cc.harness()
    with pytest.raises(ClaudeCodeHarnessError, match="saved login"):
        h._env("chat")


@pytest.mark.parametrize("url", [
    "http://evil.com\\@luotsi.luotsi.svc",
    "http://u@evil.com@luotsi.luotsi.svc",
    "http://gateway.invalid",
    "ftp://luotsi.luotsi.svc",
])
async def test_the_pod_rechecks_the_gateway_url(cc, gateway, monkeypatch, url):
    monkeypatch.setenv("MIRAGEN_MODEL_GATEWAY_URL", url)
    h = cc.harness()
    with pytest.raises(ClaudeCodeHarnessError, match="MIRAGEN_MODEL_GATEWAY_URL"):
        h._env("chat")


async def test_a_cluster_local_http_gateway_is_used_as_given(cc, gateway, monkeypatch):
    url = "http://luotsi.luotsi.svc.cluster.local:8000"
    monkeypatch.setenv("MIRAGEN_MODEL_GATEWAY_URL", url + "/")
    gateway.set(ModelToken.parse(_credential()))
    h = cc.harness()
    await h.run(turn("hi"))
    assert FakeClient.instances[0].options.env["ANTHROPIC_BASE_URL"] == f"{url}/anthropic/anthropic-main"


async def test_no_token_directory_means_no_model_calls(cc, monkeypatch):
    monkeypatch.setenv("MIRAGEN_MODEL_GATEWAY_URL", GATEWAY)
    monkeypatch.setenv("MIRAGEN_MODEL_CONNECTION", "c")
    monkeypatch.setattr(model_token, "_holder", ModelTokenHolder(None))
    h = cc.harness()
    with pytest.raises(ClaudeCodeHarnessError, match="nowhere"):
        h._env("chat")


# -- the app ------------------------------------------------------------------

@pytest.fixture
async def client(tmp_path, monkeypatch, holder):
    monkeypatch.setattr(app_module, "HISTORIES_DIR", tmp_path / "histories")
    app_module._profile = profile()
    app_module._run_store = RunStore(root=tmp_path / "runs")

    async def fake_run_agent(prompt, *, record=None, repositories=None, mcp_secret_env=None,
                             use_history=False, instance=None):
        logging.getLogger("miragen.test").info("running %s", prompt)
        app_module._run_store.finish(record, status="succeeded", output="ok")
        return "ok"

    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)
    monkeypatch.setattr(app_module, "_model_ready", lambda: True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, tmp_path
    app_module._profile = None
    app_module._run_store = None
    app_module._busy_instances.clear()
    app_module._active_runs = 0


async def _finished(c, run_id):
    for _ in range(200):
        record = (await c.get(f"/runs/{run_id}")).json()
        if record["status"] != "running":
            return record
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


async def test_a_turns_model_token_is_held_and_never_persisted_or_logged(client, holder, caplog):
    c, tmp_path = client
    caplog.set_level(logging.DEBUG)
    r = await c.post("/instances/main/turns", json={
        "prompt": "p", "idempotency_key": "k-1", "credentials": {"model": _credential()}})
    assert r.status_code == 202, r.text
    record = await _finished(c, r.json()["turn_id"])
    assert holder.current().token == TOKEN
    assert TOKEN not in json.dumps(record)
    for path in (tmp_path / "runs").rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(errors="replace"), path
    assert TOKEN not in caplog.text


async def test_the_spoke_hands_over_a_refreshed_token(client, holder):
    c, _ = client
    holder.set(ModelToken.parse(_credential("old", minutes=3)))
    r = await c.put("/credentials/model", json=_credential("new", minutes=15))
    assert r.status_code == 204
    assert _helper(holder).stdout == "new"


async def test_a_malformed_token_is_refused_without_echoing_it(client, holder):
    c, _ = client
    r = await c.put("/credentials/model", json={**_credential(), "expires_at": "never"})
    assert r.status_code == 422 and TOKEN not in r.text
    assert holder.current() is None


# -- review round 1: nothing echoes a token, and the agent cannot redirect it --

@pytest.mark.parametrize("body", [
    # The reviewer's probe: an error about a sibling field must not carry the body.
    {"prompt": "", "idempotency_key": "k"},
    {"prompt": "p"},
    {"prompt": 123, "idempotency_key": "k"},
])
async def test_a_turn_422_never_echoes_the_token(client, holder, body):
    c, _ = client
    r = await c.post("/instances/main/turns", json={**body, "credentials": {
        "model": _credential(), "secret_env": {"MCP_TOKEN": TOKEN}}})
    assert r.status_code == 422
    assert TOKEN not in r.text and "input" not in r.text
    assert holder.current() is None


async def test_malformed_credentials_are_refused_without_echo(client, holder):
    c, _ = client
    r = await c.post("/instances/main/turns", json={
        "prompt": "p", "idempotency_key": "k",
        "credentials": {"model": {**_credential(), "expires_at": 5}, "nope": TOKEN}})
    assert r.status_code == 422 and TOKEN not in r.text


async def test_every_validation_error_drops_its_input(client):
    c, _ = client
    r = await c.post("/executor-runs", json={"prompt": "p", "context": {"secrets": TOKEN}})
    assert r.status_code == 422 and TOKEN not in r.text and "input" not in r.text


def test_the_token_directory_is_private_even_on_a_world_writable_volume(tmp_path):
    volume = tmp_path / "volume"
    volume.mkdir()
    volume.chmod(0o777)
    # Left behind wide open (an earlier process, a hand-made dir): tightened.
    (volume / "private").mkdir()
    (volume / "private").chmod(0o777)
    h = ModelTokenHolder(volume)
    h.set(ModelToken.parse(_credential()))
    assert stat.S_IMODE(os.stat(h.path.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(h.path).st_mode) == 0o600
    h.clear()


async def test_the_gateway_and_helper_ride_flag_settings(cc, gateway):
    gateway.set(ModelToken.parse(_credential()))
    h = cc.harness()
    await h.run(turn("hi"))
    settings = json.loads(FakeClient.instances[0].options.settings)
    assert settings["env"]["ANTHROPIC_BASE_URL"] == f"{GATEWAY}/anthropic/anthropic-main"
    assert settings["env"]["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] == "60000"
    assert TOKEN not in json.dumps(settings)
