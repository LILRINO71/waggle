"""Timers in runtime.db: recurring (cron, interval) and one-shot, each turning into jobs when due.

The PC is shut down every night, so a timer is often found far past its run time. Each timer has a catch-up
policy for runs that were missed by more than LATE_GRACE_S:
  skip    missed runs are dropped (frequent housekeeping)
  latest  the most recent missed run fires once, if it is at most `max_late_s` late (None: any lateness)
  all     every missed run fires (one-shot reminders, follow-ups); with `max_late_s`, only runs within it
A run that is due but not late always fires. Every fired run becomes a job with the idempotency key
"timer:<id>:<scheduled_ts>" and the payload keys `scheduled_ts`, `late_s` and `timer_id`, so the handler can say
"late: the PC was off". Fired runs emit `timer.fired` or `timer.late`; dropped runs emit `timer.skipped`.

Every function takes `now` (epoch seconds) so tests run on a fake clock. Cron times are local time unless the
timer names a time zone that zoneinfo can load; without zone data (Windows needs the tzdata package) the
timer falls back to local time and a `timer.tz_fallback` event says so.
"""
import builtins
import uuid
from collections import deque
from datetime import datetime

from waggle.core import cron, jobs, store

LATE_GRACE_S = 120.0
MAX_FIRE_ALL = 50  # an 'all' timer fires at most this many missed runs in one pass; older ones are skipped
ENUM_CAP = 1000  # missed runs counted one by one; beyond this the count is reported as approximate
KINDS = ("cron", "interval", "once")
POLICIES = ("skip", "latest", "all")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _resolve_tz(name):
    """A tzinfo for `name`, or None for local time (also when zoneinfo has no data for it)."""
    if not name:
        return None
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError, missing tzdata, invalid key
        return None


def interval_seconds(spec):
    """'300', 300, '90s', '5m', '2h', '1d' -> seconds (> 0)."""
    text = str(spec).strip().lower()
    unit = _UNITS.get(text[-1:]) if text[-1:].isalpha() else 1
    if unit is None:
        raise ValueError(f"bad interval {spec!r}")
    value = float(text[:-1] if text[-1:].isalpha() else text) * unit
    if value <= 0:
        raise ValueError(f"interval must be positive: {spec!r}")
    return value


def _epoch(at):
    if isinstance(at, datetime):
        return at.timestamp()
    return float(at)


def fmt_ts(ts):
    return f"{float(ts):.3f}".rstrip("0").rstrip(".")


def _check(kind, spec, catch_up):
    if kind not in KINDS:
        raise ValueError(f"timer kind must be one of {KINDS}, not {kind!r}")
    if catch_up not in POLICIES:
        raise ValueError(f"catch_up must be one of {POLICIES}, not {catch_up!r}")
    if kind == "cron":
        cron.parse(str(spec))
        return str(spec).strip()
    if kind == "interval":
        interval_seconds(spec)
        return str(spec).strip()
    return fmt_ts(_epoch(spec))


def _first_run(kind, spec, tz, now):
    if kind == "cron":
        return cron.next_after(spec, now, _resolve_tz(tz))
    if kind == "interval":
        return now + interval_seconds(spec)
    return float(spec)


def add(timer_id, kind, spec, job_kind, payload=None, *, tz=None, catch_up="skip", max_late_s=None, origin="",
        now=None):
    """Create or update a timer. Re-adding with the same kind, spec and tz keeps its schedule and its
    enabled flag, so callers may add their timers at every start."""
    if not timer_id or not job_kind:
        raise ValueError("timer_id and job_kind are required")
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("timer payload must be a dict")
    spec = _check(kind, spec, catch_up)
    tz = tz or None
    now = store.now_or(now)
    with store.transaction() as con:
        row = con.execute("SELECT * FROM timers WHERE id=?", (timer_id,)).fetchone()
        common = (job_kind, store.dumps(payload or {}), catch_up,
                  None if max_late_s is None else float(max_late_s), origin or "", now)
        if row is not None and (row["kind"], row["spec"], row["tz"]) == (kind, spec, tz):
            con.execute("UPDATE timers SET job_kind=?, payload=?, catch_up=?, max_late_s=?, origin=?, updated=? "
                        "WHERE id=?", (*common, timer_id))
        else:
            next_run = _first_run(kind, spec, tz, now)
            if row is None:
                con.execute("INSERT INTO timers(job_kind, payload, catch_up, max_late_s, origin, updated, id, "
                            "kind, spec, tz, next_run, enabled, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)",
                            (*common, timer_id, kind, spec, tz, next_run, now))
            else:
                con.execute("UPDATE timers SET job_kind=?, payload=?, catch_up=?, max_late_s=?, origin=?, "
                            "updated=?, kind=?, spec=?, tz=?, next_run=? WHERE id=?",
                            (*common, kind, spec, tz, next_run, timer_id))
            store.insert_event(con, "timer.added" if row is None else "timer.changed", source="timers",
                               ref=timer_id, data={"kind": kind, "spec": spec, "tz": tz, "next_run": next_run,
                                                   "catch_up": catch_up}, now=now)
        if tz and _resolve_tz(tz) is None:
            store.insert_event(con, "timer.tz_fallback", source="timers", ref=timer_id,
                               data={"tz": tz, "using": "local time"}, now=now)
    return timer_id


