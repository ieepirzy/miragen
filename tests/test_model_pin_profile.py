"""A full Claude model id after `claude-code:` reaches the CLI unchanged, in
both postures: the example lane pinned to Claude Opus 4.8
(examples/profiles/opus-4-8-engineer.yaml) launches `claude --model
claude-opus-4-8`, never the bare `opus` alias (which resolves to the newest
Opus)."""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")

from miragen.harness.claude_code import ClaudeCodeHarness, ClaudeCodeSettings  # noqa: E402
from miragen.harness.gateway import ToolGateway  # noqa: E402
from miragen.load import load_profile  # noqa: E402
from tests import claude_code_snapshot as snap  # noqa: E402
from tests.test_claude_code_engineer import hermetic  # noqa: E402,F401

EXAMPLE = Path(__file__).parent.parent / "examples" / "profiles" / "opus-4-8-engineer.yaml"
PINNED = "claude-opus-4-8"


def _launch(prof, root: Path) -> dict:
    settings = ClaudeCodeSettings(claude_home=root / "home", workdirs=root / "work",
                                  gateway_url="http://127.0.0.1:9/mcp/gateway/",
                                  cli_path="/usr/bin/env")
    h = ClaudeCodeHarness(prof, ToolGateway(
        prof, native_capabilities=ClaudeCodeHarness.native_capabilities), settings)
    o = h._options("chat", root / "work" / "chat", resume=None, new_id="x", stderr=deque())
    return snap.render(h, o, root)


def _model_arg(argv: list[str]) -> str:
    i = argv.index("--model")
    return argv[i + 1]


def test_the_example_profile_pins_opus_4_8_in_engineer_posture(tmp_path, hermetic):  # noqa: F811
    prof = load_profile(EXAMPLE)
    assert prof.spec.model == f"claude-code:{PINNED}"
    view = _launch(prof, tmp_path)
    assert view["options"]["model"] == PINNED
    assert _model_arg(view["argv"]) == PINNED
    # Engineer posture: Claude Code's own tools, so the Agent tool is there
    # and subagents run in this same process under this --model.
    assert view["options"]["tools"] == {"type": "preset", "preset": "claude_code"}
    assert view["options"]["permission_mode"] == "bypassPermissions"
    # No fallback model: an overloaded Opus 4.8 fails the turn rather than
    # silently switching models.
    assert view["options"]["fallback_model"] is None
    assert "--fallback-model" not in view["argv"]


def test_a_full_model_id_passes_through_the_locked_posture_too(tmp_path, hermetic):  # noqa: F811
    prof = snap.profile({}, model=f"claude-code:{PINNED}")
    view = _launch(prof, tmp_path)
    assert view["options"]["model"] == PINNED
    assert _model_arg(view["argv"]) == PINNED


# ── spec.harness.subagent_model ─────────────────────────────────────────────

SUBAGENT_VARS = ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE")


def _engineer(model: str, **harness):
    return snap.profile({}, model=model, harness={"posture": "engineer", **harness})


@pytest.mark.parametrize("prof", [
    _engineer("claude-code:sonnet"),                                   # an existing lane
    _engineer("claude-code:sonnet", subagent_model="inherit"),
    _engineer(f"claude-code:{PINNED}"),
], ids=["sonnet-lane-unset", "sonnet-explicit-inherit", "pinned-model-inherit"])
def test_inherit_sets_no_subagent_variables(tmp_path, hermetic, prof):  # noqa: F811
    view = _launch(prof, tmp_path)
    for name in SUBAGENT_VARS:
        assert name not in view["env"]
        assert name not in view["env"]["MIRAGEN_ENV_KEEP"].split()


