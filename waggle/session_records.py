"""Session records for autonomous runs: what each run was, how it ended, whether it did real work, how many commits.

Every autonomy run (autowork.run_once, once it has picked a goal) and every objective step run (objectives.run_step,
for objectives that are not autonomy's own) leaves one row in session_records.db. A run is "real" only when it ran and
code proved it (autonomy outcome proven, objective step done); anything else (held back by the gate, refused by the
budget, outside the charter, nothing provable chosen, ran but not proven, failed) is a "noop". When the last 4 runs were
all noops the evening note says so in one line (evening_line), so a loop that spins without producing is visible.
Borrowed from gptme-agent-template's post-session records (open-source survey, borrow-list row 15).

    python -m waggle.session_records [list|status]

Off switch: config `session_records_enabled` false. Removal: delete this file and tests/test_session_records.py, and
the `session_records` hooks in waggle/autowork.py, waggle/objectives.py and waggle/brief.py (each in a try block).
"""
import sqlite3
import sys
import time
from collections import Counter

from .config import Settings, data_path

STREAK = 4
NOOP_WORDS = {"gated": "held back by the gate", "refused": "refused by the budget", "charter_refused": "outside its charter",
              "failed": "failed or found nothing provable", "unproven": "ran but was not proven",
              "needs_approval": "waited for an approval", "skipped": "skipped"}


def enabled(settings=None):
    return bool(getattr(settings or Settings.load(), "session_records_enabled", True))


def _db():
    con = sqlite3.connect(data_path("session_records.db"), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, kind TEXT,
        ref TEXT, title TEXT, outcome TEXT, work TEXT, commits INTEGER, detail TEXT)""")
    return con


def record(kind, ref, outcome, *, title="", commits=None, detail="", real=None, now=None, settings=None):
    """Store one run. `real` None means: real exactly when the outcome is proven or done. Never raises."""
    try:
        if not enabled(settings):
            return None
        if real is None:
            real = outcome in ("proven", "done")
        con = _db()
        try:
            cur = con.execute("INSERT INTO runs(time, kind, ref, title, outcome, work, commits, detail) VALUES(?,?,?,?,?,?,?,?)",
                              (time.time() if now is None else now, kind, str(ref or ""), str(title)[:160], outcome,
                               "real" if real else "noop", commits, str(detail)[:300]))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()
    except Exception:  # noqa: BLE001  (a record problem must never stop the run it describes)
        return None


def recent(n=STREAK, kind=None):
    con = _db()
    try:
        q, args = "SELECT * FROM runs", []
        if kind:
            q, args = q + " WHERE kind=?", [kind]
        return [dict(r) for r in con.execute(q + " ORDER BY id DESC LIMIT ?", [*args, n])]
    finally:
        con.close()


def evening_line(now=None, settings=None):
    """One line when the last STREAK runs all did nothing useful (and the newest is under 3 days old), else ''."""
    try:
        if not enabled(settings):
            return ""
        now = time.time() if now is None else now
        rows = recent(STREAK)
        if len(rows) < STREAK or any(r["work"] == "real" for r in rows) or now - rows[0]["time"] > 3 * 86400:
            return ""
        why = Counter(NOOP_WORDS.get(r["outcome"], r["outcome"]) for r in rows)
        return (f"Autonomy did nothing useful in its last {STREAK} runs ("
                + ", ".join(f"{n} {w}" for w, n in why.most_common()) + "). Records: python -m waggle.session_records.")
    except Exception:  # noqa: BLE001
        return ""


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    rows = recent(15)
    print(f"session records {'on' if enabled() else 'OFF'}; {len(rows)} most recent run(s), newest first")
    for r in rows:
        when = time.strftime("%a %H:%M", time.localtime(r["time"]))
        c = "" if r["commits"] is None else f", {r['commits']} commit(s)"
        print(f"- {when} {r['kind']} {r['ref']}: {r['outcome']} -> {r['work']}{c}  {r['title']}")
    print(evening_line() or "no streak of useless runs")
    return 0 if a[:1] in ([], ["list"], ["status"]) else 2


if __name__ == "__main__":
    sys.exit(main())
