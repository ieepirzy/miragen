#!/usr/bin/env python3
"""Engineer image entrypoint: prepare one lane container, then exec miragen.

Runs as the unprivileged agent user every time the container starts, before
`miragen run` (Dockerfile.engineer). Stdlib only. Steps:

1. **Credentials become files.** The plugin tokens and the GitHub broker
   credential reach the container as per-profile grants (miragend forwards
   only what the profile lists under `credentials:`). Each one found, as
   NAME or NAME_FILE, is written to ``/run/engineer/secrets/<name>`` (0400)
   and removed from the environment `miragen run` inherits. Profiles point
   at the files (git_broker.credential_file, ask_human.token_file).
2. **Broker CA.** ``ENGINEER_EXTRA_CA_B64`` (base64 PEM, not a secret) is
   appended to the system roots in ``/run/engineer/ca-bundle.pem`` for
   ``git_broker.ca_bundle``.
3. **Plugins.** Installed into Claude Code's user scope under
   ``CLAUDE_CONFIG_DIR`` (= MIRAGEN_CLAUDE_HOME), which the engineer posture
   loads (setting_sources=["user"]):
   - miragen-memory from the directory marketplace baked into the image;
   - miradesign from the MiraDesign daemon's own bundle
     (``GET $MIRADESIGN_URL/v1/harness/cloud/plugin.tar.gz``, bearer), so the
     plugin never speaks a newer hook protocol than the daemon and the image
     carries no unpublished code. The last good bundle is kept on the
     agent's volume and used when the daemon cannot be reached.
   Options (URL, token) go through ``claude plugin configure --values-stdin``,
   so no token is ever on a command line. MiraDesign's idle wake is turned
   off: MiraRun's lane supervisor is the only thing that starts turns.
4. A status file, ``/run/engineer/status.json``, says what was set up (names
   and versions, never values), and the same summary goes to the log.

Failures are logged and the container still starts (an engineer without a
plugin is visible to the supervisor as no progress; one that never boots is
a crash loop with less to go on). ``ENGINEER_SETUP_STRICT=1`` makes a plugin
failure fatal instead.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

RUN_DIR = Path(os.environ.get("ENGINEER_RUN_DIR", "/run/engineer"))
SYSTEM_ROOTS = Path("/etc/ssl/certs/ca-certificates.crt")
MIRAGEN_MARKETPLACE = Path(os.environ.get(
    "ENGINEER_MIRAGEN_MARKETPLACE", "/opt/engineer/marketplaces/miragen"))
BUNDLE_PATH = "/v1/harness/cloud/plugin.tar.gz"
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
CLI_TIMEOUT_S = 90

# Grants turned into files. The broker credential is per lane, so any
# GH_BROKER[_<LANE>]_API_KEY counts.
FIXED_SECRETS = ("MIRADESIGN_API_KEY", "MIRAGEN_BRIDGE_API_KEY")
BROKER_SECRET = re.compile(r"^GH_BROKER(?:_[A-Z0-9]+)*_API_KEY$")


def log(message: str) -> None:
    print(f"engineer-setup: {message}", file=sys.stderr, flush=True)


# ── 1. credentials → files ──────────────────────────────────────────────────

def secret_names(environ: Mapping[str, str]) -> list[str]:
    found = set()
    for key in environ:
        name = key[: -len("_FILE")] if key.endswith("_FILE") else key
        if name in FIXED_SECRETS or BROKER_SECRET.fullmatch(name):
            found.add(name)
    return sorted(found)


def _read_value(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value:
        return value.strip()
    path = environ.get(f"{name}_FILE")
    if path:
        try:
            return Path(path).read_text().strip() or None
        except OSError as exc:
            log(f"{name}_FILE unreadable ({type(exc).__name__}); skipped")
    return None


def materialize_secrets(environ: MutableMapping[str, str], run_dir: Path) -> dict[str, Path]:
    """Write each granted credential to run_dir/secrets/<lowercase name>
    (0400) and drop NAME / NAME_FILE from ``environ``. Returns name → path.
    With exactly one broker credential, ``broker_credential`` names it too."""
    secrets_dir = run_dir / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secrets_dir.chmod(0o700)
    written: dict[str, Path] = {}
    for name in secret_names(environ):
        value = _read_value(environ, name)
        environ.pop(name, None)
        environ.pop(f"{name}_FILE", None)
        if not value:
            continue
        target = secrets_dir / name.lower()
        _write_private(target, value)
        written[name] = target
    brokers = [n for n in written if BROKER_SECRET.fullmatch(n)]
    if len(brokers) == 1:
        alias = secrets_dir / "broker_credential"
        _write_private(alias, written[brokers[0]].read_text())
        written["broker_credential"] = alias
    return written


def _write_private(target: Path, value: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
        os.chmod(tmp, 0o400)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── 2. broker CA ────────────────────────────────────────────────────────────

def write_ca_bundle(environ: Mapping[str, str], run_dir: Path,
                    system_roots: Path = SYSTEM_ROOTS) -> Path | None:
    raw = (environ.get("ENGINEER_EXTRA_CA_B64") or "").strip()
    if not raw:
        return None
    try:
        pem = base64.b64decode(raw, validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        log("ENGINEER_EXTRA_CA_B64 is not base64 PEM; no CA bundle written")
        return None
    if "-----BEGIN CERTIFICATE-----" not in pem:
        log("ENGINEER_EXTRA_CA_B64 holds no certificate; no CA bundle written")
        return None
    target = run_dir / "ca-bundle.pem"
    target.write_text(system_roots.read_text().rstrip("\n") + "\n" + pem.strip() + "\n")
    target.chmod(0o444)
    return target


# ── 3. plugins ──────────────────────────────────────────────────────────────

def _https_or_loopback(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")


def fetch_bundle(url: str, token: str | None, *,
                 opener: Callable = urllib.request.urlopen) -> bytes:
    if not _https_or_loopback(url):
        raise ValueError("MIRADESIGN_URL must be https (a token is sent)")
    request = urllib.request.Request(url.rstrip("/") + BUNDLE_PATH)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with opener(request, timeout=30) as response:
        data = response.read(MAX_BUNDLE_BYTES + 1)
    if len(data) > MAX_BUNDLE_BYTES:
        raise ValueError("plugin bundle too large")
    return data


def unpack_marketplace(data: bytes, dest: Path, plugin: str) -> Path:
    """Unpack a directory-marketplace tarball into ``dest`` (replacing it
    only once the new copy is complete and valid). Refuses links, absolute
    paths and traversal (tarfile's data filter)."""
    staging = dest.parent / f".{dest.name}.new"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(staging, filter="data")
        manifest = staging / ".claude-plugin" / "marketplace.json"
        plugin_json = staging / "plugins" / plugin / ".claude-plugin" / "plugin.json"
        if not manifest.is_file() or not plugin_json.is_file():
            raise ValueError(f"bundle is not a marketplace holding '{plugin}'")
        json.loads(manifest.read_text())
        json.loads(plugin_json.read_text())
        old = dest.parent / f".{dest.name}.old"
        shutil.rmtree(old, ignore_errors=True)
        if dest.exists():
            dest.rename(old)
        staging.rename(dest)
        shutil.rmtree(old, ignore_errors=True)
        return dest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def plugin_version(marketplace: Path, plugin: str) -> str | None:
    try:
        manifest = marketplace / "plugins" / plugin / ".claude-plugin" / "plugin.json"
        return json.loads(manifest.read_text()).get("version")
    except (OSError, ValueError):
        return None


class ClaudeCLI:
    """`claude plugin …` against one config dir. Output is scrubbed of the
    secrets it was given before it is logged."""

    def __init__(self, binary: str, config_dir: Path, *, secrets: tuple[str, ...] = (),
                 run: Callable = subprocess.run) -> None:
        self.binary = binary
        self.config_dir = config_dir
        self.secrets = tuple(s for s in secrets if s)
        self._run = run
        home = config_dir / "home"
        home.mkdir(parents=True, exist_ok=True)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }

    def __call__(self, *args: str, stdin: str | None = None) -> None:
        result = self._run(
            [self.binary, "plugin", *args], input=stdin, capture_output=True, text=True,
            env=self.env, timeout=CLI_TIMEOUT_S,
        )
        if result.returncode != 0:
            out = (result.stderr or result.stdout or "").strip()
            for secret in self.secrets:
                out = out.replace(secret, "<redacted>")
            raise RuntimeError(f"claude plugin {args[0]} failed: {out[-400:]}")


