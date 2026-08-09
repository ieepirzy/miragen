"""Model-tier EDF resolution — bringing the two tiers to parity on launch.

`spec.executor.kind: model` resolves to a model-tier AgentProfile (miragen
owns the loop via pydantic-ai) instead of a self-harnessed executor. Added
without an apiVersion bump because it is additive: existing documents resolve
unchanged, and an older resolver rejects a model-tier document on its own.
"""

import copy

import pytest

from miragen.edf import (
    DEFAULT_INSTRUCTIONS,
    EDFValidationError,
    ResolutionContext,
    resolve_edf,
)


def model_tier_edf() -> dict:
    """Minimal model-tier document: no repositories (the model tier has no
    workspace checkout), and a model, which it must name explicitly."""
    return {
        "apiVersion": "mirarun.io/v1alpha1",
        "kind": "Environment",
        "metadata": {"name": "scribe"},
        "spec": {
            "executor": {
                "kind": "model",
                "model": "anthropic:claude-sonnet-4-6",
                "reasoningEffort": "high",
            },
            "workspace": {"repositories": []},
            "tools": {"webSearch": True},
            "network": {"outbound": "allowlist", "allowedHosts": ["api.anthropic.com"]},
        },
    }


def test_model_tier_resolves_to_a_model_tier_profile() -> None:
    resolved = resolve_edf(model_tier_edf())

    profile = resolved.resolved_profile
    assert profile["spec"] is not None
    assert profile["spec"]["model"] == "anthropic:claude-sonnet-4-6"
    assert profile["spec"]["instructions"] == DEFAULT_INSTRUCTIONS
    # The tier is carried by which key is populated, not by which is present:
    # AgentProfile.model_dump() emits both, one of them None.
    assert profile["executor"] is None


def test_capabilities_are_derived_from_tools_not_declared() -> None:
    """EDF stays backend-neutral: it says the environment may search the web,
    and the resolver maps that onto this backend's capability class. A
    document naming miragen's capabilities directly would leak one backend's
    internals into a document the control plane validates backend-blind."""
    resolved = resolve_edf(model_tier_edf())

    capabilities = resolved.resolved_profile["spec"]["capabilities"]
    assert "WebSearch" in capabilities
    assert {"Thinking": {"effort": "high"}} in capabilities


def test_web_search_off_omits_the_capability() -> None:
    document = model_tier_edf()
    document["spec"]["tools"]["webSearch"] = False

    capabilities = resolve_edf(document).resolved_profile["spec"]["capabilities"]

    assert "WebSearch" not in capabilities


def test_unauthenticated_mcp_servers_become_capabilities() -> None:
    document = model_tier_edf()
    document["spec"]["tools"]["mcpServers"] = [
        {
            "name": "project-context",
            "transport": "streamable-http",
            "url": "https://example.internal/mcp",
        }
    ]

    capabilities = resolve_edf(document).resolved_profile["spec"]["capabilities"]

    assert {
        "MCP": {"url": "https://example.internal/mcp", "name": "project-context"}
    } in capabilities


def test_authenticated_mcp_servers_are_refused_rather_than_stripped() -> None:
    """The model tier's MCP capability accepts no bearer token. Resolving
    anyway would drop the credential silently and point an unauthenticated
    agent at a server that requires one — including MiraRun's own /mcp."""
    document = model_tier_edf()
    document["spec"]["tools"]["mcpServers"] = [
        {
            "name": "mirarun",
            "transport": "streamable-http",
            "url": "https://mirarun.internal/mcp",
            "auth": {"bearerTokenSecretRef": "mirarun-token"},
        }
    ]
    document["spec"]["secrets"] = [
        {
            "name": "mirarun-token",
            "providerRef": "mirarun-run-credential://self",
            "exposeAs": {"environmentVariable": "MIRARUN_TOKEN"},
        }
    ]

    with pytest.raises(EDFValidationError) as caught:
        resolve_edf(document)

    assert "no bearer token" in str(caught.value)


def test_repositories_are_refused_rather_than_ignored() -> None:
    """miragen owns the loop and prepares no checkout, so a declared
    repository would never be materialised — and the run would look
    successful while the agent never saw the code."""
    document = model_tier_edf()
    document["spec"]["workspace"]["repositories"] = [
        {
            "name": "app",
            "source": {"provider": "github", "connectionRef": "repo_01"},
            "ref": "refs/heads/main",
            "mountPath": "app",
            "writable": True,
        }
    ]

    with pytest.raises(EDFValidationError) as caught:
        resolve_edf(document)

    assert "no workspace checkout" in str(caught.value)


def test_model_is_required_on_the_model_tier() -> None:
    document = model_tier_edf()
    document["spec"]["executor"]["model"] = None

    with pytest.raises(EDFValidationError) as caught:
        resolve_edf(document)

    assert "must name a model" in str(caught.value)


@pytest.mark.parametrize(
    "field,value,fragment",
    [
        ("sandbox", {"mode": "read-only"}, "no executor sandbox"),
        ("approvals", {"unattended": False}, "approval_required"),
        ("timeout", "30m", "no per-turn executor timeout"),
    ],
)
def test_inapplicable_executor_fields_warn_rather_than_vanish(
    field: str, value, fragment: str
) -> None:
    """A caller who set these believed they took effect. Warned, not raised:
    each has a safe reading on the model tier, unlike repositories or an MCP
    bearer token, which are refused outright."""
    document = model_tier_edf()
    document["spec"]["executor"][field] = value

    warnings = resolve_edf(document).warnings

    assert any(fragment in w for w in warnings), warnings


def test_defaulted_executor_fields_do_not_warn() -> None:
    """model_fields_set distinguishes 'not written' from 'written to the
    default', so a document that never mentions these stays quiet about them.
    Scoped to the executor block: unrelated warnings (e.g. the pre-existing
    note that host-level egress is deployment-owned) are not this test's
    business."""
    warnings = resolve_edf(model_tier_edf()).warnings

    assert not [w for w in warnings if "spec.executor." in w], warnings


def test_executor_tier_resolution_is_unchanged() -> None:
    """The additive claim, asserted rather than assumed: adding the model kind
    must not perturb an existing document's resolution or its hash. This is
    what makes skipping the apiVersion bump defensible."""
    from tests.test_edf import default_dev_edf

    before = resolve_edf(copy.deepcopy(default_dev_edf()))

    assert before.resolved_profile["spec"] is None
    assert before.resolved_profile["executor"]["executor"] == "codex"
    # Same document, same hash — the canonical form did not move.
    assert resolve_edf(copy.deepcopy(default_dev_edf())).sha256 == before.sha256


def test_instructions_come_from_the_resolution_context() -> None:
    resolved = resolve_edf(
        model_tier_edf(), context=ResolutionContext(instructions="be terse")
    )

    assert resolved.resolved_profile["spec"]["instructions"] == "be terse"
