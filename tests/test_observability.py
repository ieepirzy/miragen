"""Sentry stays off without a DSN, and nothing credential-shaped leaves."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from miragen import observability
from miragen.observability import (
    DEFAULT_TRACES_SAMPLE_RATE,
    FILTERED,
    init_sentry,
    read_build_revision,
    scrub_event,
    sentry_options,
)

# Obviously fake credentials in real shapes.
GITHUB_INSTALLATION = "ghs_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"  # gitleaks:allow
GITHUB_PAT = "github_pat_FAKE0FAKE0FAKE0FAKE0FAKE0FAKE0FAKE0FAKE0"  # gitleaks:allow
JWT = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJmYWtlLXRlc3QifQ.ZmFrZS1zaWduYXR1cmUtZm9yLXRlc3Rz"  # gitleaks:allow
ANTHROPIC = "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKEFAKE"  # gitleaks:allow
BEARER = "fake-daemon-bearer-0123456789"  # gitleaks:allow
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"  # gitleaks:allow
    "MIIEfakefakefakefakefakefakefakefakefakefakefake\n"
    "-----END RSA PRIVATE KEY-----"
)
DSN = "https://public@sentry.example.invalid/1"
SECRETS = (GITHUB_INSTALLATION, GITHUB_PAT, JWT, ANTHROPIC, BEARER, "oauth-code-123", "Gh5tate")


def _event() -> dict[str, Any]:
    return {
        "message": f"push failed with token {GITHUB_INSTALLATION}",
        "exception": {
            "values": [
                {
                    "type": "HTTPError",
                    "value": f"401 for https://x-access-token:{GITHUB_PAT}@github.com/o/r.git",
                    "stacktrace": {"frames": [{"function": "push", "vars": {"t": JWT}}]},
                }
            ]
        },
        "request": {
            "url": "https://bridge.example.invalid/oauth/callback",
            "query_string": "code=oauth-code-123&state=Gh5tate&page=2",
            "headers": {
                "Authorization": f"Bearer {BEARER}",
                "Cookie": "session=abc",
                "Accept": "application/json",
            },
        },
        "extra": {"private_key": PEM, "note": PEM, "model_token": ANTHROPIC, "hint": ANTHROPIC},
        "breadcrumbs": {
            "values": [
                {
                    "category": "httpx",
                    "data": {
                        "url": "https://api.github.com/app/installations/1/access_tokens",
                        "http.query": "access_token=" + BEARER,
                        "headers": [["authorization", "token " + GITHUB_PAT]],
                    },
                }
            ]
        },
    }


def test_scrubber_removes_every_secret_and_keeps_the_rest() -> None:
    scrubbed = scrub_event(_event())
    assert scrubbed is not None
    text = json.dumps(scrubbed)

    for secret in (*SECRETS, "BEGIN RSA PRIVATE KEY", "session=abc"):
        assert secret not in text, secret
    headers = scrubbed["request"]["headers"]
    assert headers["Authorization"] == FILTERED
    assert headers["Accept"] == "application/json"
    assert scrubbed["request"]["query_string"] == f"code={FILTERED}&state={FILTERED}&page=2"
    # What makes the event useful survives.
    assert "push failed with token" in scrubbed["message"]
    assert scrubbed["exception"]["values"][0]["type"] == "HTTPError"
    assert "github.com/o/r.git" in scrubbed["exception"]["values"][0]["value"]


def test_unscrubbed_fixture_really_contains_the_secrets() -> None:
    # Guards the test above: if the fixture lost a secret, the absence
    # assertion would pass for nothing.
    text = json.dumps(_event())
    for secret in SECRETS:
        assert secret in text, secret


def test_scrubber_drops_an_event_it_cannot_scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(value: Any, depth: int = 0) -> Any:
        raise RuntimeError("cannot scrub")

    monkeypatch.setattr(observability, "_scrub", boom)
    assert scrub_event(_event()) is None


class _Capture(Transport):
    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)


def test_real_sdk_event_passes_through_the_scrubber(tmp_path: Path) -> None:
    revision = tmp_path / "BUILD_REVISION"
    revision.write_text("a" * 40 + "\n")
    options = sentry_options("miragend", {"SENTRY_DSN": DSN}, revision_file=revision)
    assert options is not None
    transport = _Capture()
    sentry_sdk.init(**options, transport=transport, default_integrations=False)
    try:
        secret_in_scope = GITHUB_INSTALLATION  # noqa: F841 - would be a frame local
        try:
            raise RuntimeError(f"clone failed: {GITHUB_PAT} with Bearer {BEARER}")
        except RuntimeError:
            sentry_sdk.capture_exception()
        sentry_sdk.flush()
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.init()  # back to a disabled client for other tests

    payloads = [item.get_bytes().decode() for env in transport.envelopes for item in env.items]
    assert payloads, "no event reached the transport"
    sent = "\n".join(payloads)
    assert GITHUB_PAT not in sent
    assert GITHUB_INSTALLATION not in sent
    assert BEARER not in sent
    assert "clone failed" in sent
    assert '"release":"' + "a" * 40 + '"' in sent.replace(" ", "")


def test_no_dsn_means_off(tmp_path: Path) -> None:
    assert sentry_options("miragend", {}) is None
    assert sentry_options("miragend", {"SENTRY_DSN": "  "}) is None
    assert sentry_options("miragend", {"SENTRY_DSN_FILE": str(tmp_path / "missing")}) is None
    empty = tmp_path / "dsn"
    empty.write_text("\n")
    assert sentry_options("miragend", {"SENTRY_DSN_FILE": str(empty)}) is None


def test_init_without_dsn_never_touches_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
    assert init_sentry("memory-worker", {}) is False
    assert calls == []


def test_init_with_dsn_but_no_sdk_stays_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_sentry(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "sentry_sdk":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sentry)
    assert init_sentry("miragend", {"SENTRY_DSN": DSN}) is False


def test_dsn_file_wins_and_options_are_safe(tmp_path: Path) -> None:
    dsn_file = tmp_path / "dsn"
    dsn_file.write_text("https://file@sentry.example.invalid/2\n")
    options = sentry_options(
        "memory-worker",
        {"SENTRY_DSN": DSN, "SENTRY_DSN_FILE": str(dsn_file)},
        revision_file=tmp_path / "absent",
    )
    assert options is not None
    assert options["dsn"] == "https://file@sentry.example.invalid/2"
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["traces_sample_rate"] == DEFAULT_TRACES_SAMPLE_RATE
    assert options["environment"] == "production"
    assert options["server_name"] == "memory-worker"
    assert options["release"] is None
    for hook in ("before_send", "before_send_transaction", "before_breadcrumb"):
        assert options[hook] is scrub_event


@pytest.mark.parametrize(
    ("content", "expected"),
    [("", None), ("unknown\n", None), ("  ", None), ("b" * 40 + "\n", "b" * 40)],
)
def test_build_revision(tmp_path: Path, content: str, expected: str | None) -> None:
    path = tmp_path / "BUILD_REVISION"
    path.write_text(content)
    assert read_build_revision(path) == expected
    assert read_build_revision(tmp_path / "missing") is None


def test_release_and_rate_overrides(tmp_path: Path) -> None:
    revision = tmp_path / "BUILD_REVISION"
    revision.write_text("c" * 40)
    options = sentry_options(
        "miragend",
        {
            "SENTRY_DSN": DSN,
            "SENTRY_RELEASE": "miragend@1",
            "SENTRY_ENVIRONMENT": "staging",
            "SENTRY_TRACES_SAMPLE_RATE": "7",
        },
        revision_file=revision,
    )
    assert options is not None
    assert options["release"] == "miragend@1"
    assert options["environment"] == "staging"
    assert options["traces_sample_rate"] == 1.0
    bad = sentry_options(
        "miragend", {"SENTRY_DSN": DSN, "SENTRY_TRACES_SAMPLE_RATE": "x"}, revision_file=revision
    )
    assert bad is not None
    assert bad["traces_sample_rate"] == DEFAULT_TRACES_SAMPLE_RATE
    assert bad["release"] == "c" * 40


def test_credential_query_params_in_free_text_are_filtered() -> None:
    # httpx puts the full URL, query included, into its error message, and
    # a log line can quote one too: the raw-query-string keys never see it.
    message = (
        "Client error '400 Bad Request' for url "
        "'https://api.example.invalid/v1/me?access_token=plainvalue123&fields=id'"
    )
    scrubbed = scrub_event({"message": message, "logentry": {"formatted": message}})
    assert scrubbed is not None
    assert "plainvalue123" not in json.dumps(scrubbed)
    assert f"?access_token={FILTERED}&fields=id'" in scrubbed["message"]


def test_malformed_dsn_never_stops_the_process() -> None:
    # sentry_sdk.init raises BadDsn on a typo; init_sentry must swallow it.
    sentry_sdk.init()
    assert init_sentry("miragend", {"SENTRY_DSN": "not a dsn"}) is False
    assert sentry_sdk.get_client().dsn is None
