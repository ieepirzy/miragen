"""The profile↔runtime contract (#75): declaration, inference, and the
lockstep between the module's constants and the image label.

What these pin: a profile cannot understate what it uses, an unlabeled
image reads as the pre-executor runtime it is, and the Dockerfile's baked
label can never drift from the source of truth without a test failing.
"""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from miragen.models import AgentProfile
from miragen.profile_contract import (
    CURRENT_PROFILE_CONTRACT,
    PROFILE_CONTRACTS_LABEL,
    SUPPORTED_PROFILE_CONTRACTS,
    format_api_version,
    parse_api_version,
    parse_contracts_label,
)

MODEL_TIER = {
    "name": "alpha",
    "mode": "interactive",
    "triggers": [{"type": "http"}],
    "spec": {"model": "test:whatever", "instructions": "be helpful"},
}

EXECUTOR_TIER = {
    "name": "alpha",
    "mode": "interactive",
    "triggers": [{"type": "http"}],
    "executor": {"executor": "claude-code", "instructions": "review"},
}


class TestApiVersionParsing:
    def test_round_trip(self):
        assert parse_api_version("miragen/v2") == 2
        assert format_api_version(2) == "miragen/v2"

    @pytest.mark.parametrize("bad", ["v2", "miragen/2", "miragen/v0", "miragen/vx", ""])
    def test_malformed_rejected(self, bad):
        with pytest.raises(ValueError):
            parse_api_version(bad)


class TestContractsLabel:
    def test_absent_label_is_the_pre_executor_runtime(self):
        # An image that never heard of contract labeling supports exactly
        # the original tier — the conservative reading, and the correct one
        # for every pre-executor image in the wild.
        assert parse_contracts_label(None) == {1}
        assert parse_contracts_label("") == {1}

    def test_present_label_parses(self):
        assert parse_contracts_label("1 2") == {1, 2}

    @pytest.mark.parametrize("bad", ["1 two", "0", "-1", "1,2"])
    def test_malformed_label_fails_loudly(self, bad):
        # A label that cannot be trusted must not silently widen what the
        # image claims to support.
        with pytest.raises(ValueError):
            parse_contracts_label(bad)


class TestProfileRequirement:
    def test_model_tier_requires_contract_1(self):
        assert AgentProfile.model_validate(MODEL_TIER).required_contract() == 1

    def test_executor_tier_requires_contract_2(self):
        assert AgentProfile.model_validate(EXECUTOR_TIER).required_contract() == 2

    def test_declaring_forward_raises_the_requirement(self):
        profile = AgentProfile.model_validate(
            {**MODEL_TIER, "apiVersion": "miragen/v2"}
        )
        assert profile.required_contract() == 2

    def test_understating_is_rejected(self):
        # An executor profile claiming v1 is lying about its requirement;
        # trusting it would recreate the crash-loop this contract exists
        # to prevent.
        with pytest.raises(ValidationError, match="understate"):
            AgentProfile.model_validate({**EXECUTOR_TIER, "apiVersion": "miragen/v1"})

    def test_declaring_agent_type_requires_contract_3(self):
        # Any declared value, internal included: a v1/v2 runtime rejects the
        # unknown key, so the profile must not be spawned there.
        for value in ("internal", "external", "hybrid"):
            profile = AgentProfile.model_validate({**MODEL_TIER, "agent_type": value})
            assert profile.required_contract() == 3

    def test_omitted_agent_type_keeps_contract_1(self):
        profile = AgentProfile.model_validate(MODEL_TIER)
        assert profile.agent_type == "internal"
        assert profile.required_contract() == 1

    def test_agent_type_cannot_understate(self):
        with pytest.raises(ValidationError, match="understate"):
            AgentProfile.model_validate(
                {**MODEL_TIER, "agent_type": "external", "apiVersion": "miragen/v2"}
            )

    def test_malformed_api_version_rejected(self):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate({**MODEL_TIER, "apiVersion": "v2"})

    def test_snake_case_spelling_also_accepted(self):
        profile = AgentProfile.model_validate(
            {**MODEL_TIER, "api_version": "miragen/v1"}
        )
        assert profile.api_version == "miragen/v1"


