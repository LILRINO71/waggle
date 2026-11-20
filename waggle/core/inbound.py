"""Inbound messages: the inbox in runtime.db and the one handler the core runs them through.

`submit` stores a message (one inbox row and one `inbox.received` event, in one transaction); the core runs
waiting rows through the registered handler, one at a time per chat in arrival order, different chats in
parallel, on threads of their own (never the job workers').

The handler is called as `fn(msg)` with:
  turn_id, chat (chat key), address (Telegram chat id as a string), text, kind, media, platform, origin,
  received_ts, attempt (1 on the first run, 2 after a restart interrupted the first)
It is responsible for replying. Returning ends the turn (`done`); an exception marks the row `failed`.

A row still `running` when its core died is run again once (attempt 2). If that run dies too, the row is
`failed`, an `inbox.lost` event is recorded and one honest text message is queued for that chat asking the
user to send it again.

States: waiting -> running -> done | failed.
"""
import os
import threading

from waggle.core import procs, store

MAX_ATTEMPTS = 2
LOST_TEXT = ("Sorry, I lost your message{quote} in a restart before I could answer it. "
             "Please send it again.")

_handler = None
_wake = threading.Event()


def register(fn):
    """Set the one inbound handler (a later call replaces it)."""
    global _handler
    _handler = fn


def handler():
    return _handler


def wait_for_work(timeout):
    """Block until a message was submitted in this process or `timeout` passes (used by the core's loop)."""
    woke = _wake.wait(timeout)
    _wake.clear()
    return woke


