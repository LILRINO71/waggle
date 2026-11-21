"""The one outbox in runtime.db: every outgoing message is a row here before any transport sees it.

States: queued -> sending -> sent | queued (retry) | failed | expired. A row becomes `sent` only with the
provider's message id. A row stuck in `sending` for SENDING_STALE_S (the sender crashed mid-send) goes back to
`queued`: a possible duplicate is preferred over a lost message. Retries back off exponentially up to
RETRY_CAP_S; `expires` is honoured; the same idem_key never creates a second row.
"""
from waggle.core import store

KINDS = ("text", "voice", "file", "push", "call")
SENDING_STALE_S = 120.0
RETRY_BASE_S = 15.0
RETRY_CAP_S = 900.0
MAX_ATTEMPTS = 8


def retry_delay(attempts):
    return min(RETRY_BASE_S * 2 ** max(attempts - 1, 0), RETRY_CAP_S)


def enqueue(kind, body="", *, chat="", file=None, idem_key=None, priority=0, origin="", not_before=None,
            expires=None, meta=None, now=None):
    """Queue a message; with an idem_key already used, returns the existing row's id and changes nothing."""
    if kind not in KINDS:
        raise ValueError(f"outbox kind must be one of {KINDS}, not {kind!r}")
    now = store.now_or(now)
    with store.transaction() as con:
        if idem_key is not None:
            row = con.execute("SELECT id FROM outbox WHERE idem_key=?", (idem_key,)).fetchone()
            if row:
                return row[0]
        cur = con.execute(
            "INSERT INTO outbox(idem_key, chat, kind, body, file, priority, origin, not_before, expires, meta, "
            "state, attempts, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,'queued',0,?,?)",
            (idem_key, str(chat or ""), kind, body or "", None if file is None else str(file), int(priority),
             origin or "", None if not_before is None else float(not_before),
             None if expires is None else float(expires), store.dumps(meta), now, now))
        return cur.lastrowid


def _due_sql(kinds):
    sql = ("SELECT id FROM outbox WHERE state='queued' AND (not_before IS NULL OR not_before<=?) "
           "AND (next_try IS NULL OR next_try<=?) AND (expires IS NULL OR expires>?)")
    if kinds is not None:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
    return sql + " ORDER BY priority DESC, id ASC LIMIT ?"


def claim_due(now=None, limit=10, *, kinds=None):
    """Move up to `limit` due rows (only of `kinds`, if given) to `sending` (attempts+1) and return them,
    highest priority first."""
    now = store.now_or(now)
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else [k for k in kinds]
        if not kinds:
            return []
    with store.transaction() as con:
        rows = con.execute(_due_sql(kinds), (now, now, now, *(kinds or ()), int(limit))).fetchall()
        ids = [r[0] for r in rows]
        for oid in ids:
            con.execute("UPDATE outbox SET state='sending', attempts=attempts+1, updated=? "
                        "WHERE id=? AND state='queued'", (now, oid))
        return [get(oid) for oid in ids]


def mark_sent(id, provider_msg_id, *, now=None):  # noqa: A002 - the public API name
    """Record a confirmed delivery. Requires the provider's message id; a row is never sent twice."""
    if provider_msg_id is None or not str(provider_msg_id).strip():
        raise ValueError("a message is only 'sent' with the provider's message id")
    now = store.now_or(now)
    with store.transaction() as con:
        row = con.execute("SELECT state, chat, kind, origin FROM outbox WHERE id=?", (id,)).fetchone()
        if row is None or row["state"] == "sent":
            return False
        con.execute("UPDATE outbox SET state='sent', provider_msg_id=?, sent_at=?, updated=? WHERE id=?",
                    (str(provider_msg_id), now, now, id))
        store.insert_event(con, "outbox.sent", source="outbox", chat=row["chat"], ref=id, now=now,
                           data={"kind": row["kind"], "origin": row["origin"], "provider_msg_id": str(provider_msg_id),
                                 "was": row["state"]})
        return True


