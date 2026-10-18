"""deploy/engineer/engineer_setup.py, the engineer image's entrypoint: grants
become files, the broker CA becomes a bundle, plugins are configured with no
token on a command line, and a bad plugin bundle never replaces a good one."""

import base64
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parent.parent / "deploy" / "engineer" / "engineer_setup.py"
_spec = importlib.util.spec_from_file_location("engineer_setup", SCRIPT)
es = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es)

CERT = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"


def _bundle(files: dict[str, str], *, extra: list[tarfile.TarInfo] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return buf.getvalue()


def _marketplace(version="0.8.1") -> dict[str, str]:
    return {
        ".claude-plugin/marketplace.json": json.dumps({"name": "miradesign", "plugins": []}),
        "plugins/miradesign/.claude-plugin/plugin.json": json.dumps(
            {"name": "miradesign", "version": version}),
    }


class FakeRun:
    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, input=None, capture_output=True, text=True, env=None, timeout=None):
        self.calls.append({"argv": argv, "stdin": input, "env": env})
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr=self.stderr)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── credentials → files ──────────────────────────────────────────────────────


def test_grants_become_private_files_and_leave_the_environment(tmp_path):
    token_file = tmp_path / "bridge"
    token_file.write_text("bridge-token\n")
    env = {
        "MIRADESIGN_API_KEY": "design-token",
        "MIRAGEN_BRIDGE_API_KEY_FILE": str(token_file),
        "GH_BROKER_MIRARUN_API_KEY": "broker-cred",
        "OPENAI_API_KEY": "kept",          # not ours: untouched
        "CLAUDE_CODE_OAUTH_TOKEN": "kept",  # the harness reads it from env
    }
    written = es.materialize_secrets(env, tmp_path / "run")

    assert env == {"OPENAI_API_KEY": "kept", "CLAUDE_CODE_OAUTH_TOKEN": "kept"}
    assert written["MIRADESIGN_API_KEY"].read_text() == "design-token"
    assert written["MIRAGEN_BRIDGE_API_KEY"].read_text() == "bridge-token"
    assert written["GH_BROKER_MIRARUN_API_KEY"].name == "gh_broker_mirarun_api_key"
    assert written["broker_credential"].read_text() == "broker-cred"
    for path in written.values():
        assert path.stat().st_mode & 0o777 == 0o400
    assert (tmp_path / "run" / "secrets").stat().st_mode & 0o777 == 0o700


def test_two_broker_credentials_get_no_ambiguous_alias(tmp_path):
    env = {"GH_BROKER_A_API_KEY": "a", "GH_BROKER_B_API_KEY": "b"}
    written = es.materialize_secrets(env, tmp_path)
    assert "broker_credential" not in written
    assert set(written) == {"GH_BROKER_A_API_KEY", "GH_BROKER_B_API_KEY"}


def test_unrelated_api_keys_are_not_matched():
    assert es.secret_names({"GH_BROKERX_API_KEY": "x", "MY_GH_BROKER_API_KEY": "y",
                            "GH_BROKER_API_KEY": "z"}) == ["GH_BROKER_API_KEY"]


# ── CA bundle ────────────────────────────────────────────────────────────────


def test_the_broker_ca_is_appended_to_the_system_roots(tmp_path):
    roots = tmp_path / "roots.pem"
    roots.write_text("SYSTEM ROOTS\n")
    env = {"ENGINEER_EXTRA_CA_B64": base64.b64encode(CERT.encode()).decode()}
    bundle = es.write_ca_bundle(env, tmp_path, roots)
    assert bundle.read_text() == "SYSTEM ROOTS\n" + CERT


@pytest.mark.parametrize("value", ["", "not base64!", base64.b64encode(b"no cert").decode()])
def test_no_or_bad_ca_writes_nothing(tmp_path, value):
    roots = tmp_path / "roots.pem"
    roots.write_text("x")
    assert es.write_ca_bundle({"ENGINEER_EXTRA_CA_B64": value}, tmp_path, roots) is None
    assert not (tmp_path / "ca-bundle.pem").exists()


# ── the MiraDesign bundle ────────────────────────────────────────────────────


def test_the_bundle_is_fetched_over_https_with_the_bearer():
    seen = {}

    def opener(request, timeout):
        seen["url"], seen["auth"] = request.full_url, request.get_header("Authorization")
        return Response(b"data")

    assert es.fetch_bundle("https://design.example/", "tok", opener=opener) == b"data"
    assert seen == {"url": "https://design.example/v1/harness/cloud/plugin.tar.gz",
                    "auth": "Bearer tok"}


