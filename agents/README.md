# Deployed agent definitions

Each subdirectory is one agent, in the exact layout miragend keeps in its
workspace:

```
agents/<name>/
  agent.yaml   # the profile, validated by miragen.models.AgentProfile
  tools.py     # @register tools the profile whitelists under `tools:`
```

This directory is the reviewable source of truth for agents that run in
production. It is **not** what the daemon reads. The live copy lives at
`$MIRAGEN_WORKSPACE/agents/<name>/` on the daemon's host (default
`/opt/miragen`), and the daemon bind-mounts it into the agent container at
`/agent`. Changes here reach production only when the profile is applied
through a control plane — mirarun's `POST /api/agents`, or miragen-mcp — or
written into the workspace directly.

`agents/` is in `.dockerignore`: these are deploy inputs, like the compose
files, and must not be baked into the published engine image.

## Validating before applying

A profile that fails validation is rejected by `create_agent`, so check it
first with the same function the daemon calls:

```bash
python -c "
from miragen.daemon.core import validate_profile_text
print(validate_profile_text(open('agents/seo-audit/agent.yaml').read()))
"
```

## Agents

| Agent | Mode | Trigger | What it does |
|---|---|---|---|
| `seo-audit` | autonomous | cron, Mondays 06:00 UTC | Weekly SEO audit of muutto365.fi. Read-only HTTP GETs against a public site; writes nothing. |

## Two things worth knowing before you write a profile

**`on_complete` fails silently.** `_handle_on_complete` dispatches with
`if oc.notify and oc.notify in handlers` — a handler name that was never
registered is skipped without a warning, and miragen ships no handlers built
in. A profile saying `notify: telegram` with no registered `telegram` handler
produces no notification and no error, which reads exactly like delivery that
worked. Register the handler in the agent's `tools.py` with
`@register_handler`, or leave `on_complete` out.

**`WebFetch` is not a page fetcher.** The capability returns markdownified
content (`md(text, strip=['img', 'script', 'style'])`) with no status code and
no response headers. That is fine for reading an article and useless for
anything that inspects `<head>`, structured data, alt attributes, redirects,
or status. `seo-audit/tools.py` shows the alternative: fetch with `httpx`,
return facts.
