"""Background Claude budget: work the user isn't watching (objectives, routines, proactive checks, the morning brief,
weekly learning) gets its own daily allowance, so it can never quietly use up the Claude usage needed for chatting.

Every Claude call is written to ~/.waggle/budget.db with:
- kind        interactive | background (set by the background entry points through `background(...)`)
- objective / step / purpose / model
- tokens      as the Claude CLI reported them. measured=1 means the CLI returned a usage block; measured=0 means it
              returned none (timeout, crash) and the row carries the reservation's ESTIMATE instead.
- units       weighted tokens (quota.weighted). Always an estimate of how much of the plan limit a call used:
              Anthropic does not publish what a call costs against the limit, only the token counts.

Before a background call, reserve() runs one exclusive SQLite transaction that checks
    used today + other workers' open reservations + this call's expected cost <= background_daily_units,
    the shared 5-hour window is under background_stop_at (interactive chat may go to stop_at, which is higher),
    fewer than background_max_workers background calls are running,
    and the user has not chatted in the last interactive_yield_s seconds,
then writes a reservation. settle() swaps it for the real usage row. A crashed worker's reservation expires after
RESERVATION_TTL. The day resets at background_reset_hour (local time). Interactive calls are never blocked here.

OWN_ALLOWANCE: a few named purposes have their own narrow allowance instead of the shared daily units and the
background_stop_at line (mail watch: telling the user about important mail must not wait for a quiet window). They
still pass every other check (switched off, running workers, the user chatting, a real limit hit), stop at their
own window line, and are recorded in `usage` like any other call.
"""
import contextlib
import contextvars
import datetime as dt
import json
import os
import sqlite3
import time
import uuid

from .config import Settings, data_path

RESERVATION_TTL = 30 * 60
# Fallback expected cost per call (weighted units) before there is history for that purpose + model.
DEFAULT_COST = {"haiku": 0.005, "sonnet": 0.03, "opus": 0.6, "fable": 0.6}
AGENT_COST = {"haiku": 0.06, "sonnet": 0.2, "opus": 0.8, "fable": 0.8}
# purpose -> its own allowance: allowed while the 5-hour window stays at or below stop_at, at most daily_calls calls
# per budget day. Not counted against background_daily_units, not stopped at background_stop_at.
OWN_ALLOWANCE = {"mailwatch": {"stop_at": 0.92, "daily_calls": 40},
                 # 3 thinking passes a day, each at most 2 small calls: they keep running until the week is nearly
                 # used up (the general background stop, seven_day_stop_at, is for long work)
                 "think": {"stop_at": 0.92, "daily_calls": 8, "week_stop_at": 0.97}}

_ctx = contextvars.ContextVar("waggle_budget_ctx", default=None)


class Deferred(Exception):
    """Background work must wait. .until is when it is worth trying again; .reason says why (for the log)."""

    def __init__(self, reason, until):
        super().__init__(reason)
        self.reason, self.until = reason, until


# ---- context: who is spending ---------------------------------------------------------------------

@contextlib.contextmanager
def background(purpose="background", objective=None, step=None, user_started=None):
    """Mark everything inside as background work (cron jobs, objective workers). user_started: the user just asked
    for this work (started or approved it), so it doesn't wait for the user to stop chatting."""
    if user_started is None:
        user_started = bool((_ctx.get() or {}).get("user_started"))
    tok = _ctx.set({"kind": "background", "purpose": purpose, "objective": objective, "step": step, "ticket": None,
                    "user_started": user_started})
    try:
        yield _ctx.get()
    finally:
        _ctx.reset(tok)


def set_background(purpose):
    """For cron entry points that run as their own process: the whole process is background."""
    _ctx.set({"kind": "background", "purpose": purpose, "objective": None, "step": None, "ticket": None})


def current():
    c = _ctx.get()
    if c:
        return c
    if os.environ.get("WAGGLE_KIND") == "background":
        return {"kind": "background", "purpose": os.environ.get("WAGGLE_PURPOSE", "background"),
                "objective": os.environ.get("WAGGLE_OBJECTIVE"), "step": None, "ticket": None}
    return {"kind": "interactive", "purpose": "", "objective": None, "step": None, "ticket": None}


def is_background():
    return current()["kind"] == "background"


# ---- storage ---------------------------------------------------------------------------------------

