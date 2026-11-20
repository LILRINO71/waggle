"""Outreach on the core: register_core() is the one entry point the core process calls at start.

It registers
  transports    push -> notify.push_sender, call -> call.call_sender (with waggle.core.sender, imported lazily)
  job handlers  (waggle.core.jobs) for the kinds below
  timers        (waggle.core.timers; re-adding at every start keeps their schedule)

  timer id           schedule                       catch-up                         job kind
  outreach.look      every LOOK_EVERY (5 min)       skip                             outreach.look
  outreach.brief     brief_time (cron, 07:30)       latest, max_late brief_catchup   outreach.brief
  outreach.evening   evening_time (cron, 21:00)     latest, max_late evening_catchup outreach.evening
  outreach.scout     17:00 daily (cron)             latest, max_late 7 h             outreach.scout
  reminder:<id>      once (followups.add)           all                              outreach.reminder
  checkin:<id>       once (followups.capture)       all                              outreach.checkin
  call:<id>          once (call.schedule_at)        all                              outreach.call

outreach.look, each step guarded so one failing step never stops the others:
  calls       how placed calls ended (the "call" ledger evidence)
  followups   new commitments and "remind me" from the user's own turns -> check-in and reminder timers
  watchers    GitHub (github_every_min), Waggle's work, cron failures, unpushed commits (5 min), Upcoming (15 min)
  responses   which sent messages the user answered
  decide      the reasoning step over pending signals: every initiative_every_min while awake, at once if urgent
Cadences and cursors live in runtime.db meta ("outreach.last.<step>", "outreach.last_decision"), never in files.

Handlers get the timer payload with scheduled_ts and late_s; a reminder that fires late says so. Nothing here
sends: everything goes into the core outbox and the core's sender loop delivers it.
main() runs one pass without the core (fire due timers, run this module's due jobs) for the old cron entries.
"""
import datetime as dt
import time

from . import events
from .config import Settings

LOOK_EVERY = "5m"
SCOUT_CRON = "0 17 * * *"
SCOUT_MAX_LATE_S = 7 * 3600
JOB_KINDS = ("outreach.look", "outreach.brief", "outreach.evening", "outreach.scout", "outreach.reminder",
             "outreach.checkin", "outreach.call")
LOOK_STEPS = ("calls", "followups", "watchers", "responses", "decide")


def _cron(spec, default):
    """'21:00' or a 5-field cron -> a 5-field cron."""
    spec = (spec or default).strip()
    if ":" in spec and len(spec.split()) == 1:
        try:
            h, m = (int(x) for x in spec.split(":")[:2])
            return f"{m} {h} * * *"
        except ValueError:
            return _cron(default, default) if spec != default else "0 21 * * *"
    return spec if len(spec.split()) == 5 else _cron(default, default)


def _every(key, minutes, now):
    """True (and remembered) when `minutes` passed since this step last ran."""
    from waggle.core import store
    k = f"outreach.last.{key}"
    if now - float(store.get_meta(k, 0) or 0) >= minutes * 60:
        store.set_meta(k, now)
        return True
    return False


def _late(payload):
    from waggle.core import timers
    return float(payload.get("late_s") or 0) > timers.LATE_GRACE_S


# ---- job handlers --------------------------------------------------------------------------------------------

def look(payload=None, *, settings=None, now=None, asker=None, gh=None, activity=None, steps=None):
    """Job outreach.look: one look around. Returns {step: result or error}. The keyword arguments are for tests."""
    from waggle.core import store

    from . import call, followups, initiative, notify, watchers
    s = settings or Settings.load()
    now = now or time.time()
    steps = steps or LOOK_STEPS
    report = {}

    def step(name, fn):
        if name not in steps:
            return None
        try:
            report[name] = fn()
        except Exception as e:  # noqa: BLE001
            report[name] = f"error: {type(e).__name__}: {e}"[:300]
            events.log("tick_error", step=name, error=report[name])
        return report[name]

    acts = {}

    def get_activity():
        if "v" not in acts:
            acts["v"] = activity if activity is not None else watchers.repo_activity(s, now=now)
        return acts["v"]

    step("calls", lambda: call.check_pending(now=now))
    step("followups", lambda: len(followups.capture(s, now)))

    def do_watchers():
        sigs = []
        if s.github_watch and _every("github", s.github_every_min, now):
            sigs += watchers.github_watch(s, gh=gh, now=now)
        if _every("work", 5, now):
            sigs += watchers.work_watch(s, now) + watchers.cron_watch(s, now) + watchers.unpushed_watch(s, now)
        if _every("upcoming", 15, now):
            sigs += watchers.upcoming_watch(s, now)
        return initiative.record(sigs, now)
    step("watchers", do_watchers)
    step("responses", lambda: initiative.track_responses(s, now))

    def do_decide():
        if not initiative.due(s, now):
            return None
        quiet = notify.in_quiet_hours(s, now)
        out = initiative.decide(s, now, asker=asker, activity=None if quiet else get_activity(), only_urgent=quiet)
        if not quiet:
            store.set_meta(initiative.LAST_DECISION_META, now)
        return [(d["signal"], d["action"]) for d in out]
    step("decide", do_decide)
    return report


def brief_due(payload, *, settings=None, compose=None):
    """Job outreach.brief: the morning brief, once per day (the timer drops it when later than brief_catchup)."""
    from . import brief, notify
    s = settings or Settings.load()
    sched = float(payload.get("scheduled_ts") or time.time())
    day = dt.date.fromtimestamp(sched).isoformat()
    text = (compose or brief.compose)(s)
    row = notify.deliver("text", text, asked=True, origin="brief", ref=day, topic=f"brief:{day}", settings=s,
                         expires=sched + (s.brief_catchup_hours + 1) * 3600)
    return {"outbox": row["id"], "late_s": payload.get("late_s")}


