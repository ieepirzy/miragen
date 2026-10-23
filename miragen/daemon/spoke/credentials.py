"""Where an agent's model access comes from. The seam, not an implementation.

MiraRun ADR-045 decision 3: inference goes through the deployment's provider
gateway (Luotsi). A workload holds only a short-lived token bound to itself
and its tenant, and no model credential ever enters a sandbox namespace.
Luotsi's inference capability does not exist yet, so the only source here is
`NoModelCredentials`: an agent spawned today gets no model access from the
spoke at all.

A Luotsi source returns only non-credential environment (the gateway's base
URL). How the short-lived workload token then reaches the agent is still
open, and deliberately not solved here by putting it into the pod spec: the
driver refuses credential-looking names, and a token in the spec is readable
by anything that may read pods in the namespace. The candidates are a
per-turn header carried by the delivery relay, or a projected volume the
spoke refreshes. Luotsi's API is Luotsi's to define; nothing here guesses it.
"""

from __future__ import annotations

from typing import Protocol

from miragen.daemon.spoke.hub import ClaimedIntent


class ModelCredentialSource(Protocol):
    def environment(self, intent: ClaimedIntent) -> dict[str, str]:
        """Environment for this one workload's model access. It must never
        hold a long-lived credential (the driver refuses credential-looking
        names anyway)."""


class NoModelCredentials:
    def environment(self, intent: ClaimedIntent) -> dict[str, str]:
        return {}