def mark_failed(id, error, *, permanent=False, retry_after=None, now=None):  # noqa: A002 - the public API name
    """Record a failed send of a `sending` row. Returns the new state (queued, failed or expired), or None
    when the row is not being sent. `retry_after` (seconds, e.g. from the provider) replaces the backoff."""
    now = store.now_or(now)
    with store.transaction() as con:
        row = con.execute("SELECT * FROM outbox WHERE id=? AND state='sending'", (id,)).fetchone()
        if row is None:
            return None
        data = {"kind": row["kind"], "origin": row["origin"], "attempts": row["attempts"], "error": str(error)}
        if permanent or row["attempts"] >= MAX_ATTEMPTS:
            state, next_try, kind = "failed", None, "outbox.failed"
            data["permanent"] = bool(permanent)
        else:
            delay = retry_delay(row["attempts"]) if retry_after is None else max(0.0, float(retry_after))
            next_try = now + delay
            if row["expires"] is not None and next_try >= row["expires"]:
                state, kind = "expired", "outbox.expired"
            else:
                state, kind = "queued", "outbox.retry"
                data["next_try"] = next_try
        con.execute("UPDATE outbox SET state=?, next_try=?, last_error=?, updated=? WHERE id=?",
                    (state, next_try, str(error), now, id))
        store.insert_event(con, kind, source="outbox", chat=row["chat"], ref=id, data=data, now=now)
        return state


def recover(now=None):
    """Rows left in `sending` for more than SENDING_STALE_S go back to `queued`. Returns how many."""
    now = store.now_or(now)
    with store.transaction() as con:
        rows = con.execute("SELECT id, chat, attempts FROM outbox WHERE state='sending' AND updated<?",
                           (now - SENDING_STALE_S,)).fetchall()
        for r in rows:
            con.execute("UPDATE outbox SET state='queued', next_try=?, updated=? WHERE id=? AND state='sending'",
                        (now, now, r["id"]))
            store.insert_event(con, "outbox.recovered", source="outbox", chat=r["chat"], ref=r["id"],
                               data={"attempts": r["attempts"]}, now=now)
        return len(rows)


def expire(now=None):
    """Queued rows past their `expires` become `expired`. Returns how many."""
    now = store.now_or(now)
    with store.transaction() as con:
        rows = con.execute("SELECT id, chat, kind, origin, attempts FROM outbox WHERE state='queued' "
                           "AND expires IS NOT NULL AND expires<=?", (now,)).fetchall()
        for r in rows:
            con.execute("UPDATE outbox SET state='expired', updated=? WHERE id=?", (now, r["id"]))
            store.insert_event(con, "outbox.expired", source="outbox", chat=r["chat"], ref=r["id"], now=now,
                               data={"kind": r["kind"], "origin": r["origin"], "attempts": r["attempts"]})
        return len(rows)


def prune(older_than_s, now=None):
    """Delete sent and expired rows last touched more than `older_than_s` ago. Returns how many."""
    cutoff = store.now_or(now) - older_than_s
    with store.transaction() as con:
        return con.execute("DELETE FROM outbox WHERE state IN ('sent', 'expired') "
                           "AND COALESCE(sent_at, updated, created) < ?", (cutoff,)).rowcount


def get_by_key(idem_key):
    """The row queued with this idempotency key, or None."""
    row = store.connect().execute("SELECT * FROM outbox WHERE idem_key=?", (idem_key,)).fetchone()
    return _row(row) if row else None


def get(id):  # noqa: A002 - the public API name
    row = store.connect().execute("SELECT * FROM outbox WHERE id=?", (id,)).fetchone()
    return _row(row) if row else None


def list(state=None, chat=None, limit=100):  # noqa: A001 - the public API name
    """Most recent rows first."""
    where, args = [], []
    if state is not None:
        where.append("state=?")
        args.append(state)
    if chat is not None:
        where.append("chat=?")
        args.append(str(chat))
    sql = "SELECT * FROM outbox" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [_row(r) for r in store.connect().execute(sql, args).fetchall()]


def _row(r):
    d = dict(r)
    d["meta"] = store.loads(d.get("meta")) or {}
    return d

