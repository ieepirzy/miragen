"""The versioned contract between an agent profile and the runtime (#75).

Motivating failure: an executor-tier profile — valid against the daemon's
own miragen — was spawned onto a host whose cached agent image predated the
executor tier. Nothing checked that the profile's contract and the runtime
that would actually execute it agree, so the mismatch surfaced as a pydantic
traceback in a crash-looping container instead of a refusal at create.

Three parties meet here:

- **Profiles** declare (or imply) the contract level they need.
  ``AgentProfile.required_contract()`` is the effective requirement: the
  greater of the declared ``apiVersion`` and what the profile's features
  imply — a profile cannot understate what it uses.
- **Runtimes** declare what they support: this module's constants are baked
  into the image as an OCI label (Dockerfile), served on ``/health`` as
  ``profile_contracts``, and printable via ``miragen contract`` — three
  surfaces, one source of truth.
- **miragend** compares the two at create time, against the image it will
  actually spawn — never against its own bundled miragen, which is exactly
  the two-interpreters gap that caused the motivating failure.

Contract levels:

    1  the original model-tier profile (spec, triggers, approvals, tools)
    2  the executor tier (executor block: codex/claude-code/…, leash,
       injected MCP servers, artifact sink)
    3  `agent_type` (internal | external | hybrid), with the external/hybrid
       load-time rules (miragen/agent_type.py). Implied by declaring the
       field at all: a v1/v2 runtime rejects it as an unknown key, so a
       profile carrying it must not be spawned there. A runtime advertising
       3 enforces the rules — this is how a control plane detects it.
    4  `spec.harness` (the Claude Code harness posture: `locked` |
       `engineer`, docs/design/harnesses.md). Implied by declaring the
       block: older runtimes reject the key.
    5  `spec.harness.subagent_model: pin` (CLAUDE_CODE_SUBAGENT_MODEL +
       _FORCE for engineer subagents). Implied by the value `pin` only: the
       default `inherit` is left out of serialized profiles, so a dumped
       profile that never pins still runs on a v4 runtime.
    6  `credentials` (per-profile credential grants miragend honours at
       spawn, docs/credential-grants.md). Implied by declaring the field at
       all, like `agent_type`: an older runtime rejects even an empty list.
       Serialization drops an empty list.
    7  `spec.harness.turn_timeout_s` (a per-agent Claude Code turn timeout
       overriding MIRAGEN_CLAUDE_TURN_TIMEOUT_S). Implied by setting it:
       an unset timeout is left out of serialized profiles.

A new level is added when a profile feature requires runtime support that
older runtimes would reject or — worse — silently ignore.
"""

from __future__ import annotations

import re

# Every contract level this build of the runtime can execute. Extend when a
# new level is introduced; drop old levels only with a major version.
SUPPORTED_PROFILE_CONTRACTS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7)

# The newest contract this build serves — what freshly authored profiles
# may declare.
CURRENT_PROFILE_CONTRACT: int = max(SUPPORTED_PROFILE_CONTRACTS)

# OCI image label carrying the supported levels, space-separated (e.g.
# "1 2 3 4 5 6 7"). The Dockerfile bakes it in; a drift test pins the Dockerfile to
# these constants. An image WITHOUT this label predates contract labeling
# and is treated as supporting contract 1 only — the conservative reading,
# and the correct one for every pre-executor-tier image in the wild.
PROFILE_CONTRACTS_LABEL = "io.miragen.profile-contracts"

API_VERSION_PATTERN = r"^miragen/v([1-9]\d*)$"
_API_VERSION_RE = re.compile(API_VERSION_PATTERN)


def parse_api_version(value: str) -> int:
    """``miragen/vN`` -> N. Raises ValueError on anything else."""
    match = _API_VERSION_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            f"apiVersion {value!r} does not match {API_VERSION_PATTERN} "
            "(expected e.g. 'miragen/v2')"
        )
    return int(match.group(1))


def format_api_version(level: int) -> str:
    return f"miragen/v{level}"


def parse_contracts_label(raw: str | None) -> set[int]:
    """The label value -> supported levels. Absent/empty -> {1}: an image
    that never heard of contract labeling is a pre-executor-tier runtime.
    A malformed token fails loudly — a label that cannot be trusted must
    not silently widen what the image claims to support."""
    if not raw or not raw.strip():
        return {1}
    levels = set()
    for token in raw.split():
        if not token.isdigit() or int(token) < 1:
            raise ValueError(
                f"malformed {PROFILE_CONTRACTS_LABEL} label token {token!r} "
                f"in {raw!r}"
            )
        levels.add(int(token))
    return levels