def test_a_token_is_never_sent_over_plain_http():
    with pytest.raises(ValueError):
        es.fetch_bundle("http://design.example", "tok", opener=lambda *a, **k: None)


def test_a_valid_bundle_replaces_the_cached_copy(tmp_path):
    dest = tmp_path / "miradesign"
    es.unpack_marketplace(_bundle(_marketplace("0.8.0")), dest, "miradesign")
    es.unpack_marketplace(_bundle(_marketplace("0.8.1")), dest, "miradesign")
    assert es.plugin_version(dest, "miradesign") == "0.8.1"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["miradesign"]


def _link_bundle() -> bytes:
    link = tarfile.TarInfo("plugins/miradesign/evil")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    return _bundle(_marketplace(), extra=[link])


@pytest.mark.parametrize("make_bad", [
    lambda: _bundle({"README": "no marketplace"}),
    lambda: _bundle({**_marketplace(), "../escape": "x"}),
    _link_bundle,
    lambda: b"not a tarball",
])
def test_a_bad_bundle_never_replaces_a_good_one(tmp_path, make_bad):
    dest = tmp_path / "miradesign"
    es.unpack_marketplace(_bundle(_marketplace("0.8.0")), dest, "miradesign")
    with pytest.raises((ValueError, tarfile.TarError, OSError)):
        es.unpack_marketplace(make_bad(), dest, "miradesign")
    assert es.plugin_version(dest, "miradesign") == "0.8.0"
    assert not (tmp_path / "escape").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["miradesign"]


# ── plugins ──────────────────────────────────────────────────────────────────


def _secrets(tmp_path, **values):
    out = {}
    for name, value in values.items():
        path = tmp_path / name.lower()
        path.write_text(value)
        out[name] = path
    return out


def test_plugins_are_configured_with_tokens_on_stdin_only(tmp_path, monkeypatch):
    monkeypatch.setattr(es, "MIRAGEN_MARKETPLACE", tmp_path / "mm")
    run = FakeRun()
    cli = es.ClaudeCLI("claude", tmp_path / "home", run=run)
    secrets = _secrets(tmp_path, MIRADESIGN_API_KEY="design-tok", MIRAGEN_BRIDGE_API_KEY="bridge-tok")
    env = {"MIRADESIGN_URL": "https://design.example", "MIRAGEN_MEMORY_URL": "https://memory.example/"}

    status = es.setup_plugins(env, secrets, cli,
                              opener=lambda r, timeout: Response(_bundle(_marketplace())))

    assert status["miradesign"] == {"installed": True, "source": "daemon",
                                    "version": "0.8.1", "token": True}
    assert status["miragen-memory"]["installed"] is True
    for call in run.calls:
        assert "design-tok" not in " ".join(call["argv"])
        assert "bridge-tok" not in " ".join(call["argv"])
        assert call["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "home")
    configures = [json.loads(c["stdin"]) for c in run.calls if c["argv"][2] == "configure"]
    assert {"daemon_url": "https://memory.example", "token": "bridge-tok"} in configures
    # MiraRun's supervisor is the only thing that starts turns.
    assert {"daemon_url": "https://design.example", "wake_idle_sessions": "false",
            "token": "design-tok"} in configures


def test_a_daemon_outage_falls_back_to_the_cached_plugin(tmp_path, monkeypatch):
    cli = es.ClaudeCLI("claude", tmp_path / "home", run=FakeRun())
    env = {"MIRADESIGN_URL": "https://design.example"}
    es.setup_plugins(env, {}, cli, opener=lambda r, timeout: Response(_bundle(_marketplace())))

    def down(request, timeout):
        raise OSError("refused")

    assert es.setup_plugins(env, {}, cli, opener=down)["miradesign"]["source"] == "cached"


def test_no_cache_and_no_daemon_is_reported_not_fatal(tmp_path):
    cli = es.ClaudeCLI("claude", tmp_path / "home", run=FakeRun())

    def down(request, timeout):
        raise OSError("refused")

    status = es.setup_plugins({"MIRADESIGN_URL": "https://d.example"}, {}, cli, opener=down)
    assert status["miradesign"]["installed"] is False
    assert status["miragen-memory"] == {"installed": False, "error": "MIRAGEN_MEMORY_URL unset"}


def test_cli_errors_are_scrubbed_of_secrets(tmp_path):
    cli = es.ClaudeCLI("claude", tmp_path / "home", secrets=("s3cret",),
                       run=FakeRun(returncode=1, stderr="bad token s3cret"))
    with pytest.raises(RuntimeError) as exc:
        cli("configure", "x", "--values-stdin", stdin="{}")
    assert "s3cret" not in str(exc.value) and "<redacted>" in str(exc.value)
