"""Scrub anything token-shaped from text the spoke logs or sends to the hub.

An agent's or the hub's error detail is not trusted to be clean: a peer
that echoes its input could carry a credential into a log line or a settle
reason the hub stores. Whatever looks like a JWT, or a long run of
base64url or hex, is replaced before it goes anywhere.
"""

from __future__ import annotations

import re

_JWT = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")
_OPAQUE = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
REDACTED = "[redacted]"


def scrub(text: str) -> str:
    return _OPAQUE.sub(REDACTED, _JWT.sub(REDACTED, text))
