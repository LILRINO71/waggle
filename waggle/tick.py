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
  outreach.think.<n> think_times (cron, 3 a day)    latest, max_late 3 h             outreach.think (waggle/think.py)
  reminder:<id>      once (followups.add)           all                              outreach.reminder
  checkin:<id>       once (followups.capture)       all                              outreach.checkin
  call:<id>          once (call.schedule_at)        all                              outreach.call
  recheck:<key>:<n>  once (initiative.defer)        all                              outreach.recheck

outreach.look, each step guarded so one failing step never stops the others:
  calls       how placed calls ended (the "call" ledger evidence)
  followups   new commitments and "remind me" from the user's own turns -> check-in and reminder timers
  watchers    GitHub (every 30 min at most, one repo-list call; a repo's runs, PRs and issues only when it moved), Waggle's work, cron failures, unpushed commits (5 min), Upcoming (15 min)
  responses   which sent messages the user answered
  decide      the reasoning step over pending signals: every initiative_every_min while awake, at once if urgent,
              a check-in or a look-again; a model that was unavailable is retried after 30 min, not 2 h. The result
              names the gate ("no pending signals", "quiet hours ...", "cadence ...", "model unavailable ...")
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
             "outreach.checkin", "outreach.call", "outreach.recheck", "outreach.think")
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
        """Each watcher is guarded on its own: one that fails must not take the others' signals with it (their
        cursors have already moved, so those findings would be gone for good). The result says what each did."""
        sigs, ran, not_due = [], {}, []

        def watch(name, fn):
            try:
                got = fn()
                sigs.extend(got)
                ran[name] = len(got)
            except Exception as e:  # noqa: BLE001
                ran[name] = f"error: {type(e).__name__}: {e}"[:200]
                events.log("tick_error", step=f"watchers.{name}", error=ran[name])

        if s.github_watch and _every("github", max(s.github_every_min, watchers.GITHUB_MIN_EVERY_MIN), now):
            watch("github", lambda: watchers.github_watch(s, gh=gh, now=now))
        else:
            not_due.append("github")
        if _every("work", 5, now):
            watch("work", lambda: watchers.work_watch(s, now))
            watch("cron", lambda: watchers.cron_watch(s, now))
            watch("unpushed", lambda: watchers.unpushed_watch(s, now))
        else:
            not_due += ["work", "cron", "unpushed"]
        if _every("upcoming", 15, now):
            watch("upcoming", lambda: watchers.upcoming_watch(s, now))
        else:
            not_due.append("upcoming")
        new = initiative.record(sigs, now)
        return {"found": len(sigs), "new": new, "already_known": len(sigs) - new, "ran": ran, "not_due": not_due}
    step("watchers", do_watchers)
    step("responses", lambda: initiative.track_responses(s, now))

    def do_decide():
        """{"gate": why it ran or did not, "pending", "urgent", and when it ran: "model", "decisions", "held",
        "notes"}: a pass that decided nothing says which gate stopped it."""
        g = initiative.gate(s, now)
        res = {"gate": g["gate"], "pending": g["pending"], "urgent": g["urgent"], "ran": g["due"]}
        if not g["due"]:
            return res
        quiet = notify.in_quiet_hours(s, now)
        info = {}
        out = initiative.decide(s, now, asker=asker, activity=None if quiet else get_activity(), only_urgent=quiet,
                                info=info)
        if not quiet and info.get("model") != "not asked" and not str(info.get("model")).startswith("unavailable"):
            store.set_meta(initiative.LAST_DECISION_META, now)  # a failed try does not use up the cadence slot
        res.update({"model": info.get("model"), "held": info.get("held"), "notes": info.get("notes"),
                    "decisions": [(d["signal"], d["action"]) for d in out]})
        return res
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


def _spoken_reminder(what, late):
    """The one spoken sentence for a reminder, in the manner of the spoken replies: composed, "sir" once."""
    what = " ".join(str(what or "").split()).rstrip(" .!?")
    return f"Sir, your reminder, which is late: {what}." if late else f"Sir, your reminder: {what}."


def _voice_first(item, late, settings, now, synth=None):
    """Queue the voice note of a reminder asked for by voice. (outbox id or None, why not): on any failure of the
    speech nothing is queued and the reason comes back, so the text line goes out alone."""
    import uuid
    from . import notify
    from .config import data_path
    cause = item.get("origin") or "user"
    if notify.is_dev_source(None if cause == "user" else cause, settings):
        return None, "not the user's own reminder"
    folder = data_path("voice_out")
    out = folder / f"{uuid.uuid4().hex[:10]}.ogg"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        ok = (synth or notify._synth)(_spoken_reminder(item["what"], late), out, settings)
    except Exception as e:  # noqa: BLE001
        return None, f"speech failed: {str(e)[:80]}"
    if not ok:
        return None, "speech failed"
    row = notify.deliver("voice", "", path=str(out), asked=True, origin="reminder", ref=f"{item['id']}:voice",
                         topic=f"reminder:{item['id']}", chat=item.get("chat") or "", settings=settings, now=now)
    return row["id"], ""


