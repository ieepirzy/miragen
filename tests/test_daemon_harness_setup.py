"""miragend runs the harness setup itself (miragen/daemon/harness_setup.py):
when it is on, what it writes at startup, and what /health says."""

from __future__ import annotations

import json
import time
import tomllib

import pytest
import yaml
from pydantic import ValidationError
from starlette.testclient import TestClient

from miragen.daemon.api import _build_harness_setup, create_app
from miragen.daemon.harness_setup import HarnessSetupService, resolve_harness_setup
from miragen.daemon.sessions.config import HarnessSetup, SessionsConfig
from tests.test_sessions_plane import Harness


def _no_container():
    return False


class TestGate:
    def test_off_without_a_url_never_a_guessed_loopback(self):
        resolved = resolve_harness_setup(HarnessSetup(), {}, in_container=_no_container)
        assert resolved.enabled is False and "url" in resolved.reason

    def test_on_with_a_configured_url(self):
        resolved = resolve_harness_setup(
            HarnessSetup(url="https://m.example", token_file="~/t"), {"HOME": "/h"}, in_container=_no_container)
        assert resolved.enabled and resolved.url == "https://m.example"
        assert resolved.token_file.endswith("/t") and "~" not in resolved.token_file

    def test_environment_overrides(self):
        env = {"MIRAGEND_HARNESS_SETUP_URL": "https://env.example", "MIRAGEND_HARNESS_SETUP_TOKEN_PATH": "/s/t",
               "MIRAGEND_HARNESS_SETUP_INTERVAL_S": "120"}
        resolved = resolve_harness_setup(HarnessSetup(url="https://file.example"), env, in_container=_no_container)
        assert (resolved.url, resolved.token_file, resolved.interval_s) == ("https://env.example", "/s/t", 120)
        off = resolve_harness_setup(HarnessSetup(url="https://m.example"), {"MIRAGEND_HARNESS_SETUP": "off"},
                                    in_container=_no_container)
        assert off.enabled is False

    def test_the_token_path_survives_the_secret_file_loader(self, tmp_path):
        """main() runs load_file_secrets first, which turns every *_FILE
        variable into its plain name and deletes it — the override must not
        be one of those."""
        from miragen.daemon.sessions.config import load_file_secrets
        token = tmp_path / "bridge.token"
        token.write_text("t0k")
        env = {"MIRAGEND_HARNESS_SETUP_URL": "https://m.example",
               "MIRAGEND_HARNESS_SETUP_TOKEN_PATH": str(token)}
        load_file_secrets(env)
        resolved = resolve_harness_setup(HarnessSetup(), env, in_container=_no_container)
        assert resolved.token_file == str(token)

    def test_a_container_is_the_hosted_daemon_unless_told_otherwise(self):
        hosted = resolve_harness_setup(HarnessSetup(url="https://m.example"), {}, in_container=lambda: True)
        assert hosted.enabled is False and "container" in hosted.reason
        forced = resolve_harness_setup(HarnessSetup(url="https://m.example", enabled=True), {},
                                       in_container=lambda: True)
        assert forced.enabled is True

    def test_explicitly_disabled(self):
        assert resolve_harness_setup(HarnessSetup(url="https://m.example", enabled=False), {},
                                     in_container=_no_container).enabled is False

    def test_sessions_yaml_block(self, tmp_path):
        config = SessionsConfig.model_validate(yaml.safe_load(
            "principal: assistant\nharness_setup:\n  url: https://memory.example\n"
            "  token_file: /home/u/.config/miragend/bridge.token\n  interval_s: 300\n"))
        assert config.harness_setup.url == "https://memory.example" and config.harness_setup.interval_s == 300
        with pytest.raises(ValidationError):
            HarnessSetup(url="https://m.example/$x")
        with pytest.raises(ValidationError):
            HarnessSetup(url="https://m.example", surprise=1)


def _service(tmp_path, **env):
    environ = {"HOME": str(tmp_path), "GROK_HOME": str(tmp_path / ".grok"),
               "CODEX_HOME": str(tmp_path / ".codex"), "PATH": "/nonexistent", **env}
    resolved = resolve_harness_setup(HarnessSetup(url="https://m.example"), environ, in_container=_no_container)
    return HarnessSetupService(resolved, environ)