def test_current_is_the_max_supported():
    assert CURRENT_PROFILE_CONTRACT == max(SUPPORTED_PROFILE_CONTRACTS)


def test_dockerfile_label_matches_source():
    # The image label is one of the runtime's three declaration surfaces;
    # this is the lockstep guard the Dockerfile comment promises.
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
    match = re.search(
        rf'^LABEL {re.escape(PROFILE_CONTRACTS_LABEL)}="([^"]*)"',
        dockerfile,
        re.MULTILINE,
    )
    assert match, f"Dockerfile is missing the {PROFILE_CONTRACTS_LABEL} LABEL"
    assert parse_contracts_label(match.group(1)) == set(SUPPORTED_PROFILE_CONTRACTS)


def test_cli_contract_command_prints_the_same_declaration():
    from click.testing import CliRunner

    from miragen.cli import cli

    result = CliRunner().invoke(cli, ["contract"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "profile_contracts": list(SUPPORTED_PROFILE_CONTRACTS)
    }


class TestAgentTypeSerialization:
    """The default never leaks into a serialized profile: a dumped profile
    that never declared agent_type re-validates to contract 1, so a
    re-submitted profile is not refused on a v1/v2 image."""

    @staticmethod
    def _round_trip(doc, *, json_mode=False):
        profile = AgentProfile.model_validate(doc)
        if json_mode:
            dumped = json.loads(profile.model_dump_json(by_alias=True))
        else:
            dumped = profile.model_dump(by_alias=True)
        return profile, dumped, AgentProfile.model_validate(dumped)

    @pytest.mark.parametrize("json_mode", [False, True])
    def test_undeclared_stays_contract_1(self, json_mode):
        _, dumped, again = self._round_trip(MODEL_TIER, json_mode=json_mode)
        assert "agent_type" not in dumped
        assert again.agent_type == "internal"
        assert again.required_contract() == 1

    @pytest.mark.parametrize("json_mode", [False, True])
    def test_explicit_internal_is_dropped_as_the_default(self, json_mode):
        # As written it requires 3 (miragend spawns the text, and an old
        # runtime rejects the key); dumped, the key is gone, so 1.
        profile, dumped, again = self._round_trip(
            {**MODEL_TIER, "agent_type": "internal"}, json_mode=json_mode
        )
        assert profile.required_contract() == 3
        assert "agent_type" not in dumped
        assert again.required_contract() == 1

    @pytest.mark.parametrize("value", ["external", "hybrid"])
    @pytest.mark.parametrize("json_mode", [False, True])
    def test_public_types_survive_the_dump(self, value, json_mode):
        _, dumped, again = self._round_trip(
            {**MODEL_TIER, "agent_type": value}, json_mode=json_mode
        )
        assert dumped["agent_type"] == value
        assert again.agent_type == value
        assert again.required_contract() == 3

    def test_executor_profile_round_trip_stays_contract_2(self):
        _, dumped, again = self._round_trip(EXECUTOR_TIER)
        assert "agent_type" not in dumped
        assert again.required_contract() == 2

    @pytest.mark.parametrize("fixture, contract", [
        ("tests.test_edf_model_tier:model_tier_edf", 1),
        ("tests.test_edf:minimal_edf", 2),
    ])
    def test_edf_resolved_profile_is_not_bumped_to_3(self, fixture, contract):
        import importlib

        from miragen.edf import resolve_edf

        module, func = fixture.split(":")
        resolved = resolve_edf(getattr(importlib.import_module(module), func)())
        assert "agent_type" not in resolved.resolved_profile
        again = AgentProfile.model_validate(resolved.resolved_profile)
        assert again.agent_type == "internal"
        assert again.required_contract() == contract
        # And the snapshot form, JSON-serialized, stays the same.
        assert "agent_type" not in json.dumps(resolved.model_dump(mode="json"))
