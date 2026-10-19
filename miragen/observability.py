"""Optional Sentry error reporting for miragend and the memory worker.

Inert unless both are true: the ``sentry`` extra is installed
(``pip install "miragen[sentry]"``) and a DSN is configured. Agent tracing is a
separate concern (``miragen.telemetry``, OTLP); this module only reports
unhandled errors.

* ``SENTRY_DSN`` or ``SENTRY_DSN_FILE`` (the file wins when both are set).
  Neither set, or an empty/unreadable file: nothing is sent and the SDK is not
  even imported. A DSN without the SDK installed logs a warning and stays off.
* ``SENTRY_RELEASE`` overrides the release. Otherwise the release is the
  commit the image was built from, read from :data:`BUILD_REVISION_FILE`
  (``Dockerfile.miragend`` writes it from the ``BUILD_REVISION`` build arg).
  Empty, missing or ``unknown`` means no release at all, never a shared fake
  release that would pool unrelated builds together.
* ``SENTRY_ENVIRONMENT`` (default ``production``).
* ``SENTRY_TRACES_SAMPLE_RATE`` (default 0.01).

These processes hold bearer tokens (the daemon's, the bridge's OAuth state,
Loimi principals, model subscription tokens). So: no PII, no frame locals, no
request bodies, and :func:`scrub_event` runs over every event, transaction and
breadcrumb before it leaves the process.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BUILD_REVISION_FILE = Path("/app/BUILD_REVISION")
DEFAULT_TRACES_SAMPLE_RATE = 0.01
FILTERED = "[Filtered]"

# Header, cookie, query, form or extra key names whose value is dropped whole,
# whatever it looks like. Matched case-insensitively on the full name after
# normalising "-" to "_".
_SENSITIVE_KEY = re.compile(
    r"^(?:authorization|proxy_authorization|cookie|set_cookie|x_api_key|api_key|"
    r"apikey|x_hub_signature(?:_256)?|x_[a-z_]*token|code|state|token|"
    r"access_token|refresh_token|id_token|client_secret|password|passwd|secret|"
    r"private_key|code_verifier|signature|sig|dsn)$"
    r"|(?:_token|_secret|_password|_private_key|_key)$",
    re.IGNORECASE,
)

# Well-known credential shapes, redacted wherever they appear in text
# (exception messages, log lines, URLs).
_TOKEN_SHAPES: tuple[tuple[str, re.Pattern[str], int | str], ...] = (
    (
        "private_key_block",
        re.compile(
            r"-{2,}\s*BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?\s*-{2,}"
            r"(?:.*?-{2,}\s*END [A-Z ]*PRIVATE KEY(?: BLOCK)?\s*-{2,}|.*\Z)",
            re.DOTALL | re.IGNORECASE,
        ),
        0,
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"), 0),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
        0,
    ),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), 0),
    (
        "url_credentials",
        re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://(?P<secret>[^\s/:@\[\]]+:[^\s/@\[\]]+)@"),
        "secret",
    ),
    (
        "bearer_token",
        re.compile(r"(?i:\b(?:bearer|basic|token))\s+(?P<secret>[A-Za-z0-9._~+/-]{16,}=*)"),
        "secret",
    ),
)


def redact_text(text: str) -> str:
    """``text`` with every credential shape replaced by ``[redacted:<kind>]``."""
    for name, pattern, group in _TOKEN_SHAPES:
        marker = f"[redacted:{name}]"

        def replace(match: re.Match[str], marker: str = marker, group: int | str = group) -> str:
            whole = match.group(0)
            start, end = match.span(group)
            offset = match.start()
            return whole[: start - offset] + marker + whole[end - offset :]

        text = pattern.sub(replace, text)
    return text


_MAX_DEPTH = 16
# Where Sentry keeps a raw query string: the request interface, and the
# httpx/stdlib HTTP breadcrumbs.
_QUERY_KEYS = frozenset({"query_string", "http.query"})
# A query parameter inside free text: an httpx error message or a log line
# quoting a full URL ("...?access_token=...&page=2"). The parameter's name
# decides, exactly as for a raw query string.
_TEXT_QUERY_PARAM = re.compile(r"(?P<head>[?&](?P<name>[A-Za-z0-9_.-]+)=)[^&#\s'\"<>]+")


def _is_sensitive_key(key: object) -> bool:
    return isinstance(key, str) and bool(_SENSITIVE_KEY.search(key.replace("-", "_")))


# Query parameters that carry a credential although the name alone would be
# too broad as a dict key (Google APIs take the API key as ``key=``).
_SENSITIVE_QUERY_ONLY = frozenset({"key"})


def _is_sensitive_param(name: str) -> bool:
    return name.lower() in _SENSITIVE_QUERY_ONLY or _is_sensitive_key(name)


def _is_raw_log_parameter(key: object, mapping: Mapping[Any, Any]) -> bool:
    """A log call's %-arguments, kept apart from the formatted message.

    They travel twice: inside the formatted message, where context such as
    ``Bearer <x>`` lets the scrubber recognise a secret, and alone (an
    event's ``logentry.params``, a log's ``sentry.message.parameter.N``),
    where an opaque token has no shape to catch. The alone copy is dropped.
    """
    return isinstance(key, str) and (
        key.startswith("sentry.message.parameter.") or (key == "params" and "formatted" in mapping)
    )


def _redact_text_query_params(value: str) -> str:
    if "=" not in value:
        return value
    return _TEXT_QUERY_PARAM.sub(
        lambda m: m["head"] + FILTERED if _is_sensitive_param(m["name"]) else m[0],
        value,
    )


def _scrub_query_string(value: str) -> str:
    parts = []
    for pair in value.split("&"):
        name, sep, _ = pair.partition("=")
        parts.append(f"{name}={FILTERED}" if sep and _is_sensitive_param(name) else pair)
    return redact_text("&".join(parts))


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        # Too deep to inspect: drop it rather than send it unread.
        return FILTERED
    if isinstance(value, str):
        return redact_text(_redact_text_query_params(value))
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_raw_log_parameter(key, value):
                continue
            if _is_sensitive_key(key):
                out[key] = FILTERED
            elif key in _QUERY_KEYS and isinstance(item, str):
                out[key] = _scrub_query_string(item)
            else:
                out[key] = _scrub(item, depth + 1)
        return out
    if isinstance(value, list | tuple):
        # Header lists come as [name, value] pairs.
        if len(value) == 2 and isinstance(value[0], str) and _is_sensitive_key(value[0]):
            return [value[0], FILTERED]
        return [_scrub(item, depth + 1) for item in value]
    return value


def scrub_event(event: dict[str, Any], hint: object = None) -> dict[str, Any] | None:
    """``event`` with credential-named fields dropped and token-shaped text redacted.

    Used as ``before_send``, ``before_send_transaction`` and (one breadcrumb at
    a time) ``before_breadcrumb``. Never raises: an event that cannot be
    scrubbed is dropped rather than sent as it is.
    """
    try:
        return _scrub(event)
    except Exception:  # noqa: BLE001 - never send an unscrubbed event
        logger.exception("dropping a Sentry event that could not be scrubbed")
        return None


def read_build_revision(path: Path = BUILD_REVISION_FILE) -> str | None:
    """The image's build commit, or None when the file is missing, empty or ``unknown``."""
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    if not value or value == "unknown":
        return None
    return value


