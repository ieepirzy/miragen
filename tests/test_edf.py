"""EDF contract (issue #33 Phase A) — strict validation, deterministic
default/preset expansion, canonical serialization + SHA-256, and resolution
into an executable AgentProfile plus binding plans.

The valid/invalid fixtures mirror mirarun's docs/contracts/edf fixtures so
both sides of the contract validate the same corpus.
"""

import copy
import json

import pytest

from miragen.edf import (
    DEFAULT_INSTRUCTIONS,
    EDF_API_VERSION,
    EDFValidationError,
    RESOLVER_VERSION,
    ResolutionContext,
    build_run_snapshot,
    canonical_document,
    canonical_json,
    canonical_sha256,
    parse_duration,
    resolve_edf,
    validate_edf,
)


def default_dev_edf() -> dict:
    """The `default-dev` example from mirarun's implementation plan §6.1 —
    the shared golden fixture."""
    return {
        "apiVersion": "mirarun.io/v1alpha1",
        "kind": "Environment",
        "metadata": {"name": "default-dev"},
        "spec": {
            "executor": {
                "kind": "codex",
                "model": None,
                "reasoningEffort": "medium",
                "timeout": "30m",
                "budget": {"tokensPerRun": 500000},
                "sandbox": {"mode": "workspace-write"},
                "approvals": {"unattended": True},
            },
            "workspace": {
                "repositories": [
                    {
                        "name": "app",
                        "source": {"provider": "github", "connectionRef": "repo_01"},
                        "ref": "refs/heads/main",
                        "mountPath": "app",
                        "writable": True,
                    },
                    {
                        "name": "contracts",
                        "source": {"provider": "github", "connectionRef": "repo_02"},
                        "ref": "refs/tags/v2.1.0",
                        "mountPath": "contracts",
                        "writable": False,
                    },
                ]
            },
            "tools": {
                "preset": "mirarun-default",
                "git": True,
                "github": True,
                "shell": True,
                "webSearch": True,
                "runtimeDiscovery": True,
                "mcpServers": [
                    {
                        "name": "project-context",
                        "transport": "streamable-http",
                        "url": "https://example.internal/mcp",
                        "auth": {"bearerTokenSecretRef": "project-context-token"},
                    }
                ],
            },
            "variables": {"LOG_LEVEL": "info"},
            "secrets": [
                {
                    "name": "github-token",
                    "providerRef": "secret-provider://github/app-installation",
                    "exposeAs": {"environmentVariable": "GITHUB_TOKEN"},
                },
                {
                    "name": "project-context-token",
                    "providerRef": "secret-provider://internal/project-context",
                    "exposeAs": {"environmentVariable": "PROJECT_CONTEXT_TOKEN"},
                },
            ],
            "network": {"outbound": "allowlist", "allowedHosts": ["api.github.com", "github.com"]},
            "lifecycle": {
                "workspaceSetup": [
                    {"id": "install", "run": "./scripts/bootstrap.sh", "workingDirectory": "app", "timeout": "10m"}
                ],
                "beforeRun": [],
                "afterRun": [],
            },
            "targets": [],
        },
    }


def minimal_edf(**executor_overrides) -> dict:
    return {
        "apiVersion": "mirarun.io/v1alpha1",
        "kind": "Environment",
        "metadata": {"name": "minimal"},
        "spec": {
            "executor": {"kind": "codex", **executor_overrides},
            "workspace": {"repositories": []},
        },
    }


def errors_of(exc_info) -> str:
    return json.dumps(exc_info.value.errors)


# ── Duration grammar ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,seconds", [
    ("45s", 45),
    ("30m", 1800),
    ("1h30m", 5400),
    ("2h", 7200),
    ("1h2m3s", 3723),
])
def test_duration_grammar_parses(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "30", "m30", "1m1h", "30 m", "1.5h", "-5m"])
def test_duration_grammar_rejects(value):
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(value)


# ── Validation: valid corpus ──────────────────────────────────────────────────


def test_default_dev_fixture_validates():
    edf = validate_edf(default_dev_edf())
    assert edf.metadata.name == "default-dev"
    assert edf.spec.executor.budget.tokens_per_run == 500000
    assert [r.mount_path for r in edf.spec.workspace.repositories] == ["app", "contracts"]


