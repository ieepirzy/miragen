"""Thin MIT client for xAI Grok Build (headless + ACP stdio)."""

from __future__ import annotations

from grok_build_client.acp import AcpAuthError, AcpSession, PermissionDecision, permission_outcome
from grok_build_client.headless import HeadlessSession, build_headless_argv
from grok_build_client.normalize import normalize_acp_update, normalize_headless_event
from grok_build_client.util import resolve_grok_bin

__version__ = "0.1.0"

__all__ = [
    "AcpAuthError",
    "AcpSession",
    "permission_outcome",
    "HeadlessSession",
    "PermissionDecision",
    "build_headless_argv",
    "normalize_acp_update",
    "normalize_headless_event",
    "resolve_grok_bin",
    "__version__",
]