def once(at, job_kind, payload=None, *, timer_id=None, catch_up="all", max_late_s=None, origin=""):
    """A one-shot timer at epoch `at` (or a datetime). Returns the timer id."""
    timer_id = timer_id or f"once:{uuid.uuid4().hex[:12]}"
    add(timer_id, "once", _epoch(at), job_kind, payload, catch_up=catch_up, max_late_s=max_late_s, origin=origin)
    return timer_id


def fire_due(now=None):
    """Enqueue jobs for every due timer (applying catch-up policies to late runs); returns the job ids."""
    report = _process(store.now_or(now))
    return report["fired"]


def catch_up(now=None):
    """The start-of-day pass: like fire_due, but returns a report of what was late or skipped and since when
    the scheduler last ran (`last_tick`), so the next message can say how long the PC was off."""
    now = store.now_or(now)
    last_tick = store.get_meta("timers.last_tick")
    report = _process(now)
    report["last_tick"] = float(last_tick) if last_tick is not None else None
    if report["late"] or report["skipped"]:
        with store.transaction() as con:
            store.insert_event(con, "timers.catch_up", source="timers", now=now, data={
                "last_tick": report["last_tick"], "fired": len(report["fired"]), "late": len(report["late"]),
                "skipped": sum(s["count"] for s in report["skipped"])})
    return report


