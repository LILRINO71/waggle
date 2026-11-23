"""Replay the tool calls of the owner's real chat turns through a checkout's permission policy (read-only).

    py -3.14 tools/replay_asks.py [--repo PATH] [--since EPOCH] [--json]

Turns come from ~/.waggle/traces.db (origin "user"); each turn's tool calls, with their full inputs, come from the
chat's Claude session transcript (~/.claude/projects/*/<session>.jsonl). Every call is decided by `policy.decide` of
the checkout in --repo (default: this one) with the owner's own words as the turn state, so the owner rule applies
exactly as in the live hook. Nothing is written: no approval is filed, the guard's integrity gate is skipped.
Prints asks and denies with the command, and the totals. Used for Step 1's "0 false asks" evidence."""
import argparse
import glob
import json
import os
import sqlite3
import sys
from pathlib import Path


def turns(since=0.0):
    db = sqlite3.connect("file:" + str(Path.home() / ".waggle" / "traces.db") + "?mode=ro", uri=True)
    rows = db.execute("SELECT id, time, request, extra FROM traces WHERE origin='user' AND time>=? ORDER BY id",
                      (since,)).fetchall()
    out = []
    for tid, t, req, extra in rows:
        try:
            e = json.loads(extra or "{}")
            sess = e.get("session")
            sess = json.loads(sess) if isinstance(sess, str) and sess.startswith("{") else sess
            sid = (sess or {}).get("claude_session") if isinstance(sess, dict) else None
        except (ValueError, TypeError, AttributeError):
            sid = None
        voice = '"voice": true' in (extra or "").lower() or "'voice': true" in (extra or "").lower()
        out.append({"id": tid, "time": t, "request": req or "", "session": sid, "voice": voice})
    return out


def session_file(sid):
    hits = glob.glob(str(Path.home() / ".claude" / "projects" / "*" / f"{sid}.jsonl"))
    return hits[0] if hits else None


def calls_by_turn(path, requests):
    """{request text: [(tool, input)]}: tool uses after the user message that carries the request."""
    out, current = {}, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            msg = ev.get("message") or {}
            content = msg.get("content")
            if ev.get("type") == "user" and msg.get("role") == "user":
                text = content if isinstance(content, str) else " ".join(
                    c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text")
                if text:
                    # the owner's words come last (after the context, note and voice blocks; a fresh session's seed
                    # of earlier requests comes first), so a request only matches the tail of the message
                    tail = text.rsplit("</", 1)[-1].split(">", 1)[-1] if "</" in text else text
                    tail = tail.split("\n\n<waggle_attachments>", 1)[0]
                    match = [r for r in requests if r and r.strip() and r.strip() in tail]
                    current = max(match, key=len) if match else None  # a turn of another origin (canary, test)
                    if current:
                        out.setdefault(current, [])
            elif ev.get("type") == "assistant" and current and isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        out[current].append((c.get("name"), c.get("input") or {}))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--since", type=float, default=0.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    sys.path.insert(0, a.repo)
    from waggle import guard, policy
    guard.gate = lambda *x, **k: None  # judging the rules, not this machine's manifest
    from waggle import security
    security._owner_turn = lambda: True  # every replayed turn is the owner's (the live hook reads it from a file)
    ts = turns(a.since)
    by_session = {}
    for t in ts:
        if t["session"]:
            by_session.setdefault(t["session"], []).append(t)
    total, asks, denies, rows = 0, [], [], []
    for sid, group in by_session.items():
        path = session_file(sid)
        if not path:
            continue
        found = calls_by_turn(path, [t["request"] for t in group])
        for t in group:
            state = {"owner": True, "origin": "user", "text": t["request"], "voice": t["voice"], "confident": True}
            for tool, args in found.get(t["request"], []):
                total += 1
                d = policy.decide(tool, args, cwd=str(Path.home() / ".waggle" / "chat_home"), turn=state)
                call = policy._short(tool, args)
                rows.append({"turn": t["id"], "tool": tool, "action": d.action, "reason": d.reason, "call": call})
                if d.action == "ask":
                    asks.append(rows[-1])
                elif d.action == "deny":
                    denies.append(rows[-1])
    summary = {"repo": a.repo, "owner_turns": len(ts), "turns_with_transcript": sum(
        1 for t in ts if t["session"] and session_file(t["session"])), "tool_calls": total, "asks": len(asks),
        "denies": len(denies)}
    if a.json:
        print(json.dumps({"summary": summary, "asks": asks, "denies": denies}, ensure_ascii=False))
    else:
        for r in asks + denies:
            print(f"{r['action'].upper():5} turn {r['turn']}: {r['reason'][:110]}\n      {r['call'][:160]}")
        print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