def _read_dsn(env: Mapping[str, str]) -> str | None:
    dsn_file = env.get("SENTRY_DSN_FILE", "").strip()
    if dsn_file:
        try:
            value = Path(dsn_file).read_text().strip()
        except OSError:
            logger.warning("SENTRY_DSN_FILE is set but unreadable; Sentry stays off")
            return None
        return value or None
    return env.get("SENTRY_DSN", "").strip() or None


def _traces_rate(env: Mapping[str, str]) -> float:
    raw = env.get("SENTRY_TRACES_SAMPLE_RATE", "").strip()
    if not raw:
        return DEFAULT_TRACES_SAMPLE_RATE
    try:
        rate = float(raw)
    except ValueError:
        logger.warning("SENTRY_TRACES_SAMPLE_RATE=%r is not a number; using default", raw)
        return DEFAULT_TRACES_SAMPLE_RATE
    return min(max(rate, 0.0), 1.0)


def sentry_options(
    service: str,
    env: Mapping[str, str] | None = None,
    revision_file: Path = BUILD_REVISION_FILE,
) -> dict[str, Any] | None:
    """The ``sentry_sdk.init`` keyword arguments, or None when Sentry is off."""
    env = os.environ if env is None else env
    dsn = _read_dsn(env)
    if not dsn:
        return None
    release = env.get("SENTRY_RELEASE", "").strip() or read_build_revision(revision_file)
    return {
        "dsn": dsn,
        "release": release,
        "environment": env.get("SENTRY_ENVIRONMENT", "").strip() or "production",
        "server_name": service,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "traces_sample_rate": _traces_rate(env),
        "profiles_sample_rate": 0.0,
        "before_send": scrub_event,
        "before_send_transaction": scrub_event,
        "before_breadcrumb": scrub_event,
    }


def init_sentry(service: str, env: Mapping[str, str] | None = None) -> bool:
    """Start Sentry for this process; True when it was started. Never raises."""
    options = sentry_options(service, env)
    if options is None:
        logger.info("Sentry disabled for %s (no SENTRY_DSN)", service)
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.warning(
            "SENTRY_DSN is set but sentry-sdk is not installed (miragen[sentry]); "
            "Sentry stays off for %s",
            service,
        )
        return False
    try:
        sentry_sdk.init(**options)
        sentry_sdk.set_tag("service", service)
    except Exception:  # noqa: BLE001 - error reporting must never stop the service
        logger.exception("Sentry init failed for %s; continuing without it", service)
        return False
    logger.info(
        "Sentry enabled for %s (release %s, environment %s)",
        service,
        options["release"] or "none",
        options["environment"],
    )
    return True
