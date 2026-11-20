"""The tick: the one place that decides what is due. Cron entries only call tick.main(); all timing lives here and
in durable state (~/.waggle/tick_state.json, the outbox, initiative.db, followups.json, routines.json), so the
whole thing can move into an always-on process by calling tick() from a loop.

Every run (each step guarded; one failing step never stops the others):
  flush       send what is due in the outbox (retries with backoff, morning release after quiet hours)
  calls       scheduled "call me in N min" calls; how placed calls ended (call evidence)
  reminders   one-shot reminders and calls from routines ("remind me at 7")
  followups   new commitments from the user's Telegram turns; check-ins whose time has come -> signals
  watchers    GitHub (github_every_min), Waggle's work, cron failures, unpushed commits (5 min), Upcoming (15 min)
  responses   which sent messages the user answered (evaluation + channel evidence)
  decide      the reasoning step over pending signals: every initiative_every_min while awake, at once if urgent
  brief       the morning brief at brief_time, caught up for brief_catchup_hours, never later
  evening     the evening note at evening_time, always sent, caught up for evening_catchup_hours
  scout       one automation candidate a day (after 17:00)
  flush       again, so anything decided this run goes out now
A lock file keeps two ticks from overlapping (the minute job and any old cron entry may both call this).
"""
import datetime as dt
import json
import os
import time

from . import events
from .config import Settings, data_path

STEPS = ("calls", "reminders", "followups", "watchers", "responses", "decide", "brief", "evening", "scout")
LOCK_STALE_S = 15 * 60


def _state():
    try:
        return json.loads(data_path("tick_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(st):
    data_path("tick_state.json").write_text(json.dumps(st, indent=1), encoding="utf-8")


def _lock(now):
    p = data_path("tick.lock")
    for _ in range(2):
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps({"pid": os.getpid(), "time": now}).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                held = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                held = {"time": 0}
            if time.time() - held.get("time", 0) < LOCK_STALE_S:
                return False
            try:
                p.unlink()
            except OSError:
                return False
    return False


def _unlock():
    try:
        data_path("tick.lock").unlink()
    except OSError:
        pass


def _hm(spec, default):
    """'21:00' or a cron '30 7 * * *' -> (hour, minute)."""
    spec = (spec or default).strip()
    try:
        if ":" in spec:
            h, m = spec.split(":")[:2]
            return int(h), int(m)
        parts = spec.split()
        return int(parts[1]), int(parts[0])
    except (ValueError, IndexError):
        return _hm(default, default) if spec != default else (7, 30)


def note_due(name, hm, catchup_h, st, now):
    """'send' when today's note is due (at or after its time, within the catch-up window, not sent yet),
    'missed' once when the window passed without a send, else None."""
    nowd = dt.datetime.fromtimestamp(now)
    sched = nowd.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    today = nowd.date().isoformat()
    if st.get(f"{name}_day") == today or now < sched.timestamp():
        return None
    if now <= sched.timestamp() + catchup_h * 3600:
        return "send"
    if st.get(f"{name}_missed") != today:
        st[f"{name}_missed"] = today
        return "missed"
    return None


def tick(now=None, settings=None, *, asker=None, gh=None, steps=None, compose_brief=None, activity=None):
    """One pass. Returns {step: result or error}. asker/gh/compose_brief/activity are injectable for tests."""
    s = settings or Settings.load()
    now = now or time.time()
    steps = steps or STEPS
    if not _lock(now):
        return {"skipped": "another tick is running"}
    report = {}
    st = _state()

    def step(name, fn):
        if name not in steps:
            return None
        try:
            report[name] = fn()
        except Exception as e:  # noqa: BLE001
            report[name] = f"error: {type(e).__name__}: {e}"[:300]
            events.log("tick_error", step=name, error=report[name])
        return report[name]

    def every(key, minutes):
        if now - st.get("last", {}).get(key, 0) >= minutes * 60:
            st.setdefault("last", {})[key] = now
            return True
        return False

    acts = {}

    def get_activity():
        if "v" not in acts:
            if activity is not None:
                acts["v"] = activity
            else:
                from . import watchers
                acts["v"] = watchers.repo_activity(s, now=now)
        return acts["v"]

    try:
        from . import call, followups, initiative, notify, routines, watchers
        step("calls", lambda: {"placed": len(call.run_due(s, now)), "ended": call.check_pending(now=now)})
        step("reminders", lambda: routines.run_due_reminders(s, now))

        def do_followups():
            added = followups.capture(s, now)
            return {"captured": len(added), "due": initiative.record(followups.due(now), now)}
        step("followups", do_followups)

        def do_watchers():
            sigs = []
            if s.github_watch and every("github", s.github_every_min):
                sigs += watchers.github_watch(s, gh=gh, now=now)
            if every("work", 5):
                sigs += watchers.work_watch(s, now) + watchers.cron_watch(s, now) + watchers.unpushed_watch(s, now)
            if every("upcoming", 15):
                sigs += watchers.upcoming_watch(s, now)
            return initiative.record(sigs, now)
        step("watchers", do_watchers)
        step("responses", lambda: initiative.track_responses(s, now))

        def do_decide():
            if not initiative.due(s, now, st):
                return None
            quiet = notify.in_quiet_hours(s, now)
            out = initiative.decide(s, now, asker=asker, activity=None if quiet else get_activity(), only_urgent=quiet)
            if not quiet:
                st["last_decision"] = now
            return [(d["signal"], d["action"]) for d in out]
        step("decide", do_decide)

        def do_brief():
            due = note_due("brief", _hm(s.brief_time, "30 7 * * *"), s.brief_catchup_hours, st, now)
            if due == "missed":
                events.log("note_missed", note="brief", reason="the PC or gateway was off past the catch-up window")
                return "missed"
            if due != "send":
                return None
            from . import brief
            text = (compose_brief or brief.compose)(s)
            h, m = _hm(s.brief_time, "30 7 * * *")
            sched = dt.datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0)
            row = notify.deliver("text", text, asked=True, origin="brief", topic=f"brief:{dt.date.fromtimestamp(now)}",
                                 expires=sched.timestamp() + (s.brief_catchup_hours + 1) * 3600, settings=s, now=now)
            st["brief_day"] = dt.datetime.fromtimestamp(now).date().isoformat()
            return row["status"]
        step("brief", do_brief)

        def do_evening():
            hm = _hm(s.evening_time, "21:00")
            due = note_due("evening", hm, s.evening_catchup_hours, st, now)
            if due == "missed":
                events.log("note_missed", note="evening", reason="the PC or gateway was off past the catch-up window")
                return "missed"
            if due != "send":
                return None
            from . import brief
            text = brief.evening_note(s, now, activity=get_activity())
            row = notify.deliver("text", text, asked=True, origin="evening",
                                 topic=f"evening:{dt.date.fromtimestamp(now)}", settings=s, now=now,
                                 expires=now + 12 * 3600)
            st["evening_day"] = dt.datetime.fromtimestamp(now).date().isoformat()
            return row["status"]
        step("evening", do_evening)

        def do_scout():
            if dt.datetime.fromtimestamp(now).hour < 17:
                return None
            from . import scout
            return initiative.record(scout.daily(s, now), now)
        step("scout", do_scout)
    finally:
        _save({**_state(), **st})
        _unlock()
    return report


def main(argv=None):
    """Cron entry point (thin): runs one tick, prints nothing (everything goes out through the outbox)."""
    from . import budget
    budget.set_background("tick")
    tick()
    return 0


if __name__ == "__main__":
    main()