def reminder_due(payload, *, settings=None, now=None, synth=None):
    """Job outreach.reminder: the reminder text as is; when the PC was off at its time, it says so. A reminder
    asked for by voice (payload voice) first goes out as a one-sentence voice note; if the speech fails the text
    goes out alone and the reason is recorded."""
    from . import events, followups, notify
    s = settings or Settings.load()
    item = followups.get(payload.get("id"))
    if not item or item["status"] != "open":
        return {"skipped": "no open reminder"}
    from . import deadman
    if deadman.worker_sent(item["id"], s):  # the backup Worker sent it while the PC was off: never twice
        followups.mark(item["id"], "sent", now, None, "already sent by the backup service")
        return {"skipped": "sent by the backup service"}
    text = f"⏰ {item['what']}"
    if _late(payload):
        at = dt.datetime.fromtimestamp(payload.get("scheduled_ts") or item["due"])
        text += f"\n(late: the PC was off at {at:%#I:%M %p}, when this was due)"
    cause = item.get("origin") or "user"
    voice_id, why = None, ""
    if payload.get("voice"):
        voice_id, why = _voice_first(item, _late(payload), s, now, synth)
        if why:
            events.log("reminder_voice", id=item["id"], ok=False, why=why)
    row = notify.deliver("text", text, asked=True, origin="reminder", ref=item["id"], topic=f"reminder:{item['id']}",
                         chat=item.get("chat") or "", source=None if cause == "user" else cause, settings=s, now=now,
                         meta={"spoken": _spoken_reminder(item["what"], _late(payload))})  # what the PC says aloud
    notes = [f"late {int(float(payload.get('late_s') or 0))} s"] if _late(payload) else []
    if why:
        notes.append(why)
    followups.mark(item["id"], "sent" if row["id"] else "suppressed", now, row["id"], "; ".join(notes))
    deadman.ack(item["id"], s)  # tell the backup Worker at once (no-op when it is not set up)
    return {"outbox": row["id"], "late": _late(payload), "voice_outbox": voice_id, **({"voice_error": why} if why else {})}


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


def recheck_due(payload, *, now=None):
    """Job outreach.recheck: a finding the decision step put off ("look again at ...") comes back as pending."""
    from . import initiative
    key = payload.get("key") or ""
    back = initiative.recheck(key, now or time.time())
    return {"key": key, "back": back, "deferred": initiative.deferral(key)["count"], "late": _late(payload)}


def call_due(payload):
    from . import call
    return call.call_due(payload)


def think_due(payload=None):
    """Job outreach.think: the open thinking pass over the whole picture (waggle/think.py)."""
    from . import think
    return think.due(payload)


HANDLERS = {"outreach.look": look, "outreach.brief": brief_due, "outreach.evening": evening_due,
            "outreach.scout": scout_due, "outreach.reminder": reminder_due, "outreach.checkin": checkin_due,
            "outreach.call": call_due, "outreach.recheck": recheck_due, "outreach.think": think_due}


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
        # a look or a thinking pass that fails is simply the next one; a reminder or note gets the default retries
        jobs.register(kind, fn, max_attempts=1 if kind in ("outreach.look", "outreach.think") else 3, lease_s=600)
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
        if hasattr(sender, "before_send"):   # held messages are let go at the owner's next break (waggle/holdq.py)
            from . import holdq
            sender.before_send(holdq.release_hook)
    for tid, kind, spec, job_kind, policy, max_late in timer_specs(settings):
        timers.add(tid, kind, spec, job_kind, catch_up=policy, max_late_s=max_late, origin="outreach", now=now)
        report["timers"].append(tid)
    from . import think
    wanted = think.timer_specs(settings)
    for tid, kind, spec, job_kind, policy, max_late, payload in wanted:
        timers.add(tid, kind, spec, job_kind, payload, catch_up=policy, max_late_s=max_late, origin="outreach", now=now)
        report["timers"].append(tid)
    keep = {w[0] for w in wanted}
    for i in range(4):   # a pass time that was removed from think_times (or the switch turned off) stops firing
        tid = f"outreach.think.{i}"
        if tid not in keep:
            try:
                timers.remove(tid)
            except Exception:  # noqa: BLE001 - it never existed
                pass
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
