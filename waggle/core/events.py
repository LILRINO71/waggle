"""The append-only event log in runtime.db.

Every state change someone may later ask about lands here (job retried or dead, outbox failed or expired, a
timer fired late or skipped, recovery actions, capability checks). The database refuses updates, and deletes
of events younger than the retention period (see store.MIGRATIONS v2); `metrics.rollup_events` is the only
thing that removes events, after counting them into metrics_daily.
"""
from waggle.core import store


def emit(kind, *, source="", chat="", ref="", data=None, now=None):
    """Append one event; returns its id. Joins the caller's transaction if one is open."""
    if not kind:
        raise ValueError("event kind is required")
    with store.transaction() as con:
        return store.insert_event(con, kind, source=source, chat=chat, ref=ref, data=data, now=now)


def query(kind=None, since=None, chat=None, ref=None, limit=200):
    """Events in time order (oldest first). With more matches than `limit`, the most recent `limit` are kept.

    `kind` matches exactly, or as a prefix when it ends in '*' ("job.*")."""
    where, args = [], []
    if kind:
        if kind.endswith("*"):
            prefix = kind[:-1]
            where.append("substr(kind, 1, ?) = ?")
            args += [len(prefix), prefix]
        else:
            where.append("kind = ?")
            args.append(kind)
    if since is not None:
        where.append("ts >= ?")
        args.append(float(since))
    if chat is not None:
        where.append("chat = ?")
        args.append(str(chat))
    if ref is not None:
        where.append("ref = ?")
        args.append(str(ref))
    sql = "SELECT rowid AS seq, * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, seq DESC LIMIT ?"
    args.append(int(limit))
    rows = store.connect().execute(sql, args).fetchall()
    return [_row(r) for r in reversed(rows)]


def _row(r):
    return {"id": r["id"], "ts": r["ts"], "kind": r["kind"], "source": r["source"], "chat": r["chat"],
            "ref": r["ref"], "data": store.loads(r["data"])}