def install_plugin(cli: ClaudeCLI, marketplace: Path, plugin_id: str,
                   options: Mapping[str, object]) -> None:
    """Add (or refresh) the directory marketplace, install the plugin at user
    scope, and set its options from stdin. Every step is idempotent."""
    settings = cli.config_dir / "settings.json"
    if settings.exists():
        # miragen's harness leaves it read-only after prepare(); the CLI
        # replaces it atomically, but keep it writable for older CLIs.
        settings.chmod(0o644)
    cli("marketplace", "add", str(marketplace))
    cli("install", plugin_id, "--scope", "user")
    cli("configure", plugin_id, "--values-stdin", stdin=json.dumps(dict(options)))


def setup_plugins(environ: Mapping[str, str], secrets: Mapping[str, Path], cli: ClaudeCLI,
                  *, opener: Callable = urllib.request.urlopen) -> dict:
    status: dict[str, dict] = {}

    memory_url = (environ.get("MIRAGEN_MEMORY_URL") or "").strip()
    bridge = secrets.get("MIRAGEN_BRIDGE_API_KEY")
    if memory_url:
        try:
            options = {"daemon_url": memory_url.rstrip("/")}
            if bridge:
                options["token"] = bridge.read_text()
            install_plugin(cli, MIRAGEN_MARKETPLACE, "miragen-memory@miragen", options)
            status["miragen-memory"] = {
                "installed": True, "version": plugin_version(MIRAGEN_MARKETPLACE, "miragen-memory"),
                "token": bool(bridge)}
        except Exception as exc:  # noqa: BLE001 - reported, setup continues
            status["miragen-memory"] = {"installed": False, "error": str(exc)}
    else:
        status["miragen-memory"] = {"installed": False, "error": "MIRAGEN_MEMORY_URL unset"}

    design_url = (environ.get("MIRADESIGN_URL") or "").strip().rstrip("/")
    design_token = secrets.get("MIRADESIGN_API_KEY")
    if design_url:
        marketplace = cli.config_dir / "engineer-marketplaces" / "miradesign"
        source = "cached"
        try:
            try:
                data = fetch_bundle(design_url, design_token.read_text() if design_token else None,
                                    opener=opener)
                unpack_marketplace(data, marketplace, "miradesign")
                source = "daemon"
            except (OSError, ValueError, urllib.error.URLError, tarfile.TarError) as exc:
                if not marketplace.is_dir():
                    raise RuntimeError(f"bundle unavailable and none cached: {exc}") from exc
                log(f"miradesign bundle fetch failed ({exc}); using the cached copy")
            options: dict[str, object] = {"daemon_url": design_url, "wake_idle_sessions": "false"}
            if design_token:
                options["token"] = design_token.read_text()
            install_plugin(cli, marketplace, "miradesign@miradesign", options)
            status["miradesign"] = {
                "installed": True, "source": source,
                "version": plugin_version(marketplace, "miradesign"), "token": bool(design_token)}
        except Exception as exc:  # noqa: BLE001
            status["miradesign"] = {"installed": False, "error": str(exc)}
    else:
        status["miradesign"] = {"installed": False, "error": "MIRADESIGN_URL unset"}
    return status


