"""miragen-hook — the harness-side adapter for miragend's session plane.

Deliberately dependency-free: this package imports only the standard
library so that a hook invocation (one process per harness event) costs a
few tens of milliseconds, not the ~1 s the `miragen` package pays for
`pydantic-ai`. It is installed by the `miragen` distribution but never
imports it.

One command, one event: `miragen-hook <harness>` reads a hook payload on
stdin, normalizes it (`normalize.py`), posts one envelope to miragend
(`client.py`) and, when the daemon answers with context, prints the
harness's own output shape. Everything fails open — a missing daemon must
never block the agent's work (design record: docs/design/external-sessions.md).
"""

ADAPTER_VERSION = "miragen-hook/1"

__all__ = ["ADAPTER_VERSION"]
