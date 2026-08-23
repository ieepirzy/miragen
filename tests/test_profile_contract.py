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
