"""Run long real-use scenarios (bench/scenarios.json) against the LIVE Waggle and judge every answer.

Each turn goes to the live core's voice line as the Home page does (client dash, JSON reply) with the test mark, so
the real engine, tools, memory and permission hook answer it, the turn is recorded as origin "canary" and nothing is
learned from it. A turn still running after the voice line's hold time is followed in traces.db until it finishes.
A separate model (Sonnet, tool-less) judges each answer against the scenario's expectation.

  py -3.14 tools/scenarios.py                       every scenario
  py -3.14 tools/scenarios.py --only study-week     one scenario (repeatable)
  py -3.14 tools/scenarios.py --no-judge            send and record only

Results: ~/.waggle/scenarios/<stamp>.json and a table on stdout. Run from the live checkout's Python or any checkout:
only the live core's HTTP port and the live home's databases are used.
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

MARK = "[waggle-test] "
FOLLOW_S = 600          # a held turn is followed this long in traces.db
JUDGE_SYSTEM = """You judge one answer of a personal assistant (Waggle) to its owner, a 17-year-old student in
California who does FTC robotics and builds software projects. You get the conversation so far, the message, what a
good answer does, the tools Waggle called (with short results) and its answer. Judge only against the expectation
and plain correctness; be strict about invented facts, claims of actions that the tool list does not show, and
answers that ignore the question. Return JSON only."""
JUDGE_SCHEMA = {"type": "object", "properties": {"pass": {"type": "boolean"}, "why": {"type": "string"},
                                                 "fault": {"type": "string"}},
                "required": ["pass", "why"]}


def home():
    return Path(os.environ.get("WAGGLE_HOME") or Path.home() / ".waggle")


def core_port():
    try:
        cfg = json.loads((home() / "config.json").read_text(encoding="utf-8"))
        return int(cfg.get("core_port") or 8896)
    except (OSError, ValueError):
        return 8896


def send(text, timeout=240):
    """(status code, headers, body dict, seconds)."""
    token = (home() / "voice.token").read_text(encoding="utf-8").strip()
    req = urllib.request.Request(f"http://127.0.0.1:{core_port()}/voice?reply=json&client=dash",
                                 data=json.dumps({"text": MARK + text}).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    t0 = time.monotonic()
    for attempt in range(200):   # one voice request at a time: wait up to 10 minutes
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, json.loads(r.read()), \
                    time.monotonic() - t0
        except urllib.error.HTTPError as e:
            if e.code == 429:            # one voice request at a time: wait for the previous one
                time.sleep(3)
                continue
            body = e.read()
            try:
                data = json.loads(body)
            except ValueError:
                data = {"error": body[:200].decode("utf-8", "replace")}
            return e.code, {}, data, time.monotonic() - t0
    return 429, {}, {"error": "busy for ten minutes"}, time.monotonic() - t0


def trace_of(turn_id):
    con = sqlite3.connect(home() / "traces.db")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM traces WHERE turn_id=? ORDER BY id DESC LIMIT 1", (turn_id,)).fetchone()
    steps = []
    if row:
        steps = [dict(s) for s in con.execute("SELECT tool, input, ok, denied, result FROM steps WHERE trace_id=? "
                                              "ORDER BY idx", (row["id"],))]
    con.close()
    return (dict(row) if row else None), steps


def trace_by_text(text, since):
    """The newest trace of a message with this text recorded after `since` (for a held answer that does not name
    its turn: the core before 2026-11-25 left the turn id out of the holding answer)."""
    con = sqlite3.connect(home() / "traces.db")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT turn_id FROM traces WHERE time>=? AND request LIKE ? ORDER BY id DESC LIMIT 1",
                      (since, "%" + text.strip()[:80].replace("%", "") + "%")).fetchone()
    con.close()
    return trace_of(row["turn_id"]) if row and row["turn_id"] else (None, [])


def follow(turn_id, limit_s=FOLLOW_S, text="", since=0.0):
    """Wait until the turn's trace exists (the engine writes it when the turn ends)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        row, steps = trace_of(turn_id) if turn_id else trace_by_text(text, since)
        if row:
            return row, steps
        time.sleep(4)
    return None, []


def short_tool(name):
    return name.split("__")[-1]