def test_pin_forces_every_subagent_onto_the_session_model(tmp_path, hermetic):  # noqa: F811
    view = _launch(_engineer(f"claude-code:{PINNED}", subagent_model="pin"), tmp_path)
    assert view["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == PINNED
    # Without FORCE an Agent call's own `model` wins (CLI 2.1.299, observed).
    assert view["env"]["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
    # The exec wrapper's allowlist keeps them, so the claude child sees them.
    keep = view["env"]["MIRAGEN_ENV_KEEP"].split()
    assert set(SUBAGENT_VARS) <= set(keep)


def test_the_example_profile_pins_its_subagents(tmp_path, hermetic):  # noqa: F811
    prof = load_profile(EXAMPLE)
    assert prof.spec.harness.subagent_model == "pin"
    assert _launch(prof, tmp_path)["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == PINNED


@pytest.mark.parametrize("model,harness,match", [
    (f"claude-code:{PINNED}", {"subagent_model": "pin"}, "only apply to posture"),
    ("claude-code:", {"posture": "engineer", "subagent_model": "pin"}, "names none"),
    (f"claude-code:{PINNED}", {"posture": "engineer", "subagent_model": "pin",
                               "subagent_depth": 0}, "dead config"),
    (f"claude-code:{PINNED}", {"posture": "engineer", "subagent_model": "always"},
     "inherit"),
])
def test_pin_is_refused_where_it_would_be_dead_config(model, harness, match):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=match):
        snap.profile({}, model=model, harness=harness)


# ── serialization and the profile contract ─────────────────────────────────

GOLDEN_DUMPS = Path(__file__).parent / "fixtures" / "harness_profile_dumps_v4.json"


def _golden() -> dict:
    import json

    return json.loads(GOLDEN_DUMPS.read_text())


def _doc(golden: dict, harness: dict) -> dict:
    base = golden["base"]
    return {**base, "spec": {**base["spec"], "harness": harness}}


@pytest.mark.parametrize("explicit", [False, True], ids=["unset", "explicit-inherit"])
@pytest.mark.parametrize("name", ["locked_block", "engineer_minimal", "engineer_full"])
def test_an_inherit_profile_dumps_exactly_as_before_this_field(name, explicit):
    """Golden captured on the pre-PR code (4342851). A dump that carried
    `subagent_model: inherit` would be refused by a v4 image (extra=forbid)."""
    import json

    from miragen.models import AgentProfile

    golden = _golden()
    harness = dict(golden["profiles"][name])
    if explicit and harness.get("posture") == "engineer":
        harness["subagent_model"] = "inherit"
    prof = AgentProfile.model_validate(_doc(golden, harness))
    python = json.loads(json.dumps(prof.model_dump(by_alias=True), default=str))
    assert python == golden["dumps"][name]["python"]
    assert json.loads(prof.model_dump_json(by_alias=True)) == golden["dumps"][name]["json"]
    again = AgentProfile.model_validate(prof.model_dump(by_alias=True))
    assert again.required_contract() == 4


def test_pin_survives_the_dump_and_infers_contract_5():
    from miragen.models import AgentProfile
    from miragen.profile_contract import SUPPORTED_PROFILE_CONTRACTS

    golden = _golden()
    prof = AgentProfile.model_validate(
        _doc(golden, {"posture": "engineer", "subagent_model": "pin"}))
    assert prof.inferred_contract() == 5 and prof.required_contract() == 5
    dumped = prof.model_dump(mode="json", by_alias=True)
    assert dumped["spec"]["harness"]["subagent_model"] == "pin"
    assert AgentProfile.model_validate(dumped).required_contract() == 5
    assert 5 in SUPPORTED_PROFILE_CONTRACTS
    # Declaring v4 understates a pin.
    with pytest.raises(ValueError, match="v5"):
        AgentProfile.model_validate({**_doc(golden, {"posture": "engineer",
                                                     "subagent_model": "pin"}),
                                     "apiVersion": "miragen/v4"})


def test_the_example_lane_requires_contract_5():
    assert load_profile(EXAMPLE).required_contract() == 5


@pytest.mark.parametrize("fixture", ["tests.test_edf_model_tier:model_tier_edf",
                                     "tests.test_edf:minimal_edf"])
def test_edf_resolved_profile_and_its_snapshot_never_carry_the_field(fixture):
    """edf.py's resolved_profile (profile.model_dump(mode="json")) and the
    /profiles/resolve payload (daemon/contract.py: resolved.model_dump(mode=
    "json")) are the two re-submission round trips."""
    import importlib
    import json

    from miragen.edf import resolve_edf

    module, func = fixture.split(":")
    resolved = resolve_edf(getattr(importlib.import_module(module), func)())
    assert "subagent_model" not in json.dumps(resolved.resolved_profile)
    assert "subagent_model" not in json.dumps(resolved.model_dump(mode="json"))


def test_the_resolved_profile_dump_path_drops_inherit_from_a_harness_profile():
    """The same call edf.py makes for resolved_profile, on a harness profile
    (an EDF cannot express spec.harness today)."""
    from miragen.models import AgentProfile

    golden = _golden()
    for harness in ({"posture": "engineer"}, {"posture": "engineer", "subagent_model": "inherit"}):
        dumped = AgentProfile.model_validate(_doc(golden, harness)).model_dump(mode="json")
        assert "subagent_model" not in dumped["spec"]["harness"]
        assert AgentProfile.model_validate(dumped).required_contract() == 4