def test_minimal_edf_expands_deterministic_defaults():
    doc = canonical_document(validate_edf(minimal_edf()))
    executor = doc["spec"]["executor"]
    assert executor["model"] is None
    assert executor["reasoningEffort"] == "medium"
    assert executor["timeout"] == "30m"
    assert executor["budget"] is None
    assert executor["sandbox"] == {"mode": "workspace-write"}
    assert executor["approvals"] == {"unattended": True}
    assert doc["spec"]["network"] == {"outbound": "deny", "allowedHosts": []}
    assert doc["spec"]["tools"]["git"] is False and doc["spec"]["tools"]["mcpServers"] == []
    assert doc["spec"]["lifecycle"] == {"workspaceSetup": [], "beforeRun": [], "afterRun": []}
    assert doc["spec"]["variables"] == {} and doc["spec"]["secrets"] == [] and doc["spec"]["targets"] == []


def test_minimal_and_explicit_equivalent_hash_identically():
    explicit = minimal_edf(
        model=None, reasoningEffort="medium", timeout="30m",
        sandbox={"mode": "workspace-write"}, approvals={"unattended": True},
    )
    assert canonical_sha256(canonical_document(validate_edf(minimal_edf()))) == \
        canonical_sha256(canonical_document(validate_edf(explicit)))


# ── Validation: invalid corpus (mirrors mirarun invalid fixtures) ────────────


def invalidate(mutator):
    doc = default_dev_edf()
    mutator(doc)
    return doc


@pytest.mark.parametrize("mutator,match", [
    # mirarun fixture: duplicate-mount-path.json
    (lambda d: d["spec"]["workspace"]["repositories"][1].__setitem__("mountPath", "app"),
     "duplicate repository mountPath"),
    # mirarun fixture: dotdot-mount-path.json
    (lambda d: d["spec"]["workspace"]["repositories"][0].__setitem__("mountPath", "../escape"),
     "mount path"),
    # mirarun fixture: unknown-field.json
    (lambda d: d.__setitem__("experimentalFeatureFlags", {}), "[Ee]xtra"),
    # mirarun fixture: non-positive-token-budget.json
    (lambda d: d["spec"]["executor"]["budget"].__setitem__("tokensPerRun", 0), "greater than 0"),
])
def test_shared_invalid_fixture_corpus(mutator, match):
    import re
    with pytest.raises(EDFValidationError) as e:
        validate_edf(invalidate(mutator))
    assert re.search(match, errors_of(e))


@pytest.mark.parametrize("mutator,match", [
    (lambda d: d.__setitem__("apiVersion", "mirarun.io/v2"), "unsupported apiVersion"),
    (lambda d: d.__setitem__("kind", "Deployment"), "Environment"),
    (lambda d: d["metadata"].__setitem__("name", "Bad_Name"), "pattern"),
    (lambda d: d["spec"]["workspace"]["repositories"][1].__setitem__("name", "app"),
     "duplicate repository name"),
    (lambda d: d["spec"]["workspace"]["repositories"][1].__setitem__("mountPath", "app/nested"),
     "overlap"),
    (lambda d: d["spec"]["workspace"]["repositories"][0].__setitem__("mountPath", "/abs"),
     "must be relative"),
    (lambda d: d["spec"]["workspace"]["repositories"][0].__setitem__("mountPath", "a//b"),
     "empty segment"),
    (lambda d: d["spec"]["executor"].__setitem__("timeout", "0s"), "must be positive"),
    (lambda d: d["spec"]["executor"].__setitem__("timeout", "soon"), "invalid duration"),
    (lambda d: d["spec"]["executor"].__setitem__("surprise", True), "[Ee]xtra"),
    (lambda d: d["spec"]["network"].__setitem__("outbound", "deny"), "dead config"),
    (lambda d: d["spec"].__setitem__("network", {"outbound": "allowlist"}), "at least one"),
    (lambda d: d["spec"]["secrets"].append(dict(d["spec"]["secrets"][0])), "duplicate secret name"),
    (lambda d: d["spec"]["secrets"][0].__setitem__("providerRef", "not-a-uri"), "pattern"),
    (lambda d: d["spec"]["secrets"][0].__setitem__("exposeAs", {}), "at least one exposure"),
    (lambda d: d["spec"]["tools"]["mcpServers"][0]["auth"].__setitem__(
        "bearerTokenSecretRef", "ghost"), "undeclared secret"),
    (lambda d: d["spec"]["tools"].__setitem__("preset", "unknown-preset"), "unknown tools preset"),
    (lambda d: d["spec"]["workspace"]["repositories"][0]["source"].__setitem__(
        "connectionRef", "https://user:pass@github.com/x.git"), "opaque connection reference"),
    (lambda d: d["spec"]["lifecycle"]["workspaceSetup"].append(
        {"id": "install", "run": "true"}), "duplicate step id"),
])
def test_strict_validation_rejects(mutator, match):
    import re
    with pytest.raises(EDFValidationError) as e:
        validate_edf(invalidate(mutator))
    assert re.search(match, errors_of(e)), errors_of(e)