def _db():
    con = sqlite3.connect(data_path("budget.db"), timeout=20, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, time REAL, day TEXT, kind TEXT, objective TEXT,
        step INTEGER, purpose TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER,
        cache_write INTEGER, units REAL, measured INTEGER, ok INTEGER, local INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS usage_day ON usage(day, kind);
    CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, time REAL, day TEXT, objective TEXT, step INTEGER,
        purpose TEXT, model TEXT, units REAL, pid INTEGER);
    CREATE TABLE IF NOT EXISTS decisions (time REAL, objective TEXT, step INTEGER, action TEXT, model TEXT,
        reason TEXT);
    CREATE TABLE IF NOT EXISTS learned (key TEXT PRIMARY KEY, value REAL, time REAL);
    """)
    return con


def day_key(now=None, settings=None):
    """Budget day: starts at background_reset_hour local time (default 04:00), not midnight."""
    s = settings or Settings.load()
    t = dt.datetime.fromtimestamp(now or time.time()) - dt.timedelta(hours=s.background_reset_hour)
    return t.strftime("%Y-%m-%d")


def next_reset(now=None, settings=None):
    s = settings or Settings.load()
    now = now or time.time()
    t = dt.datetime.fromtimestamp(now)
    reset = t.replace(hour=s.background_reset_hour, minute=0, second=0, microsecond=0)
    if reset <= t:
        reset += dt.timedelta(days=1)
    return reset.timestamp()


def _family(model):
    m = (model or "").lower()
    for k in ("opus", "sonnet", "haiku", "fable"):
        if k in m:
            return k
    return "sonnet"


def expected_cost(model, purpose="", con=None):
    """What a call will probably cost (weighted units): the mean of the last 10 measured calls with the same purpose
    and model family, else a conservative default."""
    fam = _family(model)
    base = (AGENT_COST if purpose.startswith(("objective", "agent", "routine")) else DEFAULT_COST).get(fam, 0.05)
    own = con is None
    con = con or _db()
    try:
        rows = con.execute("SELECT units FROM usage WHERE purpose=? AND model LIKE ? AND measured=1 AND units>0 "
                           "ORDER BY time DESC LIMIT 10", (purpose, f"%{fam}%")).fetchall()
    finally:
        if own:
            con.close()
    if len(rows) >= 3:
        return max(base * 0.25, sum(r["units"] for r in rows) / len(rows))
    return base


def log_decision(action, reason, objective=None, step=None, model=""):
    con = _db()
    try:
        con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)", (time.time(), objective, step, action, model,
                                                                 str(reason)[:300]))
    finally:
        con.close()


# ---- the gate --------------------------------------------------------------------------------------

def note_interactive(now=None):
    """The user just sent a message (called from the chat hook): background work gives way for a moment."""
    try:
        data_path("last_interactive").write_text(str(now or time.time()), encoding="utf-8")
    except OSError:
        pass


def _last_interactive():
    try:
        return float(data_path("last_interactive").read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _alive(pid):
    from .jobs import _alive as alive
    return alive(pid)


def _spent(con, day, kind="background"):
    return con.execute("SELECT COALESCE(SUM(units),0) FROM usage WHERE day=? AND kind=?", (day, kind)).fetchone()[0]


def _open_reservations(con, now):
    """Drop reservations of dead or long-gone workers; return the live ones."""
    rows = con.execute("SELECT * FROM reservations").fetchall()
    live = []
    for r in rows:
        if now - r["time"] > RESERVATION_TTL or (r["pid"] and r["pid"] != os.getpid() and not _alive(r["pid"])):
            con.execute("DELETE FROM reservations WHERE id=?", (r["id"],))
        else:
            live.append(r)
    return live


def _calls_today(con, day, purpose, live):
    """Calls with this purpose in the budget day: recorded usage rows (failed calls too) plus open reservations."""
    used = con.execute("SELECT COUNT(*) FROM usage WHERE day=? AND purpose=? AND local=0", (day, purpose)).fetchone()[0]
    return used + len([r for r in live if r["day"] == day and r["purpose"] == purpose])


def allowance(purpose, settings=None, now=None):
    """{calls, daily_calls, stop_at} today for a purpose with its own allowance (None for every other purpose)."""
    rule = OWN_ALLOWANCE.get(purpose)
    if not rule:
        return None
    s = settings or Settings.load()
    now = now or time.time()
    con = _db()
    try:
        calls = _calls_today(con, day_key(now, s), purpose, _open_reservations(con, now))
    finally:
        con.close()
    return {"calls": calls, **rule}


def check(model, purpose="", settings=None, now=None, window_used=None, con=None):
    """(ok, reason, retry_at) for a background call, without reserving anything."""
    s = settings or Settings.load()
    now = now or time.time()
    own = con is None
    con = con or _db()
    try:
        day = day_key(now, s)
        spent = _spent(con, day)
        live = _open_reservations(con, now)
        reserved = sum(r["units"] for r in live if r["day"] == day)
        cost = expected_cost(model, purpose, con)
        limit = s.background_daily_units
        rule = OWN_ALLOWANCE.get(purpose)
        if limit <= 0:
            return False, "background Claude use is switched off (background_daily_units = 0)", next_reset(now, s)
        if rule:
            calls = _calls_today(con, day, purpose, live)
            if calls >= rule["daily_calls"]:
                return (False, f"{purpose} allowance used: {calls} of {rule['daily_calls']} calls today",
                        next_reset(now, s))
        elif spent + reserved + cost > limit:
            return (False, f"background budget used: {spent:.2f} of {limit:.2f} units today"
                    + (f" (+{reserved:.2f} reserved by running work)" if reserved else ""), next_reset(now, s))
        running = len([r for r in live if r["pid"] != os.getpid()])
        if running >= s.background_max_workers:
            return False, f"{running} background Claude calls already running (max {s.background_max_workers})", now + 120
        if (s.interactive_yield_s and not current().get("user_started")
                and now - _last_interactive() < s.interactive_yield_s):
            return False, "the user is chatting right now; background work waits", now + s.interactive_yield_s
        from . import quota
        st = quota._state()
        if st.get("blocked_until", 0) > now:
            return False, "Claude limit reached", st["blocked_until"]
        blocked, why7, retry7 = quota.seven_day_block(s, now)  # the owner's week comes first
        if blocked and rule and rule.get("week_stop_at"):   # a few small calls a day run nearer to the week's end
            seven = quota.seven_day_reading(now)
            blocked = not (seven and seven["utilization"] < rule["week_stop_at"])
        if blocked:
            return False, why7, retry7
        if window_used is None:
            window_used = quota.window_used_units(s, now, con)
        cap = quota.window_estimate(s, con)
        if rule:
            if window_used + cost > rule["stop_at"] * cap:
                return (False, f"5-hour window at {window_used / cap:.0%}; {purpose} stops at "
                               f"{rule['stop_at']:.0%}", now + 1800)
            return True, f"ok ({purpose}: {calls} of {rule['daily_calls']} calls today)", now
        if window_used + cost > s.background_stop_at * cap:
            return (False, f"5-hour window at {window_used / cap:.0%}; background stops at "
                           f"{s.background_stop_at:.0%} so chat keeps the rest", now + 1800)
        return True, f"ok ({spent:.2f}/{limit:.2f} units today)", now
    finally:
        if own:
            con.close()


def reserve(model, purpose="objective", objective=None, step=None, settings=None, now=None, window_used=None):
    """Atomically claim room for one background call. Returns a ticket id; raises Deferred if it must wait."""
    s = settings or Settings.load()
    now = now or time.time()
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")  # one writer at a time: two workers can't both take the last slot
        ok, why, until = check(model, purpose, s, now, window_used, con=con)
        if not ok:
            con.execute("COMMIT")
            con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)", (now, objective, step, "defer", model, why))
            raise Deferred(why, until)
        tid = uuid.uuid4().hex[:12]
        con.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?)",
                    (tid, now, day_key(now, s), objective, step, purpose, model, expected_cost(model, purpose, con),
                     os.getpid()))
        con.execute("COMMIT")
        return tid
    except Deferred:
        raise
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()


def release(ticket):
    if not ticket:
        return
    con = _db()
    try:
        con.execute("DELETE FROM reservations WHERE id=?", (ticket,))
    finally:
        con.close()


COSTLY_ERRORS = (None, "timeout", "error", "model_mismatch")  # a failed call of these kinds may still have used tokens


def record(model, usage, purpose="", ok=True, settings=None, now=None, local=False, estimate=None, error_kind=None):
    """One usage row (called from quota.record for every Claude call). Returns the row's units."""
    from . import quota
    s = settings or Settings.load()
    now = now or time.time()
    c = current()
    usage = usage or {}
    measured = bool(usage.get("input_tokens") or usage.get("output_tokens"))
    if local:
        units = 0.0
    elif measured:
        units = quota.weighted(usage, model)
    else:  # nothing reported: count what we expected, flagged as an estimate
        units = estimate if estimate is not None else (
            expected_cost(model, purpose) if not ok and error_kind in COSTLY_ERRORS else 0.0)
    con = _db()
    try:
        con.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], now, day_key(now, s), c["kind"], c.get("objective"), c.get("step"),
                     purpose or c.get("purpose") or "", model, int(usage.get("input_tokens") or 0),
                     int(usage.get("output_tokens") or 0), int(usage.get("cache_read_input_tokens") or 0),
                     int(usage.get("cache_creation_input_tokens") or 0), round(units, 6), int(measured), int(ok),
                     int(local)))
    finally:
        con.close()
    return units


