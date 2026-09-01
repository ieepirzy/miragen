"""Spawn substrates: where and how a managed agent's process actually runs.

`miragen.daemon.core.LifecycleCore` owns everything ABOUT an agent (naming,
workspace layout, profile/contract validation, credential forwarding,
boot-watching); a `SpawnDriver` owns everything about the underlying compute
unit a spawned agent runs in. Adding a second substrate (Kubernetes) means
adding a second driver behind `spawn.base.SpawnDriver` — LifecycleCore itself
does not change per substrate.
"""

from __future__ import annotations
