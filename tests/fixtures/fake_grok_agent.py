#!/usr/bin/env python3
"""A stand-in for `grok agent stdio` (ACP over JSON-RPC lines), stdlib only.

Seeded with the `initialize` answer captured from grok 1.0.41 on 2026-09-24
(auth method `grok.com`, `x.ai/hooks`, session list/resume/close). It keeps
sessions under $GROK_HOME/sessions/<quoted cwd>/<id>/ (grok's layout), requires $GROK_HOME/auth.json to
authenticate, logs its argv/env to $GROK_HOME/fake-agent.log.jsonl, and
reaches MCP tools the way grok does: through the server declared in
$GROK_HOME/config.toml, with `${VAR}` header expansion.

Prompt directives (one per line) script the "model":
  CALL <tool> <json-args>   ask permission for gateway__<tool>, then call it
  BUILTIN                   ask permission for run_terminal_cmd
  SLEEP                     block until session/cancel
  HISTORY                   answer with the session's turn count and rules
anything else is echoed back.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote

HOME = Path(os.environ["GROK_HOME"])
# grok's real layout (1.0.41): sessions/<percent-encoded cwd>/<session id>/
SESSIONS = HOME / "sessions"
SESSIONS.mkdir(parents=True, exist_ok=True)
LOG = HOME / "fake-agent.log.jsonl"

INIT = {
    "protocolVersion": 1,
    "agentCapabilities": {
        "loadSession": True,
        "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
        "mcpCapabilities": {"http": True, "sse": True},
        "sessionCapabilities": {"list": {}, "resume": {}, "close": {}},
        "auth": {},
        "_meta": {"x.ai/hooks": {"blockingEvents": ["pre_tool_use", "stop", "subagent_stop"],
                                 "decisions": ["deny", "block"]}},
    },
    "authMethods": [{"id": "grok.com", "name": "Grok", "description": "Sign in with Grok"}],
}

_out_lock = threading.Lock()
_pending: dict[str, dict] = {}
_pending_ev: dict[str, threading.Event] = {}
_cancelled: dict[str, threading.Event] = {}
_authed = False


def send(msg: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def log(entry: dict) -> None:
    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def session_path(sid: str) -> Path:
    found = list(SESSIONS.glob(f"*/{sid}/session.json"))
    return found[0] if found else SESSIONS / "_missing" / sid / "session.json"


def load(sid: str) -> dict:
    return json.loads(session_path(sid).read_text())


def save(sid: str, data: dict) -> None:
    path = session_path(sid)
    if not path.exists():
        path = SESSIONS / quote(data.get("cwd") or "", safe="") / sid / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def gateway() -> tuple[str, str]:
    cfg = (HOME / "config.toml").read_text()
    block = cfg.split("[mcp_servers.gateway]", 1)[1]
    url = re.search(r'url = "([^"]+)"', block).group(1)
    header = re.search(r'Authorization = "([^"]+)"', block).group(1)
    header = re.sub(r"\$\{([A-Z_]+)\}", lambda m: os.environ.get(m.group(1), ""), header)
    return url, header


def ask(sid: str, tool: str, raw: dict) -> str | None:
    rid = f"perm-{uuid.uuid4().hex[:8]}"
    ev = threading.Event()
    _pending_ev[rid] = ev
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": sid,
        "toolCall": {"toolCallId": rid, "title": tool, "kind": "other", "rawInput": raw},
        "options": [{"optionId": "yes-once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "no-once", "name": "Reject", "kind": "reject_once"}]}})
    ev.wait(30)
    outcome = (_pending.pop(rid, {}) or {}).get("outcome") or {}
    return outcome.get("optionId") if outcome.get("outcome") == "selected" else None


def call_gateway(tool: str, args: dict) -> str:
    url, header = gateway()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": header, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return f"gateway-error:{exc}"
    result = data.get("result") or {}
    text = "".join(c.get("text", "") for c in result.get("content") or [])
    return ("ERROR " if result.get("isError") else "") + text


def chunk(sid: str, text: str) -> None:
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text", "text": text}}}})


def handle_prompt(rid, params):
    sid = params["sessionId"]
    text = "".join(b.get("text", "") for b in params.get("prompt") or [])
    data = load(sid)
    data["turns"].append(text)
    save(sid, data)
    stop = "end_turn"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SAY "):
            chunk(sid, line[4:])  # no trailing newline: the next segment must be kept apart
        elif line.startswith("CALL "):
            _, tool, raw = line.split(" ", 2)
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
                "sessionUpdate": "tool_call", "toolCallId": tool, "title": f"gateway__{tool}",
                "kind": "other", "status": "pending"}}})
            decision = ask(sid, f"gateway__{tool}", json.loads(raw))
            if decision == "yes-once":
                chunk(sid, f"tool[{tool}]={call_gateway(tool, json.loads(raw))}\n")
            else:
                chunk(sid, f"tool[{tool}]=denied\n")
        elif line == "BUILTIN":
            decision = ask(sid, "run_terminal_cmd", {"command": "hostname"})
            chunk(sid, "builtin=" + ("ran" if decision == "yes-once" else "refused") + "\n")
        elif line == "SLEEP":
            ev = _cancelled.setdefault(sid, threading.Event())
            ev.clear()
            if ev.wait(30):
                stop = "cancelled"
                break
        elif line == "HISTORY":
            chunk(sid, f"turns={len(data['turns'])} rules={data.get('rules')!r}\n")
        elif line:
            chunk(sid, "echo: ")
            chunk(sid, line[-80:] + "\n")
    send({"jsonrpc": "2.0", "id": rid, "result": {
        "stopReason": stop, "_meta": {"usage": {"inputTokens": 10, "outputTokens": 5}}}})


def main():
    global _authed
    log({"argv": sys.argv[1:], "env": sorted(os.environ), "home": os.environ.get("HOME"),
         "cwd": os.getcwd(), "has_gateway_token": bool(os.environ.get("MIRAGEN_GATEWAY_TOKEN"))})
    for raw in sys.stdin:
        msg = json.loads(raw)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None and rid in _pending_ev:  # a reply to our request
            _pending[rid] = msg.get("result") or {}
            _pending_ev.pop(rid).set()
            continue
        if method == "initialize":
            log({"initialize": params})
            send({"jsonrpc": "2.0", "id": rid, "result": INIT})
        elif method == "authenticate":
            if (HOME / "auth.json").exists() and params.get("methodId") == "grok.com":
                _authed = True
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Authentication required"}})
        elif method in ("session/new", "session/load", "_x.ai/session/fork", "session/prompt") and not _authed:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Authentication required"}})
        elif method == "session/new":
            sid = str(uuid.uuid4())
            save(sid, {"rules": (params.get("_meta") or {}).get("rules"), "cwd": params.get("cwd"),
                       "turns": []})
            log({"session_new": sid, "meta": params.get("_meta"), "mcpServers": params.get("mcpServers")})
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid}})
        elif method == "session/load":
            sid = params["sessionId"]
            log({"session_load": sid, "cwd": params.get("cwd"), "mcpServers": params.get("mcpServers")})
            if session_path(sid).exists():
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "session not found"}})
        elif method == "_x.ai/session/fork":
            # grok 1.0.41's contract (probed live): sourceSessionId,
            # sourceCwd, newCwd required; the fork copies the conversation
            # AND the original rules (new _meta.rules are ignored).
            missing = [k for k in ("sourceSessionId", "sourceCwd", "newCwd") if k not in params]
            if missing:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Invalid params",
                      "data": f"invalid params: missing field `{missing[0]}`"}})
                continue
            old = load(params["sourceSessionId"])
            sid = str(uuid.uuid4())
            save(sid, {**old, "cwd": params["newCwd"]})
            log({"fork": params["sourceSessionId"], "to": sid})
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "newSessionId": sid, "parentSessionId": params["sourceSessionId"],
                "chatMessagesCopied": len(old["turns"]), "newCwd": params["newCwd"]}})
        elif method == "session/prompt":
            threading.Thread(target=handle_prompt, args=(rid, params), daemon=True).start()
        elif method == "session/cancel":
            _cancelled.setdefault(params["sessionId"], threading.Event()).set()
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown {method}"}})


if __name__ == "__main__":
    main()
