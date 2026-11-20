"""The turn engine's per-chat state, in runtime.db's `chats` table (waggle.core.store), so any process can pick
up a conversation where another one stopped.

One row per conversation: its Claude session id (resumed after a restart), how many turns that session has had,
the model it runs on, the transport address the core needs to reach the chat again, and when the user last
wrote. Replies and background results that still have to be delivered are not kept here: they go to the core's
outbox (waggle.core.outbox) and background work to its jobs (waggle.core.jobs).
"""
from waggle.core import store


def get_chat(chat):
    row = store.connect().execute("SELECT * FROM chats WHERE chat=?", (str(chat),)).fetchone()
    return dict(row) if row else None


def touch_chat(chat, platform="", address=None, user=False, now=None):
    """Create or update a chat row; `address` is how the transport reaches it again (kept when None).
    With user=True the user just wrote: last_user moves to now. Returns the previous last_user (0.0 if none)."""
    now = store.now_or(now)
    with store.transaction() as con:
        row = con.execute("SELECT last_user FROM chats WHERE chat=?", (str(chat),)).fetchone()
        prev = (row["last_user"] if row else None) or 0.0
        if row is None:
            con.execute("INSERT INTO chats(chat, platform, address, session_turns, updated) VALUES(?,?,?,0,?)",
                        (str(chat), platform or "", address or "", now))
        con.execute("UPDATE chats SET updated=?, platform=COALESCE(NULLIF(?, ''), platform) WHERE chat=?",
                    (now, platform or "", str(chat)))
        if address:
            con.execute("UPDATE chats SET address=? WHERE chat=?", (str(address), str(chat)))
        if user:
            con.execute("UPDATE chats SET last_user=? WHERE chat=?", (now, str(chat)))
    return prev


def set_session(chat, claude_session, turns=None, model=None, now=None):
    """Store the chat's Claude session id (empty = the next start is a fresh session)."""
    now = store.now_or(now)
    with store.transaction() as con:
        con.execute("INSERT INTO chats(chat, session_turns, updated) VALUES(?,0,?) ON CONFLICT(chat) DO NOTHING",
                    (str(chat), now))
        con.execute("UPDATE chats SET claude_session=?, updated=? WHERE chat=?", (claude_session or "", now, str(chat)))
        if turns is not None:
            con.execute("UPDATE chats SET session_turns=? WHERE chat=?", (int(turns), str(chat)))
        if model:
            con.execute("UPDATE chats SET model=? WHERE chat=?", (model, str(chat)))


def chats_with_address():
    return [dict(r) for r in store.connect().execute(
        "SELECT * FROM chats WHERE address IS NOT NULL AND address != '' ORDER BY chat")]