def _process(now):
    report = {"now": now, "fired": [], "late": [], "skipped": []}
    due = [r[0] for r in store.connect().execute(
        "SELECT id FROM timers WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=? ORDER BY next_run",
        (now,)).fetchall()]
    for timer_id in due:
        with store.transaction() as con:
            t = con.execute("SELECT * FROM timers WHERE id=?", (timer_id,)).fetchone()
            if t is None or not t["enabled"] or t["next_run"] is None or t["next_run"] > now:
                continue
            _fire_timer(con, t, now, report)
    with store.transaction() as con:
        con.execute("INSERT INTO meta(k, v) VALUES('timers.last_tick', ?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(now),))
    return report


def _due_runs(t, now):
    """(count, tail, approx, next_run): how many runs are due at or before `now`, the latest MAX_FIRE_ALL of
    them in order, whether the count is approximate, and the first run after `now`."""
    first = t["next_run"]
    if t["kind"] == "once":
        return 1, [first], False, None
    if t["kind"] == "interval":
        iv = interval_seconds(t["spec"])
        n = int((now - first) // iv) + 1
        tail = [first + i * iv for i in range(max(0, n - MAX_FIRE_ALL), n)]
        return n, tail, False, first + n * iv
    tz = _resolve_tz(t["tz"])
    expr = cron.parse(t["spec"])
    tail, count, s = deque(maxlen=MAX_FIRE_ALL), 0, first
    while s is not None and s <= now and count < ENUM_CAP:
        tail.append(s)
        count += 1
        s = expr.next_after(s, tz)
    approx = s is not None and s <= now
    if approx:
        tail = _latest_runs(expr, tz, now) or tail
    return count, builtins.list(tail), approx, expr.next_after(now, tz)


def _latest_runs(expr, tz, now):
    """The last MAX_FIRE_ALL cron runs at or before `now`, searching back through widening windows."""
    for window in (3600, 86400, 31 * 86400, 400 * 86400):
        tail, s, n = deque(maxlen=MAX_FIRE_ALL), expr.next_after(now - window, tz), 0
        while s is not None and s <= now and n < ENUM_CAP * 10:
            tail.append(s)
            n += 1
            s = expr.next_after(s, tz)
        if tail and (s is None or s > now):
            return tail
    return None


def _fire_timer(con, t, now, report):
    count, tail, approx, next_run = _due_runs(t, now)
    last = tail[-1]
    policy, max_late = t["catch_up"], t["max_late_s"]
    if policy == "skip":
        fire = [last] if now - last <= LATE_GRACE_S else []
    elif policy == "latest":
        fire = [last] if (now - last <= LATE_GRACE_S or max_late is None or now - last <= max_late) else []
    else:
        limit = None if max_late is None else max(max_late, LATE_GRACE_S)
        fire = [s for s in tail if limit is None or now - s <= limit]
    base = store.loads(t["payload"]) or {}
    job_id = None
    for s in fire:
        late_s = round(now - s, 3)
        job_id = jobs.enqueue(t["job_kind"], {**base, "scheduled_ts": s, "late_s": late_s, "timer_id": t["id"]},
                              idem_key=f"timer:{t['id']}:{fmt_ts(s)}", now=now)
        late = late_s > LATE_GRACE_S
        store.insert_event(con, "timer.late" if late else "timer.fired", source="timers", ref=t["id"], now=now,
                           data={"scheduled_ts": s, "late_s": late_s, "job_id": job_id, "policy": policy})
        report["fired"].append(job_id)
        if late:
            report["late"].append({"timer_id": t["id"], "scheduled_ts": s, "late_s": late_s, "job_id": job_id})
    skipped = count - len(fire)
    if skipped > 0:
        not_fired = [s for s in tail if s not in fire]
        entry = {"timer_id": t["id"], "count": skipped, "approx": approx, "first": t["next_run"],
                 "last": not_fired[-1] if not_fired else None, "policy": policy,
                 "reason": _skip_reason(policy, max_late, fire, tail)}
        store.insert_event(con, "timer.skipped", source="timers", ref=t["id"], now=now, data=entry)
        report["skipped"].append(entry)
    con.execute("UPDATE timers SET next_run=?, last_run=COALESCE(?, last_run), last_job=COALESCE(?, last_job), "
                "enabled=?, updated=? WHERE id=?",
                (next_run, fire[-1] if fire else None, job_id, 0 if t["kind"] == "once" else 1, now, t["id"]))


def _skip_reason(policy, max_late, fire, tail):
    if policy == "skip":
        return "policy skip"
    if policy == "latest":
        return "older runs replaced by the latest" if fire else f"later than max_late_s={max_late}"
    if len(tail) == len(fire):
        return f"more than {MAX_FIRE_ALL} missed runs"
    return f"later than max_late_s={max_late}"


def remove(timer_id, *, now=None):
    with store.transaction() as con:
        gone = con.execute("DELETE FROM timers WHERE id=?", (timer_id,)).rowcount == 1
        if gone:
            store.insert_event(con, "timer.removed", source="timers", ref=timer_id, now=now)
        return gone


def enable(timer_id, *, now=None):
    """Turn a timer back on, scheduled from `now` (runs missed while it was off are not caught up).
    A one-shot that already fired stays off."""
    now = store.now_or(now)
    with store.transaction() as con:
        t = con.execute("SELECT * FROM timers WHERE id=?", (timer_id,)).fetchone()
        if t is None:
            return False
        if t["kind"] == "once":
            if t["next_run"] is None:
                return False
            next_run = t["next_run"]
        else:
            next_run = _first_run(t["kind"], t["spec"], t["tz"], now)
        con.execute("UPDATE timers SET enabled=1, next_run=?, updated=? WHERE id=?", (next_run, now, timer_id))
        store.insert_event(con, "timer.enabled", source="timers", ref=timer_id, data={"next_run": next_run},
                           now=now)
        return True


def disable(timer_id, *, now=None):
    now = store.now_or(now)
    with store.transaction() as con:
        off = con.execute("UPDATE timers SET enabled=0, updated=? WHERE id=?", (now, timer_id)).rowcount == 1
        if off:
            store.insert_event(con, "timer.disabled", source="timers", ref=timer_id, now=now)
        return off


def get(timer_id):
    row = store.connect().execute("SELECT * FROM timers WHERE id=?", (timer_id,)).fetchone()
    return _row(row) if row else None


def list(*, enabled=None, origin=None):  # noqa: A001 - the public API name
    where, args = [], []
    if enabled is not None:
        where.append("enabled=?")
        args.append(int(bool(enabled)))
    if origin is not None:
        where.append("origin=?")
        args.append(origin)
    sql = "SELECT * FROM timers" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY next_run IS NULL, next_run, id"
    return [_row(r) for r in store.connect().execute(sql, args).fetchall()]


def _row(r):
    d = dict(r)
    d["payload"] = store.loads(d["payload"]) or {}
    d["enabled"] = bool(d["enabled"])
    d["tz_effective"] = d["tz"] if d["tz"] and _resolve_tz(d["tz"]) is not None else "local"
    return d
