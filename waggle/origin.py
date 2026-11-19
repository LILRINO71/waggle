"""Who started this? Tells the user's own activity (Hermes session source telegram / local) apart from developer
one-shots and tests (oneshot / cli / tool), so test objectives and dev sessions never buzz the user's phone.

Hermes records every session's source in its state.db (read-only here). Work like an objective is attributed to
the session whose user message came last before it was created (within a few minutes): that is how the 2026-11-19
audit matched every test objective to a `hermes -z` one-shot within 2 s.
"""
import os
import sqlite3
from pathlib import Path


def state_db():
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state.db"


def source_at(ts, window_s=300, db=None):
    """The Hermes session source of the latest user message at or just before ts, or '' when unknown."""
    p = Path(db or state_db())
    if not p.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
        row = con.execute("SELECT s.source FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.role='user' "
                          "AND m.timestamp<=? AND m.timestamp>=? ORDER BY m.timestamp DESC LIMIT 1",
                          (ts + 5, ts - window_s)).fetchone()
        con.close()
        return (row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


def objective_source(oid, db=None):
    """'oneshot' for an objective a developer one-shot created, 'telegram' for the user's, '' when unknown.
    Objectives Waggle started itself (origin scout) count as the user's."""
    try:
        from . import objectives
        con = objectives._db()
        row = con.execute("SELECT created, origin FROM objectives WHERE id=?", (oid,)).fetchone()
        con.close()
    except Exception:  # noqa: BLE001
        return ""
    if not row:
        return ""
    if row[1] == "scout":
        return "waggle"
    return source_at(row[0], db=db)
