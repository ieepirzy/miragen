# grok-build-client

Thin **MIT** client library for [xAI Grok Build](https://docs.x.ai/build/overview): drive the `grok` CLI as a self-harnessed coding agent from Python.

This is a **transport client**, not a second harness. It does not harvest diffs, enforce budgets, or own workspaces — orchestrators (e.g. [miragen](https://github.com/ieepirzy/miragen)) sit above it.

## Install

```bash
pip install grok-build-client
# monorepo / from source:
pip install ./packages/grok-build-client
```

Requires the `grok` binary on `PATH` (or set `GROK_BIN`). Auth is the product's: `GROK_HOME` + OAuth (`grok login --device-auth`) or `XAI_API_KEY`.

## Transports

### Headless (`streaming-json`)

One process per turn. Simple, good for unattended always-approve runs.

```python
import asyncio
from grok_build_client import HeadlessSession, resolve_grok_bin

async def main():
    async for event in HeadlessSession(
        prompt="Explain this repo",
        cwd=".",
        always_approve=True,
    ).run():
        print(event)

asyncio.run(main())
```

### ACP (`grok agent stdio`)

Longer-lived JSON-RPC agent. Supports `session/request_permission` so a host can gate tools (leash). Prefer this when you need pre-tool approval.

```python
import asyncio
from grok_build_client import AcpSession

async def main():
    async with AcpSession(always_approve=False, permission_handler=my_gate) as acp:
        session_id = await acp.session_new(cwd=".")
        async for update in acp.prompt(session_id, "List top-level files"):
            print(update)

asyncio.run(main())
```

## Events

Normalized event dicts (stable for orchestrators):

| `type` | Meaning |
|---|---|
| `text` | Assistant text chunk (`data`) |
| `thought` | Reasoning chunk (`data`) |
| `tool_call` | Tool invocation started |
| `end` | Terminal success (`sessionId`, `usage`, …) |
| `error` | Terminal failure (`message`) |
| `permission_request` | ACP asked for approval (handled via callback when set) |

Raw CLI/ACP payloads are also available under `raw` when useful for debugging.

## License

MIT — see [LICENSE](LICENSE).
