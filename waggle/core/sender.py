"""Delivery of outbox rows through registered transports, one per row kind.

Transport contract: `fn(row) -> provider message id (str)`. Any exception is a failed attempt; an exception
with `permanent = True` fails the row for good; one with `retry_after` (seconds) sets the next try. A transport
that returns no id has not proven delivery, so that also counts as a failed attempt.

Rows of a kind with no transport stay queued; each such row gets exactly one `outbox.no_transport` event.
"""
import logging

from waggle.core import events, outbox, store

log = logging.getLogger("waggle.core.sender")
_transports = {}
_noticed = set()


def register(kind, fn):
    if kind not in outbox.KINDS:
        raise ValueError(f"outbox kind must be one of {outbox.KINDS}, not {kind!r}")
    _transports[kind] = fn


def kinds():
    return sorted(_transports)


def send_one(now=None):
    """Claim the next due row that has a transport and send it. Returns the row's final state, or None when
    nothing was due."""
    rows = outbox.claim_due(now=now, limit=1, kinds=kinds())
    if not rows:
        return None
    return deliver(rows[0], now=now)


def deliver(row, now=None):
    fn = _transports.get(row["kind"])
    try:
        msg_id = fn(row)
    except Exception as e:  # every transport error is recorded on the row, never raised into the loop
        log.warning("send %s failed: %s", row["id"], e)
        return outbox.mark_failed(row["id"], f"{type(e).__name__}: {e}", permanent=bool(getattr(e, "permanent", False)),
                                  retry_after=getattr(e, "retry_after", None), now=now)
    if msg_id is None or not str(msg_id).strip():
        return outbox.mark_failed(row["id"], "transport returned no message id", now=now)
    outbox.mark_sent(row["id"], str(msg_id), now=now)
    return "sent"


def notice_no_transport(now=None, limit=100):
    """One `outbox.no_transport` event per queued row whose kind has no transport. Returns how many new."""
    have = kinds()
    sql = "SELECT id, kind, chat, origin FROM outbox WHERE state='queued'"
    if have:
        sql += f" AND kind NOT IN ({','.join('?' * len(have))})"
    rows = store.connect().execute(sql + " ORDER BY id LIMIT ?", (*have, int(limit))).fetchall()
    new = 0
    for r in rows:
        key = (str(store.db_path()), r["id"])
        if key in _noticed:
            continue
        _noticed.add(key)
        if events.query("outbox.no_transport", ref=r["id"], limit=1):
            continue
        events.emit("outbox.no_transport", source="sender", chat=r["chat"], ref=r["id"], now=now,
                    data={"kind": r["kind"], "origin": r["origin"]})
        new += 1
    return new
