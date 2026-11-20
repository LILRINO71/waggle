"""Who started this? Tells the user's own activity (Hermes session source telegram / local) apart from developer
one-shots and tests (oneshot / cli / tool), so test objectives and dev sessions never buzz the user's phone.

Decision 10: only the user's own channels may feed what Waggle decides to say. user_turns() is the one filter the
outreach code applies wherever it reads conversation history (follow-ups, the decision step's context, the
automation scout, response tracking): a turn counts only when it came in on one of the user's platforms
(followup_platforms), was not run by Waggle itself (objective-/routine-/agent- sessions), carries no non-user
origin, and its Hermes session (when Hermes knows it) was not started by a test, bench, the CLI or an agent.

Hermes records every session's source in its state.db (read-only here). Work like an objective is attributed to
the session whose user message came last before it was created (within a few minutes): that is how the 2026-11-19
audit matched every test objective to a `hermes -z` one-shot within 2 s.
"""
import json
import os
import sqlite3
from pathlib import Path


def state_db():
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state.db"


NON_USER = ("test", "bench", "canary", "agent", "cli", "oneshot", "tool", "api", "batch")
MACHINE_SESSIONS = ("objective-", "routine-", "agent-", "bench", "test", "canary")


def session_source(session_id, db=None):
    """The Hermes source of one session (telegram, cli, oneshot, ...), or '' when unknown."""
    p = Path(db or state_db())
    if not session_id or not p.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
        row = con.execute("SELECT source FROM sessions WHERE id=?", (session_id,)).fetchone()
        con.close()
        return (row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


def is_user_turn(row, settings=None, db=None, _cache=None):
    """True only for a turn the user typed or said on one of their own channels (see the module docstring)."""
    from .config import Settings
    s = settings or Settings.load()
    if (row.get("origin") or "user") != "user":
        return False
    extra = row.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    extra = extra if isinstance(extra, dict) else {}
    if (extra.get("origin") or "user") != "user":
        return False
    if (row.get("platform") or "") not in s.followup_platforms:
        return False
    session = str(row.get("session") or "")
    if session.lower().startswith(MACHINE_SESSIONS):
        return False
    cache = _cache if _cache is not None else {}
    if session not in cache:
        cache[session] = session_source(session, db=db)
    src = cache[session].lower()
    return not src or src not in {x.lower() for x in (*NON_USER, *s.dev_sources)}


def user_turns(rows, settings=None, db=None):
    """The rows (traces dicts) that are the user's own turns, in their original order."""
    from .config import Settings
    s = settings or Settings.load()
    cache = {}
    return [r for r in rows if is_user_turn(r, s, db=db, _cache=cache)]


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
