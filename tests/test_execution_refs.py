"""MiraRun execution references (execution-refs/v1): a launch's or turn's
``mirarun`` ids reach the agent process as MIRARUN_* and nothing else can
set them."""

from __future__ import annotations

from pathlib import Path

import pytest

from miragen.models import (
    HarnessSpec,
    MiraRunRefs,
    RunProvenance,
    mirarun_env,
    passthrough_refused,
)
from tests.test_executor_adapters import _claude_executor, _spawn_executor

REFS = {"agent_session_id": "4f0c2a7e-1d2b-4c5e-9f00-112233445566", "lane_id": "lane-1",
        "lane_role": "engineer", "lane_slot": "engineer-1"}
ENV = {"MIRARUN_AGENT_SESSION_ID": REFS["agent_session_id"], "MIRARUN_LANE_ID": "lane-1",
       "MIRARUN_LANE_ROLE": "engineer", "MIRARUN_LANE_SLOT": "engineer-1"}

# The harness tests live with each harness's fixtures (test_claude_code_engineer,
# test_codex_harness, test_grok_harness); the endpoint tests in
# test_agent_launch_into_instance.


# ── the contract ───────────────────────────────────────────────────────────


def test_declared_keys_are_checked_and_others_ignored():
    refs = MiraRunRefs.model_validate({"run_id": "run-1f2e", "future_key": "whatever"})
    assert refs.present() == {"run_id": "run-1f2e"}


@pytest.mark.parametrize("bad", ["", "has space", "semi;colon", "x" * 129, "$(id)", "-leading",
                                 42, ["run-1"], {"id": "run-1"}])
def test_a_malformed_ref_is_dropped_never_refused(bad, caplog):
    refs = MiraRunRefs.model_validate({"run_id": bad, "lane_id": "lane-1"})
    assert refs.present() == {"lane_id": "lane-1"}
    assert "run_id" in caplog.text and "dropped" in caplog.text
    if isinstance(bad, str) and bad:
        assert bad not in caplog.text  # the value itself is never logged


def test_env_maps_only_valid_declared_refs():
    assert mirarun_env(MiraRunRefs.model_validate(REFS)) == ENV
    assert mirarun_env({"run_id": "r-1"}) == {"MIRARUN_RUN_ID": "r-1"}
    # A persisted dict is re-checked: a tampered value is dropped, not passed on.
    assert mirarun_env({"run_id": "a b", "lane_id": "ok", "other": "x"}) == {
        "MIRARUN_LANE_ID": "ok"}
    assert mirarun_env(None) == {} and mirarun_env("run-1") == {}


@pytest.mark.parametrize("name", ["MIRARUN_RUN_ID", "MIRARUN_LANE_ID", "mirarun_lane_slot",
                                  "MIRARUN_ANYTHING_NEW"])
def test_a_profile_can_never_pass_mirarun_names_through(name):
    assert passthrough_refused(name)
    with pytest.raises(ValueError, match="may not be passed"):
        HarnessSpec(posture="engineer", env_passthrough=[name])


def test_provenance_keeps_the_refs_across_a_round_trip():
    p = RunProvenance().model_copy(update={"mirarun": {"run_id": "r-1"}})
    assert RunProvenance.model_validate_json(p.model_dump_json()).mirarun == {"run_id": "r-1"}


# ── executor backends ──────────────────────────────────────────────────────


async def test_the_claude_code_executor_sets_refs_per_query(tmp_path):
    captured = []
    _, executor = _claude_executor(tmp_path, captured=captured)
    await executor.run_job("go", "cc-refs", execution_env={"MIRARUN_RUN_ID": "run-1"})
    await executor.run_job("go", "cc-none")
    assert captured[0][1]["env"] == {"MIRARUN_RUN_ID": "run-1"}
    assert "env" not in captured[1][1]


async def test_the_spawn_executor_sets_refs_in_the_child(tmp_path):
    _, executor = _spawn_executor(
        tmp_path, ["/bin/sh", "-c", 'cat >/dev/null; echo "id=$MIRARUN_RUN_ID" > seen.txt'])
    result = await executor.run_job("x", "sp-refs", execution_env={"MIRARUN_RUN_ID": "run-7"})
    assert result.status == "succeeded"
    assert "id=run-7" in Path(result.diff_path).read_text()
