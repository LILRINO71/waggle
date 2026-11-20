"""The capability ledger: a capability is ready only when a recorded end-to-end success exists within its
time limit and no failure was recorded after it. Configuration being present never counts.

The table keeps the latest success and the latest failure per capability; every record also emits a
`capability.verified` or `capability.failed` event, so the full history stays in the event log."""
from waggle.core import store

DEFAULT_TTL_S = 24 * 3600.0


def record(name, ok, *, method, evidence_ref="", ttl_s=None, detail="", now=None):
    """Record one end-to-end check. `method` says how it was checked (e.g. "telegram round trip")."""
    if not name:
        raise ValueError("capability name is required")
    if not method or not str(method).strip():
        raise ValueError("a capability check must say how it was verified (method)")
    now = store.now_or(now)
    with store.transaction() as con:
        con.execute("INSERT INTO capabilities(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (name,))
        if ok:
            con.execute("UPDATE capabilities SET verified_at=?, method=?, evidence_ref=?, "
                        "ttl_s=COALESCE(?, ttl_s, ?), detail=? WHERE name=?",
                        (now, method, evidence_ref or "", None if ttl_s is None else float(ttl_s), DEFAULT_TTL_S,
                         detail or "", name))
        else:
            con.execute("UPDATE capabilities SET last_failure_at=?, last_failure=?, last_failure_method=?, "
                        "ttl_s=COALESCE(?, ttl_s) WHERE name=?",
                        (now, detail or evidence_ref or "failed", method,
                         None if ttl_s is None else float(ttl_s), name))
        store.insert_event(con, "capability.verified" if ok else "capability.failed", source="ledger", ref=name,
                           now=now, data={"method": method, "evidence_ref": evidence_ref, "detail": detail})


def _assess(row, now):
    verified_at = row["verified_at"]
    if verified_at is None:
        return False, "never verified"
    if verified_at > now:
        return False, "verified in the future"
    if row["last_failure_at"] is not None and row["last_failure_at"] >= verified_at:
        return False, "failed after the last success"
    ttl = row["ttl_s"] if row["ttl_s"] is not None else DEFAULT_TTL_S
    if now - verified_at > ttl:
        return False, "last success is older than its ttl"
    return True, "verified"


def ready(name, now=None):
    row = store.connect().execute("SELECT * FROM capabilities WHERE name=?", (name,)).fetchone()
    return row is not None and _assess(row, store.now_or(now))[0]


def status(now=None):
    """Every capability with its readiness and the reason, alphabetically."""
    now = store.now_or(now)
    out = []
    for row in store.connect().execute("SELECT * FROM capabilities ORDER BY name").fetchall():
        ok, reason = _assess(row, now)
        ttl = row["ttl_s"] if row["ttl_s"] is not None else DEFAULT_TTL_S
        d = dict(row)
        d.update(ready=ok, reason=reason, ttl_s=ttl,
                 expires_at=None if row["verified_at"] is None else row["verified_at"] + ttl)
        out.append(d)
    return out
