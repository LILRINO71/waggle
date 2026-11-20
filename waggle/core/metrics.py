"""Daily counters and gauges in runtime.db (metrics_daily), and the event retention roll-up."""
import builtins
import time
from datetime import datetime

from waggle.core import store

EVENT_RETENTION_S = 90 * 86400


def day_of(ts=None):
    """Local calendar day (YYYY-MM-DD) of an epoch time; today when ts is None."""
    return datetime.fromtimestamp(store.now_or(ts)).strftime("%Y-%m-%d")


def incr(name, n=1, *, day=None):
    with store.transaction() as con:
        con.execute("INSERT INTO metrics_daily(day, name, value) VALUES(?,?,?) "
                    "ON CONFLICT(day, name) DO UPDATE SET value = value + excluded.value",
                    (day or day_of(), name, float(n)))


def set(name, value, *, day=None):  # noqa: A001 - the public API name
    with store.transaction() as con:
        con.execute("INSERT INTO metrics_daily(day, name, value) VALUES(?,?,?) "
                    "ON CONFLICT(day, name) DO UPDATE SET value = excluded.value",
                    (day or day_of(), name, float(value)))


def get(day, name):
    """The value for (day, name), or None when nothing was recorded."""
    row = store.connect().execute("SELECT value FROM metrics_daily WHERE day=? AND name=?",
                                  (day, name)).fetchone()
    return row[0] if row else None


def for_day(day_str):
    """All metrics of one day as {name: value}."""
    rows = store.connect().execute("SELECT name, value FROM metrics_daily WHERE day=? ORDER BY name",
                                   (day_str,)).fetchall()
    return {r[0]: r[1] for r in rows}


def rollup_events(now=None):
    """Count events older than 90 days into metrics_daily as `events.<kind>` per local day, then delete them.

    The cut-off never passes the real clock's 90 days, because the database refuses to delete younger events."""
    cutoff = min(store.now_or(now), time.time()) - EVENT_RETENTION_S
    with store.transaction() as con:
        rows = con.execute("SELECT ts, kind FROM events WHERE ts < ?", (cutoff,)).fetchall()
        if not rows:
            return {"events": 0, "days": 0}
        counts = {}
        for r in rows:
            key = (day_of(r["ts"]), "events." + r["kind"])
            counts[key] = counts.get(key, 0) + 1
        con.executemany("INSERT INTO metrics_daily(day, name, value) VALUES(?,?,?) "
                        "ON CONFLICT(day, name) DO UPDATE SET value = value + excluded.value",
                        [(d, n, float(c)) for (d, n), c in counts.items()])
        con.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        days = builtins.set(d for d, _ in counts)
        store.insert_event(con, "events.rollup", source="metrics",
                           data={"events": len(rows), "days": len(days), "cutoff": cutoff})
        return {"events": len(rows), "days": len(days)}
