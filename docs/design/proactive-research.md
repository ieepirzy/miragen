# Proactive research: a skill always, a librarian maybe

Status: **Foundations only**, 2026-09-23. No code has been written. This doc
records where the capability would attach and what has to be true before each
piece gets built. Tracking issues: research skill #136, librarian
experiment #137, optional integration #138.

## Goal

Agents should notice when outside evidence would materially improve the work
in front of them, and go and get it without being asked. The evidence can be
literature, industry practice, documentation, source code, or engineering
reports. The capability informs the agent's decisions. It never makes them.

There are two parts, with different lifetimes:

| Part | What it is | Availability |
| --- | --- | --- |
| **Research skill** | Operating principles for finding and weighing evidence, including how to work with scientific papers | **Always on.** It ships with the base plugin, the same way `memory-bridge` does. |
| **Research librarian** | A small model that reads the session's recent context and points out consequential information gaps | **Optional, off by default, toggleable.** It is temporary scaffolding in the sense of #127, so removing it must not change anything else. |

## What exists today (main @ 56d50f4)

**Skills.** Harness skills live in `plugins/miragen-memory/skills/<name>/SKILL.md`.
Claude Code loads them natively as plugin skills. For Codex and Grok Build,
miragend copies them into `$CODEX_HOME/skills` and `$GROK_HOME/skills`
(`miragen_hook/harness_setup.py:269`, `ensure_skills`). It marks each copy with
`.miragen-managed`, and it skips a same-named skill that isn't ours.
`scripts/sync_plugin_adapter.sh` keeps the second copy in `miragen_hook/skills/`,
and `tests/test_plugin_bundle.py` pins the two copies together. **A new skill
directory reaches all three harnesses with no code change.** That settles the
"base agent" half of the requirement for harness sessions.

**Profile agents and the executor tier have no skill loader.** PydanticAI
profile agents get `spec.instructions` plus registry capabilities
(`miragen/load.py:33`). Neither the pinned pydantic-ai 1.106 nor
`miragen/executor/*` loads skills. Search is already available to them:
`WebSearch` and `WebFetch` capabilities, and `tools.web_search` in EDF
(`miragen/edf.py:567`).

**What the session plane knows about a session.** It keeps the last 20 user
prompts, clipped to 300 characters, and the last 10 final assistant messages
from `Stop`, clipped to 600 (`miragen/daemon/sessions/models.py:31`). It also
records `transcript_path`, but it doesn't read it, and a hosted bridge can't
read it anyway because the file is on the client machine. It sees no tool
calls, no tool results, and no intermediate reasoning.

**A model call with a context lane already exists: prompt recall.** On
`UserPromptSubmit`, the plane runs a bounded selector (`plane.py:830`,
`memory/selection.py`) and injects an attributed section. Its contract is the
right precedent for a librarian:
- zero or more results, and "nothing" is the common answer;
- on failure it injects **nothing**;
- the model never authors the text that gets injected;
- it has its own model setting (`recall.model`), separate from the main agent's.

**Delivery is being reworked in two open, unmerged PRs:**
- **#126** delivers recall asynchronously. A late result arrives as
  `additionalContext` on the next `PostToolUse` or `Stop`.
- **#128** adds a Stop *block* that nudges the agent to save memory. It has
  its own `nudge.enabled` config switch, per-session caps, and `/health`
  counters.

**Evidence that a skill alone may not trigger.** The memory-effectiveness audit
(#110, `docs/design/memory-effectiveness.md` §1) found that the
`memory-bridge` skill was loaded **0 times in 36 Claude Code sessions**.

## Integration point

**The research skill** is a new skill directory next to `memory-bridge`. It
needs nothing else: no config, no code, no dependency. It is available to every
harness session whether or not the librarian exists.

**The librarian** would be a sibling of prompt recall inside the session plane.
It would read the plane's bounded session buffer, make one small-model call,
and hand back zero or more suggestions through #126's non-blocking
`additionalContext` path.

The librarian must **not** reuse #128's Stop-block pattern. A Stop block takes
the turn away from the agent and demands a specific answer. That is acceptable
for a memory save. It is not acceptable for "you might want to look this up",
because the agent has to stay free to ignore the suggestion.

Toggle: a config section on the session plane, `enabled: false` by default, in
the same style as `SessionsNudge.enabled` and `StorePolicy.enabled`. When it is
off, no model call is made, nothing is injected, and no state is kept.

**Why there is no `research` optional-dependency group yet.** The librarian
would use the same model plumbing the selector already uses: core
`pydantic-ai`, plus the `claude-code:<model>` runner in #117. An empty
extra would be a placeholder. Create the `research` extra when the librarian
first needs a package that core doesn't have, for example a scholarly-API
client.

## Minimal librarian output

Each suggestion contains:
- **gap:** what isn't known;
- **why it matters now:** the decision it bears on, which may be an implicit one;
- **where to look** (optional): the kind of source, not a conclusion.

There are no scores, no answers, and no plans. An empty list is the expected
result on most turns.

## Roadmap

1. **Foundations.** This doc and the issues. *Done.*
2. **Research skill (#136).** Independently useful; ship it.
3. **Measure the skill in real sessions.** Use the transcript method from the
   #110 audit: how often it loads, and how often agents research when it would
   have mattered. If it doesn't trigger, fix its `description` first.
   **Gate:** steps 4–5 happen only if the skill, once it triggers reliably,
   still leaves consequential gaps unnoticed.
4. **Librarian experiment (#137).** Offline, on a handful of real
   scenarios. It answers one question: can a small model spot gaps worth
   researching from what the plane actually sees? *Contingent on step 3.*
5. **Optional integration (#138).** Wire it into the plane behind the
   toggle, then validate it against real workflows. *Contingent on step 4
   succeeding and on #126 being merged.*

It is a valid outcome to stop after step 3.

## Open questions

**These must be answered before the librarian is built.**

1. **Is the plane's context enough?** Implicit decisions mostly happen in the
   assistant's reasoning and tool calls, and the plane sees neither. The
   choice is between three sources:
   - the prompt and final-message buffer;
   - a hook payload enriched with new fields;
   - the transcript, which is local sessions only.

   The experiment (#137) should test the first one before anything is
   added.
2. **Does "base agent" include profile agents and the executor tier?** The
   default answer is no. Harness sessions get the skill. Profile agents, which
   have no skill loader, would get it only if someone adds a skill loader or
   pastes the skill into `spec.instructions`. Neither is done here.
3. **Where does the toggle live?** The options are daemon-wide in
   `sessions.yaml`, per project, per session, or some combination. #128's
   nudge is daemon-wide, so that is the consistent starting point.

**These are left to the implementer.**

- The skill's name. Avoid generic names, because `ensure_skills` skips a
  same-named skill that isn't ours.
- The skill's wording.
- The plugin version bump.
- Rate limits and caps for the librarian. #128's `max_per_session` pattern is
  a reasonable model.
- The `/health` counters.
- Which small model to use. The selector's `claude-code:haiku` is the obvious
  first candidate.

## Constraints

- No orchestration subsystem, background service, scholarly database, or
  multi-agent pipeline.
- No confidence scores, research budgets, or evaluation platform.
- The main agent owns its research strategy, how it interprets what it finds,
  and every decision.
- Removing the librarian means deleting its config section and its lane. The
  skill and the plane stay as they are.