@contextlib.contextmanager
def spend(model, purpose="objective", objective=None, step=None, settings=None):
    """Reserve room for one background call, mark the context, release the reservation afterwards (the usage row
    itself is written by quota.record when the call returns). Raises Deferred when it must wait."""
    ticket = reserve(model, purpose, objective, step, settings)
    tok = _ctx.set({"kind": "background", "purpose": purpose, "objective": objective, "step": step,
                    "ticket": ticket})
    try:
        yield ticket
    finally:
        _ctx.reset(tok)
        release(ticket)


@contextlib.contextmanager
def using(ticket, purpose="objective", objective=None, step=None):
    """Run inside an existing reservation (the caller releases it)."""
    tok = _ctx.set({"kind": "background", "purpose": purpose, "objective": objective, "step": step,
                    "ticket": ticket, "user_started": bool((_ctx.get() or {}).get("user_started"))})
    try:
        yield ticket
    finally:
        _ctx.reset(tok)


def guard(model, purpose=""):
    """Backstop inside every Claude call: background work that skipped reserve() still can't overspend."""
    c = current()
    if c["kind"] != "background" or c.get("ticket"):
        return
    ok, why, until = check(model, purpose or c.get("purpose", ""))
    if not ok:
        log_decision("defer", why, c.get("objective"), c.get("step"), model)
        raise Deferred(why, until)


