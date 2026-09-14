"""The runtime tool library — default tools miragen ships across
harnesses (requested 2026-09-13, modeled on Claude Code's built-ins).

Every library tool serves BOTH established surfaces: injected as a plain
tool on model-tier agents (`build_agent(extra_tools=…)`) and mounted as
MCP for executor-tier agents — the same dual pattern voice and memory
already use. Profiles gate the library through the `runtime_tools:` block.

First member: scheduling (`miragen/runtime_tools/scheduling.py`) —
self-wakeups and recurring schedules over the managed-schedules machinery.
"""