def test_validation_errors_are_structured():
    with pytest.raises(EDFValidationError) as e:
        validate_edf(invalidate(lambda d: d["spec"]["executor"]["budget"].__setitem__("tokensPerRun", 0)))
    (error,) = e.value.errors
    assert error["loc"] == "spec.executor.budget.tokensPerRun"
    assert "greater than 0" in error["message"]


# ── Canonicalization + hashing ───────────────────────────────────────────────


# Golden vectors: these hashes are stored by control planes (MiraRun) as
# revision identity. If this test breaks, canonicalization changed — that is
# a breaking contract change and needs a new resolver/schema version, not a
# test update.
DEFAULT_DEV_SHA256 = "016aafa26ddc4841f32a1aa330be3d76b170fe432038bb2ff5802acc8d43fee9"
MINIMAL_SHA256 = "c92a3f62ec624107920c2c9b20a5efd1d2493a60ce82f80fdbfd6ff5cae706ee"


def test_golden_hash_vectors():
    assert resolve_edf(default_dev_edf()).sha256 == DEFAULT_DEV_SHA256
    assert resolve_edf(minimal_edf()).sha256 == MINIMAL_SHA256


def test_canonical_hash_is_deterministic_and_key_order_independent():
    doc = default_dev_edf()
    shuffled = json.loads(canonical_json(doc))  # round-trip with sorted keys

    def reversed_keys(obj):
        if isinstance(obj, dict):
            return {k: reversed_keys(obj[k]) for k in reversed(list(obj))}
        if isinstance(obj, list):
            return [reversed_keys(v) for v in obj]
        return obj

    h1 = resolve_edf(doc).sha256
    h2 = resolve_edf(shuffled).sha256
    h3 = resolve_edf(reversed_keys(copy.deepcopy(doc))).sha256
    assert h1 == h2 == h3


def test_canonical_json_form():
    assert canonical_json({"b": 1, "a": [2, 1], "c": "ä"}) == '{"a":[2,1],"b":1,"c":"ä"}'


def test_preset_expansion_is_part_of_the_hash_and_explicit_flags_win():
    with_preset = minimal_edf()
    with_preset["spec"]["tools"] = {"preset": "mirarun-default"}
    expanded = canonical_document(validate_edf(with_preset))["spec"]["tools"]
    assert expanded["git"] is True and expanded["webSearch"] is True

    overridden = minimal_edf()
    overridden["spec"]["tools"] = {"preset": "mirarun-default", "git": False}
    assert canonical_document(validate_edf(overridden))["spec"]["tools"]["git"] is False


# ── Resolution ───────────────────────────────────────────────────────────────


