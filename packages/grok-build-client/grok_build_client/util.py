"""Shared helpers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_grok_bin(explicit: str | None = None) -> str | None:
    """Return path to the `grok` binary, or None if not found."""
    if explicit:
        return explicit if Path(explicit).exists() else None
    env_bin = os.environ.get("GROK_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin
    return shutil.which("grok")
