"""A scripted `codex app-server` (JSON-RPC over stdio) for the Codex harness
tests. Shapes follow codex 0.159's app-server as observed live. Logs what it
was started with and asked for to $CODEX_HOME/fake.log.jsonl."""

import json
import os
import sys
import uuid
from pathlib import Path

HOME = Path(os.environ["CODEX_HOME"])
LOG = HOME / "fake.log.jsonl"
threads: dict[str, list[str]] = {}
carried: dict[str, int] = {}
running = {"in": 0, "cached": 0, "out": 0}
pending: dict[int, dict] = {}
next_id = 1000


def log(entry):
    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def rollout(tid: str) -> Path:
    return HOME / "sessions" / "2026" / f"rollout-x-{tid}.jsonl"


def ask(method, params):
    """A server->client request; returns the client's answer."""
    global next_id
    next_id += 1
    send({"id": next_id, "method": method, "params": params})
    while True:
        msg = json.loads(sys.stdin.readline())
        if msg.get("id") == next_id and "method" not in msg:
            return msg.get("result", msg.get("error"))


def note(method, params):
    send({"method": method, "params": params})


def message(tid, turn, text, phase="final_answer"):
    note("item/completed", {"threadId": tid, "turnId": turn,
                            "item": {"type": "agentMessage", "text": text, "phase": phase}})


def append(tid, entry):
    if rollout(tid).exists():
        with rollout(tid).open("a") as f:
            f.write(json.dumps(entry) + "\n")


def msg_item(role, text):
    kind = "input_text" if role == "user" else "output_text"
    return {"type": "response_item", "payload": {"type": "message", "role": role, "id": "x",
                                                 "content": [{"type": kind, "text": text}]}}


def run_turn(tid, turn, text):
    threads[tid].append(text)
    append(tid, msg_item("user", text))
    prompt = text.rsplit("\n\n", 1)[-1]
    status, error = "completed", None
    # A sub-agent's notification on another thread must be ignored.
    note("item/completed", {"threadId": "sub-agent", "turnId": "x",
                            "item": {"type": "agentMessage", "text": "SUBAGENT", "phase": "final_answer"}})
    message(tid, turn, "working on it", phase="commentary")
    if prompt.startswith("CALL "):
        _, tool, args = prompt.split(" ", 2)
        out = ask("item/tool/call", {"threadId": tid, "turnId": turn, "callId": "c1",
                                     "namespace": None, "tool": tool, "arguments": json.loads(args)})
        message(tid, turn, f"tool[{tool}]={out['contentItems'][0]['text']} ok={out['success']}")
    elif prompt == "PATCH":
        out = ask("item/fileChange/requestApproval", {"threadId": tid, "turnId": turn, "itemId": "i"})
        message(tid, turn, f"patch={out['decision']}")
    elif prompt == "WEIRD":
        out = ask("some/new/request", {"threadId": tid, "turnId": turn})
        message(tid, turn, f"weird={json.dumps(out)}")
    elif prompt.startswith("SLOWCALL "):
        # A tool call that arrives after the client has given up on the turn.
        import time as _t
        _t.sleep(float(prompt.split()[1]))
        out = ask("item/tool/call", {"threadId": tid, "turnId": turn, "callId": "c2",
                                     "namespace": None, "tool": "speak", "arguments": {"text": "late"}})
        log({"late_call": out})
        message(tid, turn, "late done")
    elif prompt == "HISTORY":
        message(tid, turn, f"turns={len(threads[tid])} carried={carried.get(tid, 0)}")
    elif prompt == "FAIL":
        status, error = "failed", {"message": "You've hit your usage limit."}
    elif prompt == "COMPACT":
        append(tid, {"type": "compacted", "payload": {"message": "", "replacement_history": [
            msg_item("user", "SUMMARISED")["payload"],
            {"type": "compaction", "id": "c", "encrypted_content": "ENC"}]}})
        note("item/completed", {"threadId": tid, "turnId": turn,
                                "item": {"type": "contextCompaction", "id": "cc"}})
        message(tid, turn, "compacted")
    elif prompt == "ONLYCOMMENTARY":
        pass
    else:
        message(tid, turn, f"echo: {text}")
    append(tid, msg_item("assistant", "reply"))
    # Two model calls per turn, as codex reports them: `total` is the
    # thread's running total across turns; the last update is repeated.
    for _ in range(2):
        running["in"] += 450
        running["cached"] += 200
        running["out"] += 10
        update = {"threadId": tid, "turnId": turn, "tokenUsage": {
            "total": {"inputTokens": running["in"], "cachedInputTokens": running["cached"],
                      "outputTokens": running["out"]},
            "last": {"inputTokens": 450, "cachedInputTokens": 200, "outputTokens": 10}}}
        note("thread/tokenUsage/updated", update)
    note("thread/tokenUsage/updated", update)
    note("turn/completed", {"threadId": tid, "turn": {"id": turn, "status": status, "error": error}})


def main():
    log({"argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd()})
    for line in sys.stdin:
        msg = json.loads(line)
        method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if rid is None:
            continue
        if method == "initialize":
            log({"initialize": params})
            send({"id": rid, "result": {"userAgent": "fake"}})
        elif method == "thread/start":
            tid = str(uuid.uuid4())
            threads[tid] = []
            if not params.get("ephemeral"):
                rollout(tid).parent.mkdir(parents=True, exist_ok=True)
                rollout(tid).write_text(json.dumps({"type": "session_meta"}) + "\n")
            log({"thread_start": params, "id": tid})
            send({"id": rid, "result": {"thread": {"id": tid}}})
        elif method == "thread/resume":
            tid = params["threadId"]
            log({"thread_resume": params})
            if not rollout(tid).exists():
                send({"id": rid, "error": {"code": -32600, "message": f"no rollout found for {tid}"}})
                continue
            users = [line for line in rollout(tid).read_text().splitlines() if '"role": "user"' in line]
            threads.setdefault(tid, [None] * len(users))
            send({"id": rid, "result": {"thread": {"id": tid}}})
        elif method == "turn/start":
            turn = str(uuid.uuid4())
            text = params["input"][0]["text"]
            log({"turn_start": params})
            send({"id": rid, "result": {"turn": {"id": turn, "status": "inProgress"}}})
            run_turn(params["threadId"], turn, text)
        elif method == "experimentalFeature/list":
            from miragen.harness.codex import FEATURES_OFF

            # (a file, not env: the harness passes only an env allowlist)
            cfg = HOME / "fake-features.json"
            cfg = json.loads(cfg.read_text()) if cfg.exists() else {}
            off = set(FEATURES_OFF) - set(cfg.get("missing", []))
            on = set(cfg.get("on", ["unified_exec"]))
            send({"id": rid, "result": {"data": [
                {"name": f, "enabled": f in on} for f in sorted(off | on)]}})
        elif method == "thread/inject_items":
            log({"inject_items": params})
            carried[params["threadId"]] = len(params["items"])
            send({"id": rid, "result": {}})
        else:
            send({"id": rid, "result": {}})


main()
