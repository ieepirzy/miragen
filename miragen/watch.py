"""The inbox: what is new or changed in the agent's sources, known without a
model turn.

The profile's ``watch`` sources name read-only gateway tools. Between turns
the host calls each one on its interval (``ToolGateway.read_tool``: the same
upstream, credential and ``allowed_tools`` as the model's own call), takes the
items out of the result, and diffs them against what it has seen:

* an item is identified by its ``id`` path when the source has one (an edit
  is then *changed*), else by a hash of its whole content;
* an item that only leaves the result window is not a change, and one seen
  before (within ``SEEN_RETENTION_S``) is not new when it comes back;
* the very first poll of a source is a baseline and produces no entries.

What a consumer (Mira's heartbeat) may rely on, and what keeps "the tool shows
it but the inbox didn't" from happening:

1. **Same view** — the watcher *is* the tool call; configure it as wide as the
   agent would look.
2. **Fail open** — a failed or unparseable poll is ``status: error``, a source
   not polled yet is ``status: never``, and a window whose every item is new
   (more may lie beyond it) sets ``overflow``. None of those may be read as
   "nothing new".
3. **Observed is not seen** — entries stay until acknowledged
   (``ack(through=seq)``); advancing the snapshot never clears them.

State lives in one JSON file, written atomically after each poll.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from miragen.models import WatchSource

logger = logging.getLogger("miragen.watch")

SEEN_RETENTION_S = 14 * 24 * 3600
MAX_ENTRIES_PER_SOURCE = 100
_SUMMARY_CHARS = 400

ReadTool = Callable[[str, dict], Awaitable[Any]]


class WatchError(RuntimeError):
    """A poll whose result can't be trusted (tool error, bad shape)."""


def _dig(value: Any, path: str | None) -> Any:
    if not path:
        return value
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def payload_of(result: Any) -> Any:
    """The JSON value a tool returned (an MCP CallToolResult or a plain value)."""
    if getattr(result, "isError", False):
        text = "".join(getattr(c, "text", "") for c in getattr(result, "content", []) or [])
        raise WatchError(f"tool error: {text[:300]}")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        # FastMCP wraps non-object returns as {"result": ...}
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    content = getattr(result, "content", None)
    if content is None:
        return result
    text = "".join(getattr(c, "text", "") for c in content or [])
    try:
        return json.loads(text)
    except ValueError as exc:
        raise WatchError("tool result is not JSON") from exc