class TestService:
    def test_writes_into_existing_homes_only(self, tmp_path):
        (tmp_path / ".codex").mkdir()
        service = _service(tmp_path)
        status = service.run_once()
        assert status["codex"]["installed"] and status["codex"]["current"]
        assert status["codex"]["last_changed"]  # logged + reported
        assert status["grok-build"]["installed"] is False and not (tmp_path / ".grok").exists()
        config = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
        assert "https://m.example" in config["mcp_servers"]["miragen-bridge"]["args"]
        again = service.run_once()
        assert again["codex"]["current"] and service.runs == 2

    @pytest.mark.parametrize("broken", ["codex", "grok-build"])
    def test_one_harness_failing_never_stops_the_other(self, tmp_path, broken):
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".grok" / "hooks").mkdir(parents=True)
        bad = {"codex": tmp_path / ".codex" / "hooks.json",
               "grok-build": tmp_path / ".grok" / "hooks" / "miragen.json"}
        bad[broken].write_text("{broken")
        status = _service(tmp_path).run_once()
        other = "grok-build" if broken == "codex" else "codex"
        assert status[broken]["current"] is False and "not valid JSON" in status[broken]["last_error"]
        assert status[other]["current"] is True and status[other]["last_error"] is None

    def test_disabled_does_nothing(self, tmp_path):
        (tmp_path / ".codex").mkdir()
        resolved = resolve_harness_setup(HarnessSetup(), {}, in_container=_no_container)
        service = HarnessSetupService(resolved, {"CODEX_HOME": str(tmp_path / ".codex")})
        service.run_once()
        assert service.runs == 0 and list((tmp_path / ".codex").iterdir()) == []

    def test_runs_at_startup_and_reports_on_health(self, tmp_path):
        (tmp_path / ".grok").mkdir()
        harness = Harness(tmp_path)
        service = _service(tmp_path)
        app = create_app(None, sessions=harness.plane, harness_setup=service)
        with TestClient(app) as client:
            for _ in range(100):  # the first run happens in a worker thread
                if service.runs:
                    break
                time.sleep(0.02)
            body = client.get("/health").json()
        setup = body["harness_setup"]
        assert setup["enabled"] and setup["url"] == "https://m.example" and setup["runs"] >= 1
        assert setup["harnesses"]["grok-build"]["current"] is True
        assert setup["harnesses"]["codex"]["installed"] is False
        hooks = json.loads((tmp_path / ".grok" / "hooks" / "miragen.json").read_text())["hooks"]
        assert "https://m.example" in hooks["SessionStart"][0]["hooks"][0]["command"]

    def test_the_daemon_builds_it_from_the_session_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MIRAGEND_HARNESS_SETUP_URL", "https://env.example")
        harness = Harness(tmp_path)
        service = _build_harness_setup(harness.plane)
        assert service.resolved.url == "https://env.example"
        assert _build_harness_setup(None) is None



def test_a_bad_interval_falls_back_instead_of_crashing():
    resolved = resolve_harness_setup(HarnessSetup(url="https://m.example", interval_s=300),
                                     {"MIRAGEND_HARNESS_SETUP_INTERVAL_S": "ten"}, in_container=_no_container)
    assert resolved.enabled and resolved.interval_s == 300


def test_disabling_removes_what_this_daemon_wrote(tmp_path):
    from miragen_hook import harness_setup as hs
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".grok").mkdir()
    _service(tmp_path).run_once()
    # a Grok home the PLUGIN manages (fallback path) is not the daemon's to remove
    hs.ensure_grok(tmp_path / ".grok", url="https://m.example", managed_by=hs.MANAGED_BY_PLUGIN)
    environ = {"HOME": str(tmp_path), "GROK_HOME": str(tmp_path / ".grok"),
               "CODEX_HOME": str(tmp_path / ".codex"), "MIRAGEND_HARNESS_SETUP": "off"}
    resolved = resolve_harness_setup(HarnessSetup(url="https://m.example"), environ, in_container=_no_container)
    assert resolved.enabled is False and resolved.remove is True
    HarnessSetupService(resolved, environ).remove_once()
    assert not (tmp_path / ".codex" / "miragen-adapter").exists()
    assert "miragen-bridge" not in (tmp_path / ".codex" / "config.toml").read_text()
    assert json.loads((tmp_path / ".codex" / "hooks.json").read_text()) == {}
    assert (tmp_path / ".grok" / "hooks" / "miragen.json").exists()  # the plugin's: kept
    # no URL is not a disable: nothing is removed
    assert resolve_harness_setup(HarnessSetup(), {}, in_container=_no_container).remove is False


def test_disabled_at_startup_runs_the_removal(tmp_path):
    (tmp_path / ".codex").mkdir()
    _service(tmp_path).run_once()
    environ = {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / ".codex"),
               "GROK_HOME": str(tmp_path / ".grok")}
    resolved = resolve_harness_setup(HarnessSetup(url="https://m.example", enabled=False), environ,
                                     in_container=_no_container)
    harness = Harness(tmp_path / "plane")
    app = create_app(None, sessions=harness.plane, harness_setup=HarnessSetupService(resolved, environ))
    with TestClient(app):
        for _ in range(100):
            if not (tmp_path / ".codex" / "miragen-adapter").exists():
                break
            time.sleep(0.02)
    assert not (tmp_path / ".codex" / "miragen-adapter").exists()



def test_the_service_snapshots_the_adapter_at_start(tmp_path):
    from miragen_hook import harness_setup as hs
    service = _service(tmp_path)
    assert service._source is not None and service._source != hs.adapter_source()
    assert hs.adapter_digest(service._source) == hs.adapter_digest()