def submit(chat, text, *, address="", kind="text", media=None, platform="telegram", origin="user",
           dedupe_key=None, now=None):
    """Store one inbound message. With a dedupe_key already used, nothing is stored and the existing turn is
    reported as a duplicate. Returns {"turn_id", "duplicate"}."""
    chat = "" if chat is None else str(chat).strip()
    if not chat:
        raise ValueError("chat is required")
    now = store.now_or(now)
    dedupe_key = None if dedupe_key in (None, "") else str(dedupe_key)
    with store.transaction() as con:
        if dedupe_key is not None:
            row = con.execute("SELECT turn_id FROM inbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            if row:
                return {"turn_id": row[0], "duplicate": True}
        turn_id = store.new_id()
        con.execute("INSERT INTO inbox(turn_id, dedupe_key, chat, address, platform, kind, text, media, origin, "
                    "state, attempts, received_ts, updated) VALUES(?,?,?,?,?,?,?,?,?,'waiting',0,?,?)",
                    (turn_id, dedupe_key, chat, str(address or ""), platform or "", kind or "text", text or "",
                     store.dumps(media), origin or "", now, now))
        store.insert_event(con, "inbox.received", source="inbound", chat=chat, ref=turn_id, now=now,
                           data={"kind": kind, "platform": platform, "origin": origin, "dedupe_key": dedupe_key,
                                 "chars": len(text or "")})
    _wake.set()
    return {"turn_id": turn_id, "duplicate": False}


def message(row):
    """The dict a handler receives for an inbox row."""
    return {"turn_id": row["turn_id"], "chat": row["chat"], "address": row["address"], "text": row["text"],
            "kind": row["kind"], "media": store.loads(row["media"]), "platform": row["platform"],
            "origin": row["origin"], "received_ts": row["received_ts"], "attempt": row["attempts"]}


def waiting_chats(limit=100):
    """Chats with waiting messages, the one waiting longest first."""
    rows = store.connect().execute("SELECT chat, MIN(id) AS first FROM inbox WHERE state='waiting' "
                                   "GROUP BY chat ORDER BY first LIMIT ?", (int(limit),)).fetchall()
    return [r["chat"] for r in rows]


def has_waiting(chat):
    return store.connect().execute("SELECT 1 FROM inbox WHERE chat=? AND state='waiting' LIMIT 1",
                                   (chat,)).fetchone() is not None


def claim_next(chat, now=None):
    """Take the chat's oldest waiting message for this process; None when there is none."""
    now = store.now_or(now)
    with store.transaction() as con:
        row = con.execute("SELECT id FROM inbox WHERE chat=? AND state='waiting' ORDER BY id LIMIT 1",
                          (chat,)).fetchone()
        if row is None:
            return None
        con.execute("UPDATE inbox SET state='running', attempts=attempts+1, started=?, pid=?, pid_ctime=?, "
                    "updated=? WHERE id=? AND state='waiting'",
                    (now, os.getpid(), procs.own_ctime(), now, row["id"]))
        return con.execute("SELECT * FROM inbox WHERE id=?", (row["id"],)).fetchone()


def finish(turn_id, error=None, now=None):
    """Record the end of a run: `done`, or `failed` with the error."""
    now = store.now_or(now)
    state = "done" if error is None else "failed"
    with store.transaction() as con:
        row = con.execute("SELECT chat, attempts FROM inbox WHERE turn_id=? AND state='running'",
                          (turn_id,)).fetchone()
        if row is None:
            return False
        con.execute("UPDATE inbox SET state=?, error=?, finished=?, updated=? WHERE turn_id=?",
                    (state, error, now, now, turn_id))
        store.insert_event(con, f"inbox.{state}", source="inbound", chat=row["chat"], ref=turn_id, now=now,
                           data={"attempt": row["attempts"], **({"error": error} if error else {})})
        return True


def run(row, now=None):
    """Run one claimed row through the handler and record the outcome. Never raises."""
    fn = _handler
    if fn is None:
        return finish(row["turn_id"], "no inbound handler registered", now=now)
    try:
        fn(message(row))
    except Exception as e:  # the handler's failure belongs to this message, not to the loop
        return finish(row["turn_id"], f"{type(e).__name__}: {e}"[:2000], now=now)
    return finish(row["turn_id"], now=now)


def recover(now=None):
    """Settle rows whose runner process is gone: run once more (attempt 2), or fail with an honest message
    to the chat when the second run died too. Returns {"retried": n, "lost": n}."""
    from waggle.core import outbox
    now = store.now_or(now)
    counts = {"retried": 0, "lost": 0}
    with store.transaction() as con:
        for row in con.execute("SELECT * FROM inbox WHERE state='running'").fetchall():
            if procs.same_process(row["pid"], row["pid_ctime"]):
                continue
            if row["attempts"] < MAX_ATTEMPTS:
                con.execute("UPDATE inbox SET state='waiting', pid=NULL, pid_ctime=NULL, updated=? WHERE id=?",
                            (now, row["id"]))
                store.insert_event(con, "inbox.retried", source="inbound", chat=row["chat"], ref=row["turn_id"],
                                   now=now, data={"attempt": row["attempts"] + 1, "reason": "runner died"})
                counts["retried"] += 1
                continue
            error = f"the core died during attempt {row['attempts']} as well"
            con.execute("UPDATE inbox SET state='failed', error=?, finished=?, updated=? WHERE id=?",
                        (error, now, now, row["id"]))
            reply_id = outbox.enqueue("text", lost_text(row["text"]), chat=row["address"] or row["chat"],
                                      idem_key=f"inbox-lost:{row['turn_id']}", priority=5, origin="inbound",
                                      meta={"turn_id": row["turn_id"], "chat_key": row["chat"]}, now=now)
            store.insert_event(con, "inbox.lost", source="inbound", chat=row["chat"], ref=row["turn_id"], now=now,
                               data={"attempts": row["attempts"], "outbox_id": reply_id})
            counts["lost"] += 1
    if counts["retried"]:
        _wake.set()
    return counts


def lost_text(text):
    text = " ".join((text or "").split())
    if not text:
        return LOST_TEXT.format(quote="")
    quote = text if len(text) <= 60 else text[:57].rstrip() + "..."
    return LOST_TEXT.format(quote=f' ("{quote}")')


def get(turn_id):
    row = store.connect().execute("SELECT * FROM inbox WHERE turn_id=?", (turn_id,)).fetchone()
    return _row(row) if row else None


def list(state=None, chat=None, limit=100):  # noqa: A001 - the public API name
    """Most recent messages first."""
    where, args = [], []
    if state is not None:
        where.append("state=?")
        args.append(state)
    if chat is not None:
        where.append("chat=?")
        args.append(str(chat))
    sql = "SELECT * FROM inbox" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [_row(r) for r in store.connect().execute(sql, args).fetchall()]


def counts(now=None):
    """Waiting and running counts and the age of the oldest waiting message (for /health)."""
    now = store.now_or(now)
    con = store.connect()
    waiting, oldest = con.execute("SELECT COUNT(*), MIN(received_ts) FROM inbox WHERE state='waiting'").fetchone()
    running = con.execute("SELECT COUNT(*) FROM inbox WHERE state='running'").fetchone()[0]
    return {"waiting": waiting, "running": running,
            "oldest_waiting_age_s": None if oldest is None else max(0.0, now - oldest)}


def _row(r):
    d = dict(r)
    d["media"] = store.loads(d["media"])
    return d