class Inbox:
    def __init__(self, sources: list[WatchSource], read_tool: ReadTool, state_path: Path, *,
                 clock: Callable[[], float] = time.time):
        self.sources = {s.name: s for s in sources}
        self.read_tool = read_tool
        self.path = Path(state_path)
        self.clock = clock
        self.state = self._load()
        for name in self.sources:
            self.state["sources"].setdefault(name, self._fresh_source())
        self.changed = asyncio.Event()

    # ── state ────────────────────────────────────────────────────────────
    @staticmethod
    def _fresh_source() -> dict:
        return {"status": "never", "seen": {}, "polled_at": None, "last_ok_at": None,
                "last_error": None, "overflow": False}

    def _load(self) -> dict:
        try:
            state = json.loads(self.path.read_text())
            if isinstance(state, dict) and "sources" in state and "entries" in state:
                return state
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning("inbox state unreadable (%s); starting over", exc)
        return {"seq": 0, "sources": {}, "entries": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".inbox-")
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)

    # ── polling ──────────────────────────────────────────────────────────
    async def poll(self, name: str) -> int:
        """Poll one source; returns how many entries it added."""
        spec = self.sources[name]
        src = self.state["sources"][name]
        now = self.clock()
        src["polled_at"] = now
        try:
            items = _dig(payload_of(await self.read_tool(spec.tool, dict(spec.arguments))), spec.items)
            if isinstance(items, dict) and items.get("ok") is False:
                raise WatchError(f"tool reported failure: {str(items.get('error'))[:300]}")
            if not isinstance(items, list):
                raise WatchError(f"'{spec.items or 'result'}' is not a list")
        except Exception as exc:  # fail open: the consumer sees status=error
            src["status"], src["last_error"] = "error", f"{type(exc).__name__}: {exc}"[:500]
            logger.warning("inbox: polling %s failed: %s", name, src["last_error"])
            self._save()
            self.changed.set()
            return 0

        baseline = src["status"] == "never" and not src["seen"]
        seen: dict[str, list] = src["seen"]
        current: list[tuple[str, str, Any]] = []
        for item in items:
            digest = _digest(item)
            ident = _dig(item, spec.id) if spec.id else None
            key = f"id:{ident}" if ident not in (None, "") else f"h:{digest}"
            current.append((key, digest, item))

        added = 0
        fresh = [k for k, _d, _i in current if k not in seen]
        # Every item in the window is new: more may lie beyond it. Sticky
        # until acknowledged, so a later quiet poll can't hide it.
        if bool(current) and bool(seen) and len(fresh) == len(current):
            src["overflow"] = True
        for key, digest, item in current:
            before = seen.get(key)
            if not baseline and (before is None or before[0] != digest):
                self._add_entry(name, "new" if before is None else "changed", key, spec, item, now)
                added += 1
            seen[key] = [digest, now]
        cutoff = now - SEEN_RETENTION_S
        src["seen"] = {k: v for k, v in seen.items() if v[1] >= cutoff}
        src["status"], src["last_ok_at"], src["last_error"] = "ok", now, None
        self._save()
        if added:
            logger.info("inbox: %s has %d new or changed item(s)", name, added)
            self.changed.set()
        return added

    def _add_entry(self, source: str, kind: str, key: str, spec: WatchSource, item: Any,
                   now: float) -> None:
        self.state["seq"] += 1
        if spec.show:
            summary: Any = {p: _dig(item, p) for p in spec.show}
        else:
            raw = json.dumps(item, ensure_ascii=False, default=str)
            summary = raw if len(raw) <= _SUMMARY_CHARS else raw[:_SUMMARY_CHARS] + "…"
        entries = self.state["entries"]
        entries.append({"seq": self.state["seq"], "source": source, "kind": kind, "key": key,
                        "summary": summary, "at": now})
        mine = [e for e in entries if e["source"] == source]
        if len(mine) > MAX_ENTRIES_PER_SOURCE:
            drop = {id(e) for e in mine[:len(mine) - MAX_ENTRIES_PER_SOURCE]}
            self.state["entries"] = [e for e in entries if id(e) not in drop]
            self.state["sources"][source]["overflow"] = True

    async def run(self, stop: asyncio.Event) -> None:
        """Poll every source on its own interval until stopped."""
        due = dict.fromkeys(self.sources, 0.0)
        while not stop.is_set():
            now = self.clock()
            for name, at in due.items():
                if at <= now:
                    await self.poll(name)
                    due[name] = self.clock() + self.sources[name].every_s
            wait = max(1.0, min(due.values()) - self.clock()) if due else 60.0
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except TimeoutError:
                pass

    # ── consumers ────────────────────────────────────────────────────────
    def view(self) -> dict:
        return {
            "enabled": bool(self.sources),
            "seq": self.state["seq"],
            "sources": {name: {k: src.get(k) for k in ("status", "polled_at", "last_ok_at",
                                                       "last_error", "overflow")}
                        for name, src in self.state["sources"].items() if name in self.sources},
            "entries": list(self.state["entries"]),
        }

    def ack(self, through: int) -> int:
        """Drop entries up to and including seq ``through``; clears overflow
        on sources whose dropped entries were acknowledged."""
        before = len(self.state["entries"])
        acked_sources = {e["source"] for e in self.state["entries"] if e["seq"] <= through}
        self.state["entries"] = [e for e in self.state["entries"] if e["seq"] > through]
        for name in acked_sources:
            if name in self.state["sources"]:
                self.state["sources"][name]["overflow"] = False
        dropped = before - len(self.state["entries"])
        if dropped:
            self._save()
        return dropped
