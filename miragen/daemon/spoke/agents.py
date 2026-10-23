"""The spoke's calls to an agent it runs, in-cluster.

The agent app's base-tier API (`miragen/app.py`), guarded by the agent's
`X-Miragen-Token`:

- `POST /instances/{name}/turns` `{prompt, idempotency_key, provenance?,
  mirarun?}`: starts a durable turn. A retried key returns the original
  turn with `duplicate: true`, so forwarding it again is safe;
- `DELETE /instances/{name}`: discards the instance's conversation (404:
  there was none);
- `POST /runs/{id}/resume`, `POST /runs/{id}/abandon`: take no idempotency
  key, so the spoke forwards them at most once;
- `GET /runs/{id}/events?after=N` and `GET /runs/{id}`: the run's event
  pages and its record.
"""

from __future__ import annotations

from typing import Any

import httpx

TURN_KEY_MAX = 200  # TurnRequest.idempotency_key max_length
# RunStatus (miragen/models.py) values in which a run is not running: its
# event stream is complete until something resumes it.
STOPPED_RUN_STATUSES = frozenset({"succeeded", "failed", "interrupted", "suspended", "abandoned"})


class AgentRejected(Exception):
    """The agent answered 4xx: the request itself is wrong, not delivered."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code


class AgentUnreachable(Exception):
    """Nothing was delivered: the connection never opened."""


class AgentOutcomeUnknown(Exception):
    """The request may or may not have reached the agent (a timeout, a broken
    connection, a 5xx). For at-most-once kinds this is `uncertain`."""


class AgentClient:
    def __init__(self, *, timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _send(
        self, method: str, endpoint: str, path: str, token: str, **kwargs: Any
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method, endpoint.rstrip("/") + path,
                headers={"X-Miragen-Token": token}, **kwargs,
            )
        except httpx.ConnectError as exc:
            raise AgentUnreachable(type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise AgentOutcomeUnknown(type(exc).__name__) from exc
        if response.status_code >= 500:
            raise AgentOutcomeUnknown(f"agent answered {response.status_code}")
        return response

    def start_turn(
        self,
        endpoint: str,
        token: str,
        *,
        instance: str,
        prompt: str,
        idempotency_key: str,
        provenance: dict[str, Any] | None = None,
        mirarun: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """`credentials` is the turn's run-scoped `TurnCredentials`
        (`secret_env`, `repositories`): held by the agent for this turn only."""
        body: dict[str, Any] = {"prompt": prompt, "idempotency_key": idempotency_key}
        if credentials:
            body["credentials"] = credentials
        if provenance:
            body["provenance"] = provenance
        if mirarun:
            body["mirarun"] = mirarun
        response = self._send("POST", endpoint, f"/instances/{instance}/turns", token, json=body)
        if response.status_code not in (200, 202):
            raise AgentRejected(response.status_code, _detail(response))
        return response.json()

    def discard_instance(self, endpoint: str, token: str, *, instance: str) -> None:
        response = self._send("DELETE", endpoint, f"/instances/{instance}", token)
        if response.status_code not in (200, 404):
            raise AgentRejected(response.status_code, _detail(response))

    def control_run(
        self, endpoint: str, token: str, run_id: str, action: str, **kwargs: Any
    ) -> dict[str, Any]:
        response = self._send("POST", endpoint, f"/runs/{run_id}/{action}", token, **kwargs)
        if response.status_code != 200:
            raise AgentRejected(response.status_code, _detail(response))
        return response.json()

    def events(
        self, endpoint: str, token: str, run_id: str, *, after: int, limit: int = 200
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        response = self._send(
            "GET", endpoint, f"/runs/{run_id}/events", token,
            params={"after": after, "limit": limit},
        )
        if response.status_code != 200:
            raise AgentRejected(response.status_code, _detail(response))
        return response.json()

    def get_turn(
        self, endpoint: str, token: str, *, instance: str, turn_id: str
    ) -> dict[str, Any] | None:
        """`GET /instances/{instance}/turns/{id}`: the turn's record, unchanged;
        None when the agent has no such turn."""
        response = self._send("GET", endpoint, f"/instances/{instance}/turns/{turn_id}", token)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise AgentRejected(response.status_code, _detail(response))
        record = response.json()
        return record if isinstance(record, dict) else None

    def run_record(self, endpoint: str, token: str, run_id: str) -> dict[str, Any] | None:
        """`GET /runs/{id}`, unchanged; None when the agent has no such run."""
        response = self._send("GET", endpoint, f"/runs/{run_id}", token)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise AgentRejected(response.status_code, _detail(response))
        record = response.json()
        return record if isinstance(record, dict) else None


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail[:200]
    return str(body)[:200]