def judge(history, turn, reply, steps):
    from waggle import claude_cli
    tools = "\n".join(f"- {short_tool(s['tool'])} {'(denied)' if s['denied'] else '' if s['ok'] else '(failed)'}: "
                      f"{(s['input'] or '')[:160]} -> {(s['result'] or '')[:220]}" for s in steps) or "(none)"
    hist = "\n".join(f"owner: {h['text']}\nwaggle: {h['reply'][:500]}" for h in history[-4:]) or "(start)"
    prompt = (f"Conversation so far:\n{hist}\n\nMessage:\n{turn['text']}\n\nA good answer:\n{turn['expect']}\n\n"
              f"Tools Waggle called:\n{tools}\n\nWaggle's answer:\n{reply[:3000]}")
    res = claude_cli.ask(prompt, model="sonnet", system=JUDGE_SYSTEM, schema=JUDGE_SCHEMA, effort="low",
                         purpose="scenario-judge", timeout=180)
    return res.get("json") or {"pass": False, "why": "judge gave no verdict"}


def run_turn(turn, history, use_judge=True):
    sent_at = time.time() - 2
    code, headers, body, secs = send(turn["text"])
    out = {"text": turn["text"], "code": code, "seconds": round(secs, 1), "status": headers.get("x-waggle-status"),
           "turn": headers.get("x-waggle-turn", "")}
    if code != 200:
        out.update(ok=False, reply=f"HTTP {code}: {body.get('error', '')}", steps=[], verdict={"pass": False,
                   "why": f"the core answered {code}"})
        return out
    reply = body.get("text") or ""
    row, steps = trace_of(out["turn"]) if out["turn"] else (None, [])
    if out["status"] == "working" or row is None:
        row, steps = follow(out["turn"], text=turn["text"], since=sent_at)
        out["held"] = True
        if row and not out["turn"]:
            out["turn"] = row.get("turn_id") or ""
    if row:
        reply = row.get("reply") or reply
        out["seconds_total"] = round(float(row.get("latency_s") or secs), 1)
        out["trace_ok"] = bool(row.get("ok"))
        out["origin"] = row.get("origin")
    out["reply"] = reply
    out["steps"] = [{"tool": short_tool(s["tool"]), "ok": s["ok"], "denied": s["denied"],
                     "input": (s["input"] or "")[:300], "result": (s["result"] or "")[:400]} for s in steps]
    names = {s["tool"] for s in out["steps"]}
    if turn.get("tools_any") and not names & set(turn["tools_any"]):
        out["tool_miss"] = f"none of {turn['tools_any']} was called (called: {sorted(names) or 'nothing'})"
    if use_judge:
        try:
            out["verdict"] = judge(history, turn, reply, steps)
        except Exception as e:  # noqa: BLE001
            out["verdict"] = {"pass": False, "why": f"judge failed: {e}"[:200]}
    ok = (out.get("verdict") or {}).get("pass", True) and not out.get("tool_miss") and out.get("trace_ok", True)
    out["ok"] = bool(ok)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--file", default=str(REPO / "bench" / "scenarios.json"))
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    from waggle import budget
    budget.OWN_ALLOWANCE["scenario-judge"] = {"stop_at": 0.99, "daily_calls": 10 ** 6, "week_stop_at": 0.99}
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    runs = []
    for sc in data["scenarios"]:
        if args.only and sc["id"] not in args.only:
            continue
        history, turns = [], []
        print(f"\n=== {sc['id']}", flush=True)
        for t in sc["turns"]:
            r = run_turn(t, history, use_judge=not args.no_judge)
            turns.append(r)
            history.append({"text": t["text"], "reply": r["reply"]})
            v = r.get("verdict") or {}
            print(f"{'PASS' if r['ok'] else 'FAIL'} {r.get('seconds_total', r['seconds']):>6}s "
                  f"{'(held) ' if r.get('held') else ''}{t['text'][:60]!r}\n      tools: "
                  f"{', '.join(s['tool'] + ('' if s['ok'] else '!') for s in r['steps']) or '-'}\n      "
                  f"{(v.get('why') or '')[:260]}" + (f"\n      TOOL MISS: {r['tool_miss']}" if r.get('tool_miss') else ""),
                  flush=True)
        runs.append({"id": sc["id"], "turns": turns})
    total = sum(len(r["turns"]) for r in runs)
    good = sum(t["ok"] for r in runs for t in r["turns"])
    print(f"\n{good} of {total} turns passed")
    out = home() / "scenarios"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(runs, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"details: {path}")
    return 0 if good == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
