# Host-imposed action leash (issue #38 Phase 2)

**Status:** implemented (`miragen/executor/leash.py`, gate wiring in the codex
and claude-code adapters, escalation in `miragen/executor/base.py`)
**Companion:** the agent-initiated intervention channel (Phase G,
`docs/design/structured-interventions.md`). The leash is the *host-initiated*
counterpart.

## 1. Why a host gate

Phase G lets the *agent* raise a question (the `.miragen/intervention.json`
sentinel). That depends on the agent knowing the protocol and choosing to
use it — it costs agent context, and a compacted or unwilling agent won't.
The leash is the opposite: a deterministic, pre-execution gate miragen
applies to consequential actions **regardless of agent judgment**, costing
the agent no context because it lives entirely in miragen. The agent never
learns it exists.

Guiding principle (owner): *a system that must take up any chunk of the
agent's brain while in use is a bad agentic system.*

## 2. The seam, per backend

Both harnesses expose a native pre-execution approval hook — the leash rides
it, so nothing new is injected into the agent:

- **Codex** (`openai-codex`): the App Server calls a client-side
  `approval_handler(method, params) -> {"decision": "accept"|"denied"}` on
  each `item/commandExecution/requestApproval` / `item/fileChange/requestApproval`.
  It runs in the SDK's offload thread (sync). miragen injects it on
  `codex._client._sync._approval_handler`.
- **Claude Code** (`claude-agent-sdk`): the `can_use_tool(tool_name,
  tool_input, ctx)` PreToolUse callback returns allow/deny before the tool
  runs. Async, same event loop.

An adapter normalizes its native request into a backend-agnostic
`GateOperation` (`op_class` ∈ {write, command, network}, plus the command
string) and calls the shared `ExecutorBackend.gate_decide`.

## 3. The policy (`LeashSpec` → `LeashPolicy`)

Opt-in via `executor.leash`; absent = today's behaviour (no gating), so
existing profiles are untouched.

```yaml
executor:
  leash:
    gate: [write, command, network]      # operation classes to gate
    deny_commands: ["rm\\s+-rf", "curl.*\\|\\s*sh"]  # regex backstop
```

- `gate` omitted → **defaults by mode**: autonomous gets a long leash
  (`{network}` only), hybrid/interactive a short one
  (`{write, command, network}` — every escaping action).
- `deny_commands` are regex matched against the command string and gate a
  match **regardless of class** — the open-ended shell-danger residue
  (`rm -rf`, `curl | sh`, piping) that rules can't classify by type. This is
  where an optional LLM classifier would later plug in, running *only* on
  commands the cheap rules can't clear, so autonomous runs don't pay
  classifier tokens on every action.

Evaluation is pure and deterministic (`LeashPolicy.evaluate`): deny-pattern
first, then gated-class membership. `~0` tokens, viable on long autonomous
runs.

## 4. Block → escalate (in-process, no JSON)

The agent channel uses a file because the agent is a separate process. The
host gate is miragen→miragen in **one process** (sync SDK thread → async
loop), so it needs no file and no serialization:

1. On a policy hit, `gate_decide` records the `GateOperation` on a
   lock-guarded list and the adapter returns `denied` — **the action never
   runs** (the prevention guarantee).
2. After the turn, `base._consume_gate_intervention` drains the list and, if
   non-empty, raises **one** Phase-G intervention (`kind: "host-gate"`,
   `source: "host-gate"` on the event) summarizing the blocked operation(s)
   as evidence — reusing the exact suspend/answer/resume contract from Phase
   G. The run suspends for human authorization before it continues.

Blocks are drained per turn, so a clean resume after a blocked turn doesn't
re-raise the prior block.

`accept` is returned instantly for the safe majority, so an unattended run
never stalls — the handler is an active default-allow responder, not a
passive one.

## 5. What's deferred

- **Approval binding (Stage 3).** The gate is stateless: it blocks and
  raises the intervention; the human's answer is guidance the agent gets on
  resume. Making an `approval_ref` authorize *the exact operation* to pass
  once (TOCTOU-bound to command/target/args/expiry) is the Stage-3
  target-adapter work.
- **Mid-turn interrupt.** v1 blocks the action and suspends after the turn;
  interrupting the turn the instant a block occurs (`turn.interrupt()`) is a
  refinement.
- **The command-danger classifier** (Phase 3) for the ambiguous shell
  residue.

## 6. The one live-verified seam

Everything above is unit-tested with the SDKs stubbed — the policy, the
escalation, and the per-backend request→`GateOperation` mapping. The single
piece unit tests can't exercise (no app-server / no Claude runtime) is that
driving Codex with `approval_policy=on-request` + **no** server-side
auto-reviewer routes every approval to miragen's handler (rather than the
app-server auto-deciding). That is marked in `codex.py._install_gate` and is
confirmed by the gated live smoke test.