def test_resolve_default_dev_maps_to_executable_profile():
    resolved = resolve_edf(default_dev_edf())
    assert resolved.resolver_version == RESOLVER_VERSION
    assert resolved.api_version == EDF_API_VERSION
    assert resolved.name == "default-dev"
    assert len(resolved.sha256) == 64

    profile = resolved.resolved_profile
    assert profile["name"] == "default-dev"
    executor = profile["executor"]
    assert executor["executor"] == "codex"
    assert executor["instructions"] == DEFAULT_INSTRUCTIONS
    assert executor["turn_timeout_s"] == 1800
    assert executor["sandbox_mode"] == "workspace-write"
    assert executor["approval_policy"] == "never"
    assert executor["network_access"] is True  # outbound: allowlist
    assert executor["web_search"] is True
    assert executor["reasoning_effort"] == "medium"
    assert profile["limits"]["tokens_per_run"] == 500000

    (mcp,) = executor["mcp_servers"]
    assert mcp["name"] == "project-context"
    assert mcp["url"] == "https://example.internal/mcp"
    # secret reference resolves to the env var NAME — never a value
    assert mcp["bearer_token_env"] == "PROJECT_CONTEXT_TOKEN"

    assert [(r.name, r.ref, r.mount_path, r.writable, r.commit) for r in resolved.repository_plan] == [
        ("app", "refs/heads/main", "app", True, None),
        ("contracts", "refs/tags/v2.1.0", "contracts", False, None),
    ]
    assert [(s.name, s.environment_variable) for s in resolved.secret_bindings] == [
        ("github-token", "GITHUB_TOKEN"),
        ("project-context-token", "PROJECT_CONTEXT_TOKEN"),
    ]
    assert resolved.preset_versions == {"mirarun-default": "1"}
    assert resolved.workspace_plan["variables"] == {"LOG_LEVEL": "info"}
    assert any("Phase D" in w for w in resolved.warnings)


def test_resolution_mapping_edges():
    edf = minimal_edf(sandbox={"mode": "full-access"}, approvals={"unattended": False})
    executor = resolve_edf(edf).resolved_profile["executor"]
    assert executor["sandbox_mode"] == "danger-full-access"
    assert executor["approval_policy"] == "on-request"
    assert executor["network_access"] is False  # default network: deny
    assert resolve_edf(edf).resolved_profile.get("limits") is None  # no budget → no limits block


def test_resolution_is_deterministic():
    a = resolve_edf(default_dev_edf())
    b = resolve_edf(default_dev_edf())
    assert a.model_dump() == b.model_dump()


def test_context_instructions_override():
    resolved = resolve_edf(minimal_edf(), context=ResolutionContext(instructions="Be careful."))
    assert resolved.resolved_profile["executor"]["instructions"] == "Be careful."
    # instructions are resolution context, not desired state: the hash is unchanged
    assert resolved.sha256 == resolve_edf(minimal_edf()).sha256


@pytest.mark.parametrize("mutator,match", [
    (lambda d: d["spec"]["executor"].__setitem__("kind", "spawn"), "not expressible"),
    (lambda d: d["spec"]["executor"].__setitem__("kind", "gemini"), "unsupported executor kind"),
    (lambda d: d["spec"]["tools"]["mcpServers"][0].__setitem__("transport", "stdio"),
     "only 'streamable-http'"),
    (lambda d: d["spec"]["tools"]["mcpServers"][0].__setitem__("name", "Bad Name"),
     "must match"),
])
def test_resolution_rejects_unmappable_documents(mutator, match):
    import re
    with pytest.raises(EDFValidationError) as e:
        resolve_edf(invalidate(mutator))
    assert re.search(match, errors_of(e)), errors_of(e)


def test_claude_code_kind_resolves():
    executor = resolve_edf(minimal_edf(kind="claude-code")).resolved_profile["executor"]
    assert executor["executor"] == "claude-code"


# ── Run snapshot ─────────────────────────────────────────────────────────────


def test_run_snapshot_document_shape():
    resolved = resolve_edf(default_dev_edf())
    snapshot = build_run_snapshot(resolved, run_id="abc123", created_at="2026-07-19T00:00:00+00:00")
    assert snapshot["snapshot_schema"] == "miragen/run-snapshot/v1"
    assert snapshot["run_id"] == "abc123"
    assert snapshot["sha256"] == resolved.sha256
    assert snapshot["canonical"] == resolved.canonical
    # the snapshot must round-trip: re-hashing its canonical doc reproduces sha256
    assert canonical_sha256(snapshot["canonical"]) == snapshot["sha256"]
    # secret VALUES can never appear; only reference identity
    dumped = json.dumps(snapshot)
    assert "GITHUB_TOKEN" in dumped and "secret-provider://" in dumped
