"""Replay dataset: the owner's real turns, redacted, written under ~/.waggle/replay (never into the repo).

Two kinds of case:
  ok         one of the last N learnable turns whose recorded outcome was "ok" (the owner got an answer and nothing
             went wrong). Its old reply is the reference.
  corrected  a turn the owner corrected afterwards (traces.feedback < 0, or a correction in the learning ledger).
             The correction text rides along: a good new answer must not repeat the mistake. These are the hard cases.

Only origin "user" (or the old rows with no origin) is read: canary, test, bench and agent turns are never learned
from. Every text goes through policy.redact before it is stored. The database is opened read-only.

Cases that the replay cannot judge are left out: slash commands, bare approvals, one-word follow-ups that only
mean something inside their conversation, turns with attachments.
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from waggle.policy import redact  # noqa: E402

LEARNABLE = "(origin IS NULL OR origin='user')"
REQUEST_CAP = 2000
REFERENCE_CAP = 3000
CORRECTION_CAP = 500
READ_ONLY_TOOLS = ("read", "grep", "glob", "ls", "websearch", "webfetch", "recall", "search", "get", "list", "status",
                   "view", "find", "note_read", "related_to")
SKIP_REQUEST = re.compile(r"^\s*(/|(yes|no|ok|okay|sure|approve|deny|merge|drop|undo)\b[\s\w#.!]{0,12}$)", re.I)


def replay_dir():
    return Path(os.environ.get("WAGGLE_REPLAY_DIR") or Path.home() / ".waggle" / "replay")


def real_home():
    return Path(os.environ.get("WAGGLE_REAL_HOME") or Path.home() / ".waggle")


def _ro(path):
    uri = "file:" + str(Path(path)).replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def acts(tools):
    """True when the turn used a tool that can change something (anything not on the read-only list)."""
    for t in tools:
        name = str(t or "").lower()
        if name and not any(k in name for k in READ_ONLY_TOOLS):
            return True
    return False


def _corrected_trace_ids(home):
    """Trace ids the learning ledger holds as owner corrections (empty when there is no ledger yet)."""
    p = Path(home) / "knowledge.db"
    if not p.exists():
        return set()
    try:
        con = _ro(p)
        try:
            rows = con.execute("SELECT trace_id FROM improvements WHERE trace_id IS NOT NULL AND "
                               "(kind LIKE '%correction%' OR source LIKE '%correction%')").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return set()
    return {int(r[0]) for r in rows}


def _case(row, tools, kind):
    request = redact(row["request"] or "")[:REQUEST_CAP].strip()
    reference = redact(row["reply"] or "")[:REFERENCE_CAP].strip()
    correction = redact(row["correction"] or "")[:CORRECTION_CAP].strip()
    if kind == "corrected":
        category = "corrected"
    else:
        category = "acts" if acts(tools) else "chat"
    return {"id": f"t{row['id']}", "trace_id": row["id"], "kind": kind, "category": category,
            "request": request, "reference": reference, "correction": correction, "tools": sorted(set(tools)),
            "time": row["time"]}


def usable(row):
    req = (row["request"] or "").strip()
    return bool(req) and len(req) >= 8 and not SKIP_REQUEST.match(req) and "<waggle_attachments>" not in req


def build(n_ok=30, n_hard=20, home=None, include_acts=False):
    """The case list, newest first inside each kind. Empty lists are fine (a new install has no history). Turns where
    the owner got an ACTION (a tool changed something) are left out unless include_acts: the replay has no tools, so
    every such case would fail on both sides and say nothing."""
    home = Path(home) if home else real_home()
    db = home / "traces.db"
    if not db.exists():
        return []
    con = _ro(db)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(traces)")}
        outcome = "turn_outcome" if "turn_outcome" in cols else "NULL"
        rows = [dict(r) for r in con.execute(
            f"SELECT id, time, request, reply, route, feedback, correction, {outcome} AS turn_outcome FROM traces "
            f"WHERE {LEARNABLE} AND length(coalesce(request,''))>0 AND length(coalesce(reply,''))>0 "
            f"ORDER BY id DESC LIMIT 2000")]
        tools = {}
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in con.execute(f"SELECT trace_id, tool FROM steps WHERE trace_id IN ({','.join('?' * len(chunk))})",
                                 chunk):
                tools.setdefault(r[0], []).append(r[1])
    finally:
        con.close()
    ledger = _corrected_trace_ids(home)
    hard, ok = [], []
    for r in rows:
        if not usable(r):
            continue
        if (r["feedback"] or 0) < 0 or r["id"] in ledger:
            hard.append(_case(r, tools.get(r["id"], []), "corrected"))
        elif r["turn_outcome"] == "ok" and (r["feedback"] or 0) >= 0 and r["route"] not in ("command", "approval"):
            if include_acts or not acts(tools.get(r["id"], [])):
                ok.append(_case(r, tools.get(r["id"], []), "ok"))
    return ok[:n_ok] + hard[:n_hard]


def save(cases, directory=None, stamp=None):
    d = Path(directory) if directory else replay_dir() / "datasets"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"dataset-{stamp or time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    return path


def load(path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def prune(directory=None, keep=6):
    d = Path(directory) if directory else replay_dir() / "datasets"
    files = sorted(d.glob("dataset-*.jsonl"))
    for f in files[:-keep]:
        try:
            f.unlink()
        except OSError:
            pass


def counts(cases):
    out = {}
    for c in cases:
        out[c["category"]] = out.get(c["category"], 0) + 1
    return out


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    cs = build(n_ok=n)
    print(f"{len(cs)} cases: {counts(cs)}")
    print(save(cs))