def choose_model(model, agent_name="", settings=None, objective=None, step=None):
    """Cheaper model when the day's background budget is running low (logged). Coding and robotics work is never
    downgraded: it defers instead of being done badly."""
    s = settings or Settings.load()
    fam = _family(model)
    if fam not in ("sonnet", "opus") or agent_name in ("coder", "robotics"):
        return model
    left = remaining(s)
    if s.background_daily_units > 0 and left / s.background_daily_units < s.background_downgrade_at:
        cheaper = s.agent_model
        log_decision("downgrade", f"{left:.2f} units left today (< {s.background_downgrade_at:.0%}): "
                                  f"{model} -> {cheaper}", objective, step, cheaper)
        return cheaper
    return model


# ---- reporting -------------------------------------------------------------------------------------

def remaining(settings=None, now=None):
    s = settings or Settings.load()
    con = _db()
    try:
        day = day_key(now, s)
        reserved = sum(r["units"] for r in _open_reservations(con, now or time.time()) if r["day"] == day)
        return max(0.0, s.background_daily_units - _spent(con, day) - reserved)
    finally:
        con.close()


def status(settings=None, now=None):
    """For the Command Center, the CLI and doctor."""
    s = settings or Settings.load()
    now = now or time.time()
    con = _db()
    try:
        day = day_key(now, s)
        live = _open_reservations(con, now)
        spent = _spent(con, day)
        inter = _spent(con, day, "interactive")
        by_obj = [dict(r) for r in con.execute(
            "SELECT COALESCE(objective,'') AS objective, purpose, COUNT(*) AS calls, ROUND(SUM(units),4) AS units "
            "FROM usage WHERE day=? AND kind='background' GROUP BY objective, purpose ORDER BY units DESC LIMIT 10",
            (day,))]
        by_model = [dict(r) for r in con.execute(
            "SELECT model, kind, local, COUNT(*) AS calls, ROUND(SUM(units),4) AS units, SUM(measured) AS measured "
            "FROM usage WHERE day=? GROUP BY model, kind, local ORDER BY units DESC", (day,))]
        decisions = [dict(r) for r in con.execute("SELECT * FROM decisions ORDER BY time DESC LIMIT 8")]
        est = con.execute("SELECT COUNT(*) FROM usage WHERE day=? AND measured=0 AND units>0", (day,)).fetchone()[0]
    finally:
        con.close()
    ok, why, until = check(s.agent_deep_model, "objective", s, now)
    return {"day": day, "limit_units": s.background_daily_units, "background_units": round(spent, 4),
            "reserved_units": round(sum(r["units"] for r in live), 4), "interactive_units": round(inter, 4),
            "remaining_units": round(max(0.0, s.background_daily_units - spent), 4),
            "running_calls": len(live), "max_workers": s.background_max_workers,
            "resets_at": next_reset(now, s), "can_start": ok, "why": why, "retry_at": until,
            "estimated_rows": est, "by_objective": by_obj, "by_model": by_model, "decisions": decisions,
            "note": "units = weighted tokens; token counts are what the Claude CLI reported, the limit cost is an "
                    "estimate"}


def set_limit(units, settings=None):
    s = settings or Settings.load()
    s.background_daily_units = max(0.0, float(units))
    s.save()
    return s.background_daily_units


def main():
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "set":
        print(f"background budget: {set_limit(sys.argv[2])} units/day")
    else:
        print(json.dumps(status(), indent=2, default=str))


if __name__ == "__main__":
    main()
