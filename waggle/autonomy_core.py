"""Autonomous work on the core: the activity model, the work loop, the miner and the evening report as jobs and
timers. The core calls register_core() at every start once this module is listed in waggle.core.plugins.MODULES;
registering again changes nothing. It also seeds the four starting goals, disabled.

  timer / job kind     schedule              catch-up             runs      what
  autonomy.activity    every 30 min          skip                 detached  activity.refresh(): ranked workstreams
  autonomy.work        every 60 min          skip                 in core   checks the switch, the daily step limit
                                                                            and that a goal is enabled; only then
                                                                            queues one autonomy.step (a cheap check,
                                                                            no process start while autonomy is off)
  autonomy.step        queued by work        -                    detached  autowork.run_once(): one proven step
  autonomy.mine        19:40 daily           latest, 20 h         detached  miner.run(): mine, prove, shadow runs
  autonomy.report      20:50 daily           latest, 3 h          in core   one line per goal for the evening note

Detached handlers run under python -m waggle.core.jobrun, so a long agent step or a slow first read of the
session logs is killed by its timeout instead of freezing the core. Every handler is safe to run twice: the
activity model reads logs from saved offsets, the step limit is counted from the step records, the miner keeps
candidate state, the report remembers when it last ran."""
import time

from . import events

ACTIVITY, WORK, STEP, MINE, REPORT = ("autonomy.activity", "autonomy.work", "autonomy.step", "autonomy.mine",
                                      "autonomy.report")
KINDS = (ACTIVITY, WORK, STEP, MINE, REPORT)
TIMEOUTS = {ACTIVITY: 900.0, STEP: 2400.0, MINE: 900.0}
LEASE_S = 120.0


def _background(purpose, fn):
    """Run fn with its Claude calls marked as background work, in a copy of the caller's context."""
    import contextvars

    from . import budget

    def go():
        budget.set_background(purpose)
        return fn()
    return contextvars.copy_context().run(go)


def activity_job(payload=None, *, settings=None, now=None):
    from . import activity
    snap = activity.refresh(settings, now=now)
    return {"workstreams": len(snap["workstreams"]), "top": [w["key"] for w in snap["workstreams"][:3]],
            **{k: snap["stats"][k] for k in ("sessions", "seconds")}}


def work_job(payload=None, *, settings=None, now=None):
    """Queue one step when there is room for it; never starts work itself."""
    from waggle.core import jobs

    from . import autowork, goals
    from .config import Settings
    s = settings or Settings.load()
    now = now or time.time()
    if not goals.autonomy_enabled():
        return {"queued": False, "why": "autonomy is off"}
    done, limit = autowork.steps_today(now, s), goals.max_steps_per_day()
    if done >= limit:
        return {"queued": False, "why": f"{done} of {limit} steps today"}
    if not goals.enabled_goals(now):
        return {"queued": False, "why": "no enabled goal"}
    hour = time.strftime("%Y-%m-%d-%H", time.localtime(now))
    jid = jobs.enqueue(STEP, {}, idem_key=f"{STEP}:{hour}", now=now)
    return {"queued": True, "job": jid}


def step_job(payload=None, *, settings=None, **ctx):
    from . import autowork
    out = _background("autowork", lambda: autowork.run_once(settings, **ctx))
    events.log("autonomy", action="step", **{k: v for k, v in out.items() if k in ("goal", "outcome", "skipped")})
    return out


def mine_job(payload=None, *, settings=None, now=None, executor=None):
    from . import miner
    return miner.run(settings, now=now, executor=executor)


def report_job(payload=None, *, settings=None, now=None):
    from . import autowork
    return autowork.report(settings, now=now)


HANDLERS = {ACTIVITY: activity_job, WORK: work_job, STEP: step_job, MINE: mine_job, REPORT: report_job}
DETACHED = {ACTIVITY, STEP, MINE}


def timer_specs():
    """(timer id, kind, spec, job kind, catch_up, max_late_s)."""
    return [(ACTIVITY, "interval", "30m", ACTIVITY, "skip", None),
            (WORK, "interval", "60m", WORK, "skip", None),
            (MINE, "cron", "40 19 * * *", MINE, "latest", 20 * 3600),
            (REPORT, "cron", "50 20 * * *", REPORT, "latest", 3 * 3600)]


def register_jobs():
    from waggle.core import jobs
    for kind, fn in HANDLERS.items():
        jobs.register(kind, fn, detached=kind in DETACHED, max_attempts=1 if kind in (WORK, STEP) else 2,
                      lease_s=LEASE_S, timeout_s=TIMEOUTS.get(kind))
    return list(HANDLERS)


def register_core(now=None):
    """Called by the core process at every start: job kinds, timers, and the starting goals (disabled)."""
    from waggle.core import timers

    from . import goals
    report = {"jobs": register_jobs(), "timers": [], "seeded": []}
    for tid, kind, spec, job_kind, policy, max_late in timer_specs():
        timers.add(tid, kind, spec, job_kind, catch_up=policy, max_late_s=max_late, origin="autonomy", now=now)
        report["timers"].append(tid)
    try:
        report["seeded"] = goals.seed(now)
    except Exception as e:  # noqa: BLE001 - goals are optional for the jobs to load
        report["seed_error"] = f"{type(e).__name__}: {e}"[:200]
    return report
