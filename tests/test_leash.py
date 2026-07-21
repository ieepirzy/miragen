"""Host-imposed action leash (issue #38 Phase 2) — the config model, the pure
policy engine, the in-process gate → intervention escalation, and the per-
backend approval-request mapping. The real SDK approval delivery is stubbed;
the handler LOGIC and the escalation path are exercised end to end.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from miragen.executor import CodexExecutor
from miragen.executor.claude_code import _gate_operation as cc_gate_operation
from miragen.executor.codex import CodexExecutor as _CX
from miragen.executor.leash import GateOperation, LeashPolicy
from miragen.models import AgentProfile, LeashSpec

from tests.test_executor import default_events


def _leash_profile(leash, mode="autonomous", executor_kind="codex"):
    triggers = [{"type": "cron", "schedule": "0 * * * *"}] if mode == "autonomous" else [{"type": "http"}]
    return AgentProfile.model_validate({
        "name": "leashed",
        "mode": mode,
        "triggers": triggers,
        "executor": {"executor": executor_kind, "instructions": "work", "leash": leash},
    })


def _codex(profile, tmp_path):
    profile.executor.workspace_root = str(tmp_path / "workspaces")
    profile.executor.codex_home = str(tmp_path / "codex-home")
    return CodexExecutor(profile, runs_root=tmp_path / "runs")


# ── Config model ─────────────────────────────────────────────────────────────


def test_gated_classes_default_by_mode():
    assert LeashSpec().gated_classes("autonomous") == {"network"}       # long leash
    assert LeashSpec().gated_classes("hybrid") == {"write", "command", "network"}  # short leash
    assert LeashSpec(gate=["write"]).gated_classes("autonomous") == {"write"}  # explicit wins


def test_deny_commands_regex_validated():
    with pytest.raises(ValidationError, match="invalid deny_commands regex"):
        LeashSpec(deny_commands=["([unclosed"])
    assert LeashSpec(deny_commands=[r"rm\s+-rf"]).deny_commands == [r"rm\s+-rf"]


def test_leash_field_on_executor_profile():
    p = _leash_profile({"gate": ["command"], "deny_commands": [r"curl.*\|\s*sh"]})
    assert p.executor.leash.gate == ["command"]


# ── Pure policy engine ───────────────────────────────────────────────────────


def test_policy_blocks_gated_class_allows_others():
    pol = LeashPolicy(LeashSpec(gate=["network"]), mode="autonomous")
    assert pol.evaluate(GateOperation("network")).allow is False
    assert pol.evaluate(GateOperation("write")).allow is True
    assert pol.evaluate(GateOperation("command", command="ls")).allow is True


def test_deny_command_backstop_beats_class():
    # gate nothing by class; deny only rm -rf. A non-gated 'command' with a
    # matching pattern is still blocked.
    pol = LeashPolicy(LeashSpec(gate=[], deny_commands=[r"rm\s+-rf"]), mode="autonomous")
    blocked = pol.evaluate(GateOperation("command", command="rm -rf /data"))
    assert blocked.allow is False and "deny pattern" in blocked.reason
    assert pol.evaluate(GateOperation("command", command="ls -la")).allow is True


# ── base tier: gate_decide + escalation ──────────────────────────────────────


def _plain_profile():
    return AgentProfile.model_validate({
        "name": "plain", "mode": "interactive", "triggers": [{"type": "http"}],
        "executor": {"executor": "codex", "instructions": "work"},
    })


def test_gate_decide_allows_all_without_leash(tmp_path):
    executor = _codex(_plain_profile(), tmp_path)
    assert executor.leash_enabled is False
    assert executor.gate_decide(GateOperation("command", command="rm -rf /")).allow is True
    assert executor._drain_gate_blocks() == []  # nothing recorded


def _gated_session(executor, ops):
    """A session_factory that fires the gate for `ops` (as the real approval
    handler would, mid-turn), then completes normally."""
    def factory(prompt, *, thread_id=None, first_turn=True, options=None):
        async def gen():
            for op in ops:
                executor.gate_decide(op)
            for e in default_events():
                yield e
        return gen()
    return factory


async def test_leash_block_suspends_with_host_gate_intervention(tmp_path):
    executor = _codex(_leash_profile({"gate": [], "deny_commands": [r"rm\s+-rf"]}), tmp_path)
    assert executor.leash_enabled is True
    executor._session_factory = _gated_session(
        executor, [GateOperation("command", command="rm -rf /data", summary="rm -rf /data")]
    )
    result = await executor.run_job("go", "leash-1")

    assert result.status == "suspended" and result.exit_reason == "intervention"
    assert result.intervention["kind"] == "host-gate"
    assert "rm -rf /data" in result.intervention["evidence"]
    assert result.diff_path is None  # blocked action → review, no harvest

    events = executor.read_events("leash-1", limit=1000)
    (req,) = [e for e in events if e["type"] == "intervention.requested"]
    assert req["source"] == "host-gate"
    assert req["intervention_id"] == result.intervention["intervention_id"]


async def test_leash_allows_clean_run_through(tmp_path):
    executor = _codex(_leash_profile({"gate": [], "deny_commands": [r"rm\s+-rf"]}), tmp_path)
    executor._session_factory = _gated_session(
        executor, [GateOperation("command", command="pytest -q")]  # not gated
    )
    result = await executor.run_job("go", "leash-clean")
    assert result.status == "succeeded"
    assert result.intervention is None


async def test_gate_blocks_drained_per_turn(tmp_path):
    """Blocks don't leak across turns: a clean resume after a blocked turn
    doesn't re-raise the prior block."""
    executor = _codex(_leash_profile({"gate": ["command"]}), tmp_path)
    executor._session_factory = _gated_session(executor, [GateOperation("command", command="x")])
    first = await executor.run_job("go", "leash-drain")
    assert first.status == "suspended"

    executor._session_factory = _gated_session(executor, [])  # clean resume
    second = await executor.run_job("go", "leash-drain", thread_id=first.thread_id, first_turn=False)
    assert second.status == "succeeded"


# ── Backend approval-request mapping ─────────────────────────────────────────


def test_codex_gate_operation_mapping():
    assert _CX._gate_operation("item/fileChange/requestApproval", {}).op_class == "write"
    op = _CX._gate_operation("item/commandExecution/requestApproval", {"command": "ls -la"})
    assert op.op_class == "command" and op.command == "ls -la"
    assert _CX._gate_operation("something/else", {}) is None


def test_codex_approval_decision(tmp_path):
    executor = _codex(_leash_profile({"gate": [], "deny_commands": [r"rm\s+-rf"]}), tmp_path)
    accept = executor._approval_decision("item/commandExecution/requestApproval", {"command": "ls"})
    deny = executor._approval_decision("item/commandExecution/requestApproval", {"command": "rm -rf /"})
    assert accept == {"decision": "accept"}
    assert deny == {"decision": "denied"}
    # unclassifiable request → fail-open (never stall the run)
    assert executor._approval_decision("mystery/request", {}) == {"decision": "accept"}
    # the two blocked ops are now queued for escalation
    assert len(executor._drain_gate_blocks()) == 1


def test_claude_code_gate_operation_mapping():
    assert cc_gate_operation("Bash", {"command": "ls"}).op_class == "command"
    assert cc_gate_operation("Write", {"file_path": "a.py"}).op_class == "write"
    assert cc_gate_operation("WebFetch", {"url": "http://x"}).op_class == "network"
    assert cc_gate_operation("Read", {}) is None