# ── main ────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> None:
    if not argv:
        argv = ["miragen", "run"]
    environ = dict(os.environ)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    secrets = materialize_secrets(environ, RUN_DIR)
    ca = write_ca_bundle(environ, RUN_DIR)

    config_dir = Path(environ.get("MIRAGEN_CLAUDE_HOME", "/agent/claude-home"))
    config_dir.mkdir(parents=True, exist_ok=True)
    cli = ClaudeCLI(
        environ.get("CLAUDE_BIN") or "claude", config_dir,
        secrets=tuple(p.read_text() for p in secrets.values()),
    )
    plugins = setup_plugins(environ, secrets, cli)

    status = {
        "secrets": sorted(str(p) for p in secrets.values()),
        "ca_bundle": str(ca) if ca else None,
        "plugins": plugins,
    }
    (RUN_DIR / "status.json").write_text(json.dumps(status, indent=1, sort_keys=True))
    log(json.dumps(status, sort_keys=True))
    failed = [name for name, entry in plugins.items() if not entry.get("installed")]
    if failed and environ.get("ENGINEER_SETUP_STRICT") == "1":
        raise SystemExit(f"engineer-setup: plugins not set up: {', '.join(failed)}")

    os.execvpe(argv[0], argv, environ)


if __name__ == "__main__":
    main(sys.argv[1:])
