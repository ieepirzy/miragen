"""OAuth for upstream MCP servers behind origo (or any RFC 8414 server whose
authorization endpoint auto-approves registered clients).

The gateway holds the client credential and obtains, caches and refreshes
access tokens itself; the harness never sees any of it. The flow is the
authorization-code grant with PKCE, headless because the server is
configured to auto-approve this client: ``GET /authorize`` answers with a
redirect carrying the code instead of a consent page. A server that shows a
consent page is refused loudly (auto-approve off) — the gateway never fills
in a consent form.

Config (under an MCP capability)::

    oauth:
      client_id: mira                 # or register: true (open dynamic registration)
      client_secret_env: MIRADB_OAUTH_SECRET
      redirect_uri: https://claude.ai/api/mcp/auth_callback   # one the server allowlists
      scope: mcp                      # optional
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


class UpstreamAuthError(RuntimeError):
    pass


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class OAuthUpstream:
    resource: str                 # the MCP URL (RFC 8707 resource)
    redirect_uri: str
    client_id: str | None = None
    client_secret: str | None = None
    register: bool = False
    scope: str | None = None
    transport: httpx.AsyncBaseTransport | None = None
    _meta: dict | None = field(default=None, init=False, repr=False)
    _access: str | None = field(default=None, init=False, repr=False)
    _refresh: str | None = field(default=None, init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @classmethod
    def from_config(cls, resource: str, cfg: dict, env: dict[str, str], *,
                    transport: httpx.AsyncBaseTransport | None = None) -> "OAuthUpstream":
        if not cfg.get("redirect_uri"):
            raise ValueError("oauth needs a redirect_uri the server allowlists for this client")
        register = bool(cfg.get("register", False))
        client_id = cfg.get("client_id")
        secret_env = cfg.get("client_secret_env")
        if not register and not client_id:
            raise ValueError("oauth needs client_id (or register: true)")
        secret = env.get(secret_env) if secret_env else None
        if secret_env and not secret:
            raise ValueError(f"oauth client secret env '{secret_env}' is not set")
        return cls(resource=resource, redirect_uri=cfg["redirect_uri"], client_id=client_id,
                   client_secret=secret, register=register, scope=cfg.get("scope"),
                   transport=transport)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self.transport, follow_redirects=False)

    async def _metadata(self, http: httpx.AsyncClient) -> dict:
        if self._meta is None:
            parts = urlparse(self.resource)
            origin = f"{parts.scheme}://{parts.netloc}"
            resp = await http.get(f"{origin}/.well-known/oauth-authorization-server")
            if resp.status_code != 200:
                raise UpstreamAuthError(f"no OAuth metadata at {origin} (http {resp.status_code})")
            self._meta = resp.json()
        return self._meta

    async def token(self) -> str:
        async with self._lock:
            if self._access and time.monotonic() < self._expires_at - 30:
                return self._access
            async with self._client() as http:
                meta = await self._metadata(http)
                if self._refresh:
                    try:
                        return await self._exchange(http, meta, {
                            "grant_type": "refresh_token", "refresh_token": self._refresh})
                    except UpstreamAuthError:
                        self._refresh = None  # fall through to a fresh authorization
                if self.register and not self.client_id:
                    await self._register(http, meta)
                return await self._authorize(http, meta)

    def invalidate(self) -> None:
        self._access = None
        self._expires_at = 0.0

    async def _register(self, http: httpx.AsyncClient, meta: dict) -> None:
        endpoint = meta.get("registration_endpoint")
        if not endpoint:
            raise UpstreamAuthError("server offers no dynamic client registration")
        resp = await http.post(endpoint, json={
            "client_name": "miragen-gateway", "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post"})
        if resp.status_code not in (200, 201):
            raise UpstreamAuthError(f"client registration failed (http {resp.status_code})")
        body = resp.json()
        self.client_id, self.client_secret = body["client_id"], body.get("client_secret")

    async def _authorize(self, http: httpx.AsyncClient, meta: dict) -> str:
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(16)
        params = {"response_type": "code", "client_id": self.client_id,
                  "redirect_uri": self.redirect_uri, "code_challenge": challenge,
                  "code_challenge_method": "S256", "state": state, "resource": self.resource}
        if self.scope:
            params["scope"] = self.scope
        resp = await http.get(f"{meta['authorization_endpoint']}?{urlencode(params)}")
        if resp.status_code not in (301, 302, 303, 307):
            raise UpstreamAuthError(
                f"authorization did not redirect (http {resp.status_code}): the server wants "
                "interactive consent for this client — enable auto-approve for it")
        location = resp.headers.get("location", "")
        query = parse_qs(urlparse(location).query)
        if query.get("error"):
            raise UpstreamAuthError(f"authorization refused: {query['error'][0]}")
        if query.get("state", [""])[0] != state or not query.get("code"):
            raise UpstreamAuthError("authorization redirect carried no matching code/state")
        return await self._exchange(http, meta, {
            "grant_type": "authorization_code", "code": query["code"][0],
            "redirect_uri": self.redirect_uri, "code_verifier": verifier})

    async def _exchange(self, http: httpx.AsyncClient, meta: dict, form: dict) -> str:
        form = {**form, "client_id": self.client_id, "resource": self.resource}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        resp = await http.post(meta["token_endpoint"], data=form)
        if resp.status_code != 200:
            raise UpstreamAuthError(f"token endpoint refused {form['grant_type']} (http {resp.status_code})")
        body = resp.json()
        self._access = body["access_token"]
        self._refresh = body.get("refresh_token") or self._refresh
        self._expires_at = time.monotonic() + float(body.get("expires_in") or 3600)
        return self._access
