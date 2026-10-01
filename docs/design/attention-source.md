# miragend as an attention source

**Status:** proposal, 2026-10-02. Nothing here is built. It describes what
miragen would do against a contract that now exists on the mirarun side.

## 1. The direction

Attention is pushed, not pulled (Ilari, 2026-09-30, written into
MiraDesign's `002-pm-platform.md`): the system that routes attention
collects requests from the whole fleet and holds the answers. A system with
something only a human can settle pushes it to a configured sink, at least
once, and the answer is pushed back to that system. Nothing waits with a
connection open.

For an agent run by miragen the second half was put this way (2026-10-01):
the control plane sends the outcome back to the relevant process, and if
that is a miragen agent that has stopped, miragen handles the restart.

mirarun's side of this is a contract, not a miragen-specific integration:
`mirarun/docs/contracts/attention-sources/README.md` (mirarun ADR-030). A
*source* files with `POST /api/attention/sources/{name}/requests` on its
own token, keyed by its own id for the request. The decision comes back as
a `POST` to the source's configured answer URL, with the decision id as
`Idempotency-Key`. The source owns getting the answer to whatever asked.

## 2. What miragen has today

- **Executor runs** raise a structured intervention (`ask_human`, or the
  `.miragen/intervention.json` file). The run suspends; the answer arrives
  as `POST /runs/{id}/resume` with the intervention id.
  `docs/design/structured-interventions.md`. A control plane learns of the
  question by polling the run.
- **Model-tier agents** gate tools through `/approvals`, held in memory
  and lost on restart (`docs/design/run-records-and-approvals.md`).
- **A stopped agent** gets nothing. The daemon answers a launch with 409
  `agent_not_running`, proxies run reads and resumes only to running
  agents, and its schedules post to an agent without starting it.
- **External sessions** (the bridge, `docs/design/external-sessions.md`)
  have no way to ask a person anything.
- Issue #163 asks for push instead of poll for run completion and
  approvals.

## 3. Proposed

miragend becomes one source.

**Up.** When an agent's run suspends on an intervention, the daemon learns
of it. How is an implementation choice: the daemon already proxies run
control and reads each running agent's runs, so it can watch for
`intervention.requested`, or the agent app can be given the daemon's
address and tell it. The daemon writes the escalation to a durable outbox on
its state volume and files it with the configured sink:

- `source_ref`: `<agent>:<run id>:<intervention id>`, which is unique and
  is everything needed to route the answer.
- `question`, `rationale`, `options`, `evidence`: the intervention's own
  fields.
- `context`: `{agent, instance, run_id, intervention_id}`.

Configuration is a sink URL and token, both or neither. Unset, nothing
changes: mirarun's projector keeps discovering interventions by polling,
as it does today.

**Down.** The daemon exposes one route for answers, on its own bearer. For
each answer, keyed by the decision id:

1. record it durably, so a restart between receiving and delivering loses
   nothing, and answer `2xx`. From here the sink is done;
2. if the agent is stopped, start it and wait for it to be ready, exactly
   as `POST /agents/{name}/start` does;
3. deliver: `POST /runs/{run id}/resume` with
   `answer{intervention_id, decision, text, approval_ref}`;
4. retry delivery with backoff until the agent takes it or refuses it for
   good (409: the run was resumed some other way, or is gone).

Step 2 is the restart the direction names. It lives in the daemon because
only the daemon can start an agent, and because the alternative is every
control plane learning to wake agents before it talks to them.

## 4. What this changes for mirarun

Today mirarun creates an attention request when its projector sees
`intervention.requested`, and delivers the decision itself by resuming the
run. With miragend as a source there would be two paths for the same
question. One has to win per deployment:

- **Source mode:** the daemon files; mirarun's projector must not also
  create a request for the same intervention. mirarun answers through the
  answer URL and never resumes the run itself.
- **Poll mode (today):** unchanged.

The switch belongs on the execution deployment, since mirarun already
keeps one per agent. mirarun's open wake stack (mirarun #67, #76, #77,
#78) puts the wake in mirarun instead. The two overlap only for answers:
launches and schedules still need something to start a stopped agent, and
under this proposal that is still open.

## 5. What it does not cover

- **Model-tier approvals.** They are held in memory and block the turn.
  Making them durable is its own piece of work (#163 touches it).
- **External sessions.** A session in Claude Code or Codex is not an agent
  the daemon can start. Those sessions ask through MiraDesign, which is a
  source of its own.
- **Kubernetes.** A stopped agent there has no state to resume: `/agent` is
  a ConfigMap and `stop` deletes the Pod. See `portainer-placement.md` §6.

## 6. Decisions needed

1. **Who wakes a stopped agent.** This proposal (the daemon, for answers)
   or mirarun's wake stack (mirarun, for everything it sends). They can
   coexist, but then two systems start agents. Pick the owner.
2. **Whether the daemon should start an agent for an answer at all** when
   the agent was stopped deliberately. A flag per agent, like the wake
   stack's per-deployment opt-in?
3. **Source mode per deployment or for the whole daemon.**
