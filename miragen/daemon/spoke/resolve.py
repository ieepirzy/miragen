"""EDF resolution inside the cluster (MiraRun ADR-050 Decision 4).

The hub sends a `resolve` delivery with the source EDF. The spoke checks it
against its own allowlist for the namespace (`namespaces.<ns>.edf` in the
spoke's config, never anything from the hub), and only then resolves it
with miragen's own resolver, which produces the canonical document and its
SHA-256 exactly as for any other caller. A launch carries the EDF and that
hash again; the spoke re-resolves under its current allowlist and refuses
the launch if the EDF is no longer allowed or the hash differs.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from miragen.daemon.spoke.config import EdfPolicy
from miragen.edf import (
    EDFValidationError,
    ResolutionContext,
    ResolvedEDF,
    resolve_edf,
    validate_edf,
)

MAX_ERRORS = 50


class EdfRefused(Exception):
    """The EDF does not validate, or names something the allowlist does not."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("; ".join(str(e.get("message")) for e in errors[:3]))
        self.errors = errors[:MAX_ERRORS]


def allowlist_errors(document: Any, policy: EdfPolicy | None) -> list[dict[str, Any]]:
    """What the EDF names that this namespace does not allow. A namespace
    without an `edf` section allows no EDF at all."""
    if policy is None:
        return [{"loc": "", "message": "this namespace resolves no EDF (no edf allowlist)"}]
    edf = validate_edf(document)
    errors: list[dict[str, Any]] = []
    preset = edf.spec.tools.preset
    if preset is not None and preset not in policy.presets:
        errors.append({"loc": "spec.tools.preset",
                       "message": f"preset '{preset}' is not allowed in this namespace"})
    for index, server in enumerate(edf.spec.tools.mcp_servers):
        if not policy.allows_tool_server(server.url):
            errors.append({"loc": f"spec.tools.mcpServers.{index}.url",
                           "message": f"tool server '{server.url}' is not allowed in this namespace"})
    for index, target in enumerate(edf.spec.targets):
        if target.name not in policy.targets:
            errors.append({"loc": f"spec.targets.{index}.name",
                           "message": f"target '{target.name}' is not allowed in this namespace"})
    return errors


def resolve_allowed(
    document: Any, policy: EdfPolicy | None, *, instructions: str | None = None
) -> ResolvedEDF:
    """Validate, check against the allowlist, then resolve. Raises EdfRefused."""
    try:
        errors = allowlist_errors(document, policy)
        if errors:
            raise EdfRefused(errors)
        context = ResolutionContext(instructions=instructions) if instructions else None
        return resolve_edf(document, context=context)
    except EDFValidationError as exc:
        raise EdfRefused(list(exc.errors) or [{"loc": "", "message": str(exc)}]) from exc
    except ValidationError as exc:
        # The resolution context (instructions) itself is malformed.
        raise EdfRefused([{"loc": "instructions", "message": str(e.get("msg"))}
                          for e in exc.errors()]) from exc
