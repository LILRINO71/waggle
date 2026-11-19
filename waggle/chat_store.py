"""The turn engine's state on disk (~/.waggle/chat.db), so any process can pick up where another stopped.

- chats:  one row per conversation: its Claude session id (resumed after a restart), how many turns that session
          has, and the transport address the adapter needs to reach the chat again (for Hermes: the session key).
- outbox: replies and background results waiting for delivery. A result is written here BEFORE anyone tries to
          send it, and only marked delivered when a transport confirms it, so nothing is lost when a process dies.
- jobs:   background work started from a chat (waggle/chat_jobs.py): who asked, what, status, result.
"""
import json
import sqlite3
import time
import uuid

from .config import data_path


def _db():
    con = sqlite3.connect(data_path("chat.db"), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS chats (
        chat TEXT PRIMARY KEY, platform TEXT, address TEXT, claude_session TEXT, session_turns INTEGER DEFAULT 0,
        model TEXT, created REAL, updated REAL, last_user REAL, prev_user REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY, chat TEXT, kind TEXT, text TEXT, file TEXT, created REAL, state TEXT,
        attempts INTEGER DEFAULT 0, last_error TEXT, delivered REAL, via TEXT, source TEXT, claimed REAL)""")
    con.execute("CREATE INDEX IF NOT EXISTS outbox_state ON outbox(state, created)")
    con.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, chat TEXT, task TEXT, agent TEXT, hard INTEGER, status TEXT, created REAL,
        started REAL, finished REAL, pid INTEGER, result TEXT, error TEXT, trace_id INTEGER)""")
    return con


# ---- chats ---------------------------------------------------------------------------------------------------

def get_chat(chat):
    con = _db()
    row = con.execute("SELECT * FROM chats WHERE chat=?", (chat,)).fetchone()
    con.close()
    return dict(row) if row else None


def touch_chat(chat, platform="", address=None, user=False):
    """Create or update a chat row; `address` is how the transport reaches it again (kept when None)."""
    now = time.time()
    con = _db()
    con.execute("INSERT OR IGNORE INTO chats (chat, platform, address, created, updated) VALUES (?,?,?,?,?)",
                (chat, platform or "", address or "", now, now))
    con.execute("UPDATE chats SET updated=?, platform=COALESCE(NULLIF(?, ''), platform) WHERE chat=?",
                (now, platform or "", chat))
    if address:
        con.execute("UPDATE chats SET address=? WHERE chat=?", (address, chat))
    if user:
        con.execute("UPDATE chats SET prev_user=last_user, last_user=? WHERE chat=?", (now, chat))
    con.commit()
    con.close()


def set_session(chat, claude_session, turns=None, model=None):
    con = _db()
    con.execute("INSERT OR IGNORE INTO chats (chat, created, updated) VALUES (?,?,?)", (chat, time.time(), time.time()))
    con.execute("UPDATE chats SET claude_session=?, updated=? WHERE chat=?", (claude_session or "", time.time(), chat))
    if turns is not None:
        con.execute("UPDATE chats SET session_turns=? WHERE chat=?", (int(turns), chat))
    if model:
        con.execute("UPDATE chats SET model=? WHERE chat=?", (model, chat))
    con.commit()
    con.close()


def chats_with_address():
    con = _db()
    rows = [dict(r) for r in con.execute("SELECT * FROM chats WHERE address IS NOT NULL AND address != ''")]
    con.close()
    return rows


# ---- outbox --------------------------------------------------------------------------------------------------

def enqueue(chat, text, kind="reply", file="", source=""):
    """Store something the user must receive; returns its id. It stays 'pending' until delivered."""
    oid = uuid.uuid4().hex[:12]
    con = _db()
    con.execute("INSERT INTO outbox (id, chat, kind, text, file, created, state, source) VALUES (?,?,?,?,?,?,?,?)",
                (oid, chat or "", kind, text or "", file or "", time.time(), "pending", source or ""))
    con.commit()
    con.close()
    return oid


def pending(chat=None, older_than=0.0):
    con = _db()
    q, params = "SELECT * FROM outbox WHERE state='pending' AND created<=?", [time.time() - older_than]
    if chat is not None:
        q += " AND chat=?"
        params.append(chat)
    rows = [dict(r) for r in con.execute(q + " ORDER BY created", params)]
    con.close()
    return rows


def claim(oid):
    """Take one item for delivery (so two processes never send it twice). True if this caller got it."""
    con = _db()
    cur = con.execute("UPDATE outbox SET state='sending', attempts=attempts+1, claimed=? WHERE id=? AND state='pending'",
                      (time.time(), oid))
    con.commit()
    con.close()
    return cur.rowcount == 1


def mark(oid, delivered, via="", error=""):
    con = _db()
    if delivered:
        con.execute("UPDATE outbox SET state='delivered', delivered=?, via=? WHERE id=?", (time.time(), via, oid))
    else:
        con.execute("UPDATE outbox SET state='pending', last_error=? WHERE id=?", (str(error)[:300], oid))
    con.commit()
    con.close()


def delivered_since(chat, since):
    con = _db()
    rows = [dict(r) for r in con.execute("SELECT * FROM outbox WHERE chat=? AND state='delivered' AND delivered>=? "
                                         "ORDER BY delivered", (chat, since))]
    con.close()
    return rows


def get_item(oid):
    con = _db()
    row = con.execute("SELECT * FROM outbox WHERE id=?", (oid,)).fetchone()
    con.close()
    return dict(row) if row else None


def release_stuck(older_than=300):
    """Items left 'sending' by a process that died mid-delivery go back to pending."""
    con = _db()
    con.execute("UPDATE outbox SET state='pending' WHERE state='sending' AND claimed<?", (time.time() - older_than,))
    con.commit()
    con.close()


def outbox_stats(since=0.0):
    con = _db()
    rows = con.execute("SELECT state, COUNT(*) n FROM outbox WHERE created>=? GROUP BY state", (since,)).fetchall()
    con.close()
    return {r["state"]: r["n"] for r in rows}


# ---- jobs ----------------------------------------------------------------------------------------------------

def new_job(chat, task, agent="general", hard=False):
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    con = _db()
    con.execute("INSERT INTO jobs (id, chat, task, agent, hard, status, created) VALUES (?,?,?,?,?,?,?)",
                (jid, chat or "", task, agent or "general", int(bool(hard)), "queued", time.time()))
    con.commit()
    con.close()
    return jid


def update_job(jid, **fields):
    if not fields:
        return
    con = _db()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*[json.dumps(v) if isinstance(v, (dict, list)) else v
                                                        for v in fields.values()], jid))
    con.commit()
    con.close()


def get_job(jid):
    con = _db()
    row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    con.close()
    return dict(row) if row else None


def jobs(chat=None, limit=10):
    con = _db()
    q, params = "SELECT * FROM jobs", []
    if chat:
        q, params = q + " WHERE chat=?", [chat]
    rows = [dict(r) for r in con.execute(q + " ORDER BY created DESC LIMIT ?", (*params, limit))]
    con.close()
    return rows
