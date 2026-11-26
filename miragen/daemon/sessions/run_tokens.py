"""Run tokens: how an agent with no person behind it opens the bridge's /mcp.

A control plane that launches agents can mint each run a short-lived ES256
JWT bound to one audience (this bridge's) and signed with its workload key,
whose public half it publishes as a JWKS. The agent sends that token as its
bearer on /mcp. It never opens the session API (/sessions/v1/*), which keeps
the daemon bearer.

What a token must show (all of it, or it is refused):

- ``alg`` is ES256 and nothing else; the key comes from the configured JWKS
  by ``kid``. A JWKS entry carrying private material is ignored.
- ``iss`` and ``aud`` are exact; ``exp`` and ``iat`` are present, at most
  900 s apart, and current within 30 s of leeway (``nbf`` too, if present).
- ``tenant`` is the configured tenant, and ``act.sub`` is an agent principal
  of it (``agent.<tenant>.<class>.<name>[.<slot>]``) that the bridge was
  configured to admit. ``sub`` equals ``act.sub``.

What it maps to: the caller is the agent principal ``act.sub`` (the
principal the run is executed under). It acts through the daemon's one
memory principal, with exactly the reach a signed-in person has on /mcp; the
project stays a tool argument, since no claim names one. The run id
(``run_intent_id``) and, when the issuer adds them, the originator
(``orig``) and root principal (``obo``) are kept for audit and shown by
``bridge_status``; none of them widens anything.

Keys: fetched on first use, refetched every 5 minutes, and at most once a
minute for an unknown ``kid``. A failed fetch keeps the last good set for at
most an hour; after that every token is refused. Nothing here logs a token.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

logger = logging.getLogger("miragend.run_tokens")

MAX_LIFETIME_SECONDS = 900
LEEWAY_SECONDS = 30
JWKS_TTL_SECONDS = 300
JWKS_REFETCH_SECONDS = 60
JWKS_MAX_STALE_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 5.0
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_PRINCIPAL_TAIL = r"(\.[a-z0-9][a-z0-9-]{0,62}){2,3}"
_CLAIM_MAX = 200


def principal_re(tenant: str) -> re.Pattern[str]:
    """`agent.<tenant>.<class>.<name>[.<slot>]` for this tenant."""
    return re.compile(rf"^agent\.{re.escape(tenant)}{_PRINCIPAL_TAIL}$")


@dataclass(frozen=True)
class RunTokenSettings:
    jwks_url: str
    issuer: str
    audience: str
    tenant: str
    agents: frozenset[str]  # agent principals admitted


@dataclass(frozen=True)
class RunCaller:
    """What a valid run token proves."""

    principal: str  # act.sub: agent.<tenant>.<class>.<name>[.<slot>]
    run_id: str | None  # run_intent_id
    originator: str | None  # orig, when the issuer sets it
    root: str | None  # obo, when the issuer sets it

    def describe(self) -> dict[str, Any]:
        return {"kind": "run_token", "principal": self.principal, "run_id": self.run_id,
                "originator": self.originator, "root": self.root}


def _short(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
        return str(value)[:_CLAIM_MAX]
    return None


def _http_fetch(url: str) -> dict[str, Any]:
    import httpx

    response = httpx.get(url, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
    response.raise_for_status()
    return response.json()


class _Jwks:
    """The issuer's public keys. The HTTP fetch never runs under the lock:
    one caller refreshes while the others keep using the cached set."""

    def __init__(self, url: str, fetch: Callable[[str], dict[str, Any]],
                 clock: Callable[[], float]):
        self._url = url
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._tried_at: float | None = None
        self._refreshing = False

    def _load(self) -> dict[str, Any] | None:
        try:
            document = self._fetch(self._url)
            keys: dict[str, Any] = {}
            for entry in document.get("keys") or []:
                if not isinstance(entry, dict) or "d" in entry:
                    continue  # never trust private material
                if entry.get("kty") != "EC" or entry.get("crv") != "P-256" or not entry.get("kid"):
                    continue
                keys[str(entry["kid"])] = jwt.PyJWK(entry, algorithm="ES256").key
            return keys
        except Exception as error:  # noqa: BLE001 - a failed fetch keeps the last good set
            logger.warning("signing key set fetch failed (%s)", type(error).__name__)
            return None

    def key(self, kid: str) -> Any | None:
        with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or now - self._fetched_at >= JWKS_TTL_SECONDS
            unknown = kid not in self._keys
            may_retry = self._tried_at is None or now - self._tried_at >= JWKS_REFETCH_SECONDS
            refresh = (stale or unknown) and may_retry and not self._refreshing
            if refresh:
                self._refreshing = True
                self._tried_at = now
        if refresh:
            keys = self._load()  # outside the lock
            with self._lock:
                self._refreshing = False
                if keys is not None:
                    self._keys = keys
                    self._fetched_at = self._clock()
        with self._lock:
            fetched = self._fetched_at
            if fetched is None or self._clock() - fetched > JWKS_MAX_STALE_SECONDS:
                return None
            return self._keys.get(kid)


class RunTokenVerifier:
    def __init__(
        self,
        settings: RunTokenSettings,
        *,
        fetch: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self._principal = principal_re(settings.tenant)
        self._clock = clock
        self._jwks = _Jwks(settings.jwks_url, fetch or _http_fetch, clock)

    @staticmethod
    def looks_like_jwt(token: str) -> bool:
        return bool(token) and token.count(".") == 2

    def caller(self, token: str | None) -> RunCaller | None:
        """The agent this token proves, or None. Never raises on a bad token.
        May block on a JWKS fetch (up to HTTP_TIMEOUT_SECONDS): call it off
        the event loop."""
        if not token or not self.looks_like_jwt(token):
            return None
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None
        if header.get("alg") != "ES256" or not isinstance(header.get("kid"), str):
            return None
        key = self._jwks.key(header["kid"])
        if key is None:
            return None
        s = self.settings
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["ES256"],
                audience=s.audience,
                issuer=s.issuer,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    # Time is checked below against the injected clock.
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.PyJWTError:
            return None
        try:
            exp, iat = int(claims["exp"]), int(claims["iat"])
            nbf = int(claims.get("nbf", iat))
        except (TypeError, ValueError):
            return None
        now = self._clock()
        if exp <= now - LEEWAY_SECONDS or iat > now + LEEWAY_SECONDS or nbf > now + LEEWAY_SECONDS:
            return None
        if exp - iat > MAX_LIFETIME_SECONDS or exp <= iat:
            return None
        act = claims.get("act")
        principal = act.get("sub") if isinstance(act, dict) else None
        if not isinstance(principal, str) or claims.get("sub") != principal:
            return None
        if claims.get("tenant") != s.tenant or not self._principal.fullmatch(principal):
            return None
        if principal not in s.agents:
            return None
        return RunCaller(
            principal=principal,
            run_id=_short(claims.get("run_intent_id")),
            originator=_short(claims.get("orig")),
            root=_short(claims.get("obo")),
        )
