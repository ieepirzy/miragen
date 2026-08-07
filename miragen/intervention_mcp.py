"""The MCP `ask_human` tool — the layered-on variant of structured
interventions (docs/design/structured-interventions.md §5, now implemented).

The mechanism decision stands unchanged: the workspace sentinel file IS the
contract. This tool writes exactly `<workspace>/.miragen/intervention.json`
and nothing else — the base tier's end-of-turn detection, validation,
id-stamping, archiving, event emission and suspension all apply as if the
agent had written the file itself. An executor whose harness makes MCP tool
calls easier than file writes (or whose sandbox blocks dot-file writes but
allows MCP) gets the same protocol through a front door.

Serving: miragen's own FastAPI app mounts this server (streamable HTTP,
stateless) at `/mcp/ask-human`. Executor profiles opt in by pointing an
`executor.mcp_servers` entry at it — nothing is auto-injected:

    executor:
      mcp_servers:
        - name: miragen
          url: http://localhost:8000/mcp/ask-human
          bearer_token_env: MIRAGEN_INTERNAL_TOKEN   # when the app is token-guarded

Run binding: the tool call arrives in miragen's process, not the executor's
workspace, so the target run must be resolved. miragen serves one agent per
container, so the common case is exactly one `running` executor run — that
run's workspace is the target. With several concurrent runs the caller must
pass `run_id` (accepted as a full id or unique prefix); with none, or with a
question already pending, the tool errors and the agent falls back to
writing the sentinel file directly. Resolution reads the run store (the
authoritative record of running runs) — never trusts a caller-supplied
workspace path.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

if TYPE_CHECKING:  # imported lazily at call time to avoid cycles
    from miragen.executor import ExecutorBackend
    from miragen.runs import RunStore

logger = logging.getLogger("miragen.intervention_mcp")

_SENTINEL = "intervention.json"


class AskHumanError(Exception):
    """Raised when the question cannot be recorded; the message is the tool
    error the agent sees, and always says what to do instead."""


def record_ask_human(
    *,
    run_store: "RunStore",
    executor: "ExecutorBackend",
    question: str,
    kind: str | None = None,
    options: list[dict[str, Any]] | None = None,
    evidence: str | None = None,
    affected_repositories: list[str] | None = None,
    run_id: str | None = None,
) -> str:
    """Write the intervention sentinel for the resolved running run.

    Returns the confirmation message. The file's schema is validated by the
    base tier at end of turn (the schema is the contract; a malformed write
    is set aside there) — this function only refuses the structurally
    hopeless cases early: no question, no resolvable run, a pending file.
    """
    if not question or not question.strip():
        raise AskHumanError("`question` is required and must be non-empty")

    running = _running_records(run_store)

    if run_id is not None:
        matches = [r for r in running if r.run_id == run_id or r.run_id.startswith(run_id)]
        if not matches:
            raise AskHumanError(
                f"no running run matches run_id '{run_id}' — running: "
                f"{[r.run_id for r in running]}"
            )
        if len(matches) > 1:
            raise AskHumanError(
                f"run_id prefix '{run_id}' is ambiguous: {[r.run_id for r in matches]}"
            )
        record = matches[0]
    elif len(running) == 1:
        record = running[0]
    elif not running:
        raise AskHumanError(
            "no running executor run to bind the question to — if your turn is "
            "still active, write .miragen/intervention.json in your workspace "
            "instead and end your turn"
        )
    else:
        raise AskHumanError(
            "multiple runs are running; pass `run_id` to pick one: "
            f"{[r.run_id for r in running]}"
        )

    workspace = Path(record.workspace) if record.workspace else (
        Path(executor.spec.workspace_root) / record.run_id
    )
    marker_dir = workspace / ".miragen"
    sentinel = marker_dir / _SENTINEL
    if sentinel.exists():
        raise AskHumanError(
            "a question is already pending for this run — one intervention per "
            "turn; end your turn and the pending question will be raised"
        )

    payload: dict[str, Any] = {"question": question.strip()}
    if kind is not None:
        payload["kind"] = kind
    if options:
        payload["options"] = options
    if evidence is not None:
        payload["evidence"] = evidence
    if affected_repositories:
        payload["affected_repositories"] = affected_repositories

    marker_dir.mkdir(parents=True, exist_ok=True)
    tmp = sentinel.with_name(sentinel.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, sentinel)
    logger.info(f"ask_human recorded for run {record.run_id}")
    return (
        f"Question recorded for run {record.run_id}. End your turn now — the run "
        "will suspend with your question, and a human's answer arrives as your "
        "next prompt."
    )


def _running_records(run_store: "RunStore"):
    records = []
    for summary in run_store.list(limit=100, status="running"):
        record = run_store.get(summary.run_id)
        if record is not None:
            records.append(record)
    return records


def build_ask_human_mcp(get_state) -> FastMCP:
    """Build the FastMCP server. `get_state` is a zero-arg callable returning
    (run_store | None, executor | None) — read per call so the server mounted
    at import time sees the state the lifespan (or a test) wired later."""
    mcp = FastMCP(
        "miragen",
        instructions=(
            "miragen's structured-intervention front door. Call ask_human when "
            "you reach a decision only a human can make, then END YOUR TURN — "
            "the run suspends on your question and resumes with the answer."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        # The SDK's DNS-rebinding protection admits only localhost Hosts,
        # which 421s an executor reaching miragen through a Docker service
        # name. This endpoint is same-deployment by design and guarded by
        # MIRAGEN_INTERNAL_TOKEN (the app-level ASGI guard); Host-header
        # checking adds nothing a browser isn't involved in.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    def ask_human(
        question: str,
        kind: str | None = None,
        options: list[dict[str, Any]] | None = None,
        evidence: str | None = None,
        affected_repositories: list[str] | None = None,
        run_id: str | None = None,
    ) -> str:
        """Raise a structured question for a human and suspend this run on it.

        Args:
            question: The decision you need made (required).
            kind: e.g. 'architecture-decision', 'confirmation'.
            options: Choices, each {"id": ..., "label": ..., "description": ...}.
            evidence: Pointers a human needs to decide (paths, findings).
            affected_repositories: Repository names the decision affects.
            run_id: Only needed when several runs are active.

        After a successful call, end your turn. Do not keep working past an
        unanswered question.
        """
        run_store, executor = get_state()
        if run_store is None or executor is None:
            raise AskHumanError(
                "this miragen instance has no executor run state — ask_human "
                "applies to executor-tier agents only"
            )
        return record_ask_human(
            run_store=run_store,
            executor=executor,
            question=question,
            kind=kind,
            options=options,
            evidence=evidence,
            affected_repositories=affected_repositories,
            run_id=run_id,
        )

    return mcp
