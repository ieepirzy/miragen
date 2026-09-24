# Base-tier harnesses

Status: accepted direction (Ilari, 2026-09-24). PR A (this seam) is
implemented; the tool gateway and the Grok Build harness follow.

## Why

The base tier (`spec:` profiles) was PydanticAI-only. PydanticAI was never
meant to be the only harness. A self-contained agent CLI such as Grok Build
(on its subscription) is a harness the same way Claude Code is, even when it
drives a single model family. Later candidates include Hermes.

The executor tier is a different thing. It runs one-off worker jobs: a
workspace, diff harvest, resumable threads, interventions. It is not the
home for a persistent conversational agent.

## Decisions

1. **Selection by model prefix.** `spec.model: grok-build:<model>` picks the
   Grok Build harness. This mirrors the `claude-code:<model>` selector
   convention. Any other string stays a pydantic-ai model string.
   Consequences:
   - A profile that relies on `spec.model` as a fallback PydanticAI model
     (the memory recall selector, the extraction worker) must name
     `memory.recall.model` / `memory.extraction.model` explicitly.
2. **The seam.** The app tier keeps these unchanged across harnesses:
   - instances, admission and run records;
   - the memory boundary;
   - telemetry run spans;
   - triggers and schedules.

   A harness only runs the turn: `run(turn)`, `stream(turn)` and `aclose()`,
   defined in `miragen/harness/base.py`. PydanticAI is the default harness,
   and its behaviour is unchanged.
3. **Tools through a miragen tool gateway.** A foreign harness can't call
   Python tools or use PydanticAI's approval hooks, so miragen serves the
   agent's whole tool set as one MCP endpoint on the agent container:
   - proxied `spec.capabilities` MCP servers, with per-run credentials
     injected by miragen;
   - runtime tools (memory, scheduling, speak, ask-human);
   - `@register` local Python tools.

   `approval_required` enforcement and tool-call records happen in the
   gateway, which fails closed. The host owns every tool call, as in the
   PydanticAI tier.
4. **Grok's session is the conversation's source of truth.** Each instance
   maps to a native Grok session, and Grok compacts it itself.
5. **Transport: ACP, one long-lived `grok agent` process per instance.** The
   agent container is persistent. Spawning the CLI per turn is out. The ACP
   client bugs (miragen#145) are fixed as part of this work. Idle processes
   are evicted and resumed with `session/load`, re-sending `mcpServers` and
   `cwd`.
6. **The memory packet rides in the prompt for Grok (option a).** Grok takes
   system rules only at `session/new`, so the per-turn packet is prepended to
   the turn's prompt and persists in Grok's transcript. Grok's compaction
   handles it. This is a deliberate exception to §17.3's "transient packet"
   rule, made because relying on the agent to pull memory itself is brittle.
   If a per-turn system channel turns up in probing, prefer it.

## Open

- **Instruction changes.** `spec.instructions` goes in once, at
  `session/new`. When it changes (the profile is edited and the container
  restarts), existing sessions keep the old text. The proposal is to store an
  instructions hash per instance, and on a mismatch fork the session so
  history is kept and the fork carries the new instructions. This is pending
  Ilari's decision.
- **The tool boundary for Grok.** Built-ins are removed through the agent
  profile (`tools: search_tool, use_tool`), a host-side `pre_tool_use` client
  hook denies anything outside the gateway, and the gateway fails closed.
  Nothing is claimed until a live probe with a subscription login shows it.