def evening_due(payload, *, settings=None, now=None, activity=None):
    """Job outreach.evening: tonight's note, always sent (the timer drops it when later than evening_catchup)."""
    from . import brief, notify
    s = settings or Settings.load()
    now = now or time.time()
    sched = float(payload.get("scheduled_ts") or now)
    day = dt.date.fromtimestamp(sched).isoformat()
    text = brief.evening_note(s, now, activity=activity)
    row = notify.deliver("text", text, asked=True, origin="evening", ref=day, topic=f"evening:{day}", settings=s,
                         now=now, expires=now + 12 * 3600)
    return {"outbox": row["id"], "late_s": payload.get("late_s")}


def scout_due(payload, *, settings=None, now=None):
    """Job outreach.scout: at most one automation candidate a day, as a signal for the decision step."""
    from . import initiative, scout
    s = settings or Settings.load()
    now = now or time.time()
    return initiative.record(scout.daily(s, now), now)


def reminder_due(payload, *, settings=None, now=None):
    """Job outreach.reminder: the reminder text as is; when the PC was off at its time, it says so."""
    from . import followups, notify
    s = settings or Settings.load()
    item = followups.get(payload.get("id"))
    if not item or item["status"] != "open":
        return {"skipped": "no open reminder"}
    text = f"⏰ {item['what']}"
    if _late(payload):
        at = dt.datetime.fromtimestamp(payload.get("scheduled_ts") or item["due"])
        text += f"\n(late: the PC was off at {at:%H:%M}, when this was due)"
    cause = item.get("origin") or "user"
    row = notify.deliver("text", text, asked=True, origin="reminder", ref=item["id"], topic=f"reminder:{item['id']}",
                         chat=item.get("chat") or "", source=None if cause == "user" else cause, settings=s, now=now)
    followups.mark(item["id"], "sent" if row["id"] else "suppressed", now, row["id"],
                   f"late {int(float(payload.get('late_s') or 0))} s" if _late(payload) else "")
    return {"outbox": row["id"], "late": _late(payload)}


def checkin_due(payload, *, now=None):
    """Job outreach.checkin: a check-in's time has come; the decision step words it (or says nothing)."""
    from . import followups, initiative
    now = now or time.time()
    item = followups.get(payload.get("id"))
    if not item or item["status"] != "open":
        return {"skipped": "no open check-in"}
    if (item.get("origin") or "user") != "user":
        followups.mark(item["id"], "dropped", now, reason="not the user's own words")
        return {"skipped": "not from the user"}
    n = initiative.record([followups.signal_for(item, now)], now)
    followups.mark(item["id"], "due", now)
    return {"signals": n}


def call_due(payload):
    from . import call
    return call.call_due(payload)


HANDLERS = {"outreach.look": look, "outreach.brief": brief_due, "outreach.evening": evening_due,
            "outreach.scout": scout_due, "outreach.reminder": reminder_due, "outreach.checkin": checkin_due,
            "outreach.call": call_due}


# ---- registration --------------------------------------------------------------------------------------------

def timer_specs(settings=None):
    """(timer id, kind, spec, job kind, catch_up, max_late_s) for the recurring timers."""
    s = settings or Settings.load()
    return [
        ("outreach.look", "interval", LOOK_EVERY, "outreach.look", "skip", None),
        ("outreach.brief", "cron", _cron(s.brief_time, "30 7 * * *"), "outreach.brief", "latest",
         s.brief_catchup_hours * 3600),
        ("outreach.evening", "cron", _cron(s.evening_time, "21:00"), "outreach.evening", "latest",
         s.evening_catchup_hours * 3600),
        ("outreach.scout", "cron", SCOUT_CRON, "outreach.scout", "latest", SCOUT_MAX_LATE_S),
    ]


def register_jobs():
    from waggle.core import jobs
    for kind, fn in HANDLERS.items():
        # a look that fails is simply the next look; a reminder or note gets the default retries
        jobs.register(kind, fn, max_attempts=1 if kind == "outreach.look" else 3, lease_s=600)
    return list(HANDLERS)


def register_core(now=None, settings=None):
    """Called by the core process at every start: registers this module's transports with the core sender, its
    job handlers, and its recurring timers (idempotent). Returns what was registered."""
    import importlib

    from waggle.core import timers

    from . import call, notify
    report = {"senders": [], "jobs": register_jobs(), "timers": []}
    try:
        sender = importlib.import_module("waggle.core.sender")
    except ImportError as e:
        report["sender_error"] = f"waggle.core.sender not available: {e}"
        events.log("outreach_register", error=report["sender_error"])
    else:
        sender.register("push", notify.push_sender)
        sender.register("call", call.call_sender)
        report["senders"] = ["push", "call"]
    for tid, kind, spec, job_kind, policy, max_late in timer_specs(settings):
        timers.add(tid, kind, spec, job_kind, catch_up=policy, max_late_s=max_late, origin="outreach", now=now)
        report["timers"].append(tid)
    return report


def main(argv=None):
    """One pass without the core process (old cron entries): fire due timers, run this module's due jobs.
    Prints nothing; everything goes into the outbox, which the core's sender loop delivers."""
    from waggle.core import jobs, timers

    from . import budget
    budget.set_background("tick")
    register_core()
    timers.fire_due()
    owner = jobs.new_owner("outreach")
    for _ in range(50):
        if jobs.run_one(owner, kinds=list(JOB_KINDS)) is None:
            break
    return 0


if __name__ == "__main__":
    main()
