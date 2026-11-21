"""Waggle's old Hermes cron duties as core timers (the Hermes entries are retired by install.py: retire_cron).

  timer / job kind  schedule (local)      catch-up                  what it does
  duty.hq           every 1 min           skip                      rewrites the Obsidian HQ dashboard (hq.write)
  duty.routines     every 5 min           skip                      legacy job recovery, objectives supervisor, then
                                                                    the most overdue routine (routines.tick)
  duty.graph        0 */6 * * *           latest, max 6 h late      knowledge graph: chat extraction + stats (the
                                                                    vault part comes from knowledge.intake's graph)
  duty.consolidate  0 18 * * 0 (Sun)      latest, max 12 h late     weekly tidy of the profile notes (silent)
  duty.learn        0 19 * * 0 (Sun)      latest, max 12 h late     weekly traces -> skills; one text when it learned

Not here, because the core already does them: waggle-proactive, waggle-digest, waggle-brief and waggle-calls only
ran tick.main() (the outreach.look / brief / evening / call timers) and waggle-ingest is knowledge.intake.

Rules every handler follows
- The OFF file (waggle/core/offswitch.py) wins: a duty that runs while Waggle is switched off does nothing.
- A pass already running (a long routine, a slow graph run) is never run twice at once: the second one skips.
- Claude calls are background work (the background budget's rules apply). Quiet hours are kept by the outbox:
  duty.learn's text is queued with notify.deliver, which holds it until the morning.
"""
import contextvars
import datetime as dt
import time

from . import events

HQ = "duty.hq"
ROUTINES = "duty.routines"
GRAPH = "duty.graph"
CONSOLIDATE = "duty.consolidate"
LEARN = "duty.learn"
KINDS = (HQ, ROUTINES, GRAPH, CONSOLIDATE, LEARN)
DETACHED = {ROUTINES, GRAPH, CONSOLIDATE, LEARN}  # long or model-bound: own process, killed on timeout
TIMEOUTS = {HQ: 120.0, ROUTINES: 2400.0, GRAPH: 1500.0, CONSOLIDATE: 900.0, LEARN: 1500.0}
LEASE_S = 120.0
WEEKLY_MAX_LATE_S = 12 * 3600
GRAPH_MAX_LATE_S = 6 * 3600
INTAKE_GRAPH_FRESH_S = 3 * 3600  # knowledge.intake rebuilt the graph's vault part this recently: do not redo it


def timer_specs():
    """(timer id, kind, spec, job kind, catch_up, max_late_s): the same schedules the Hermes entries had."""
    return [(HQ, "interval", "1m", HQ, "skip", None),
            (ROUTINES, "interval", "5m", ROUTINES, "skip", None),
            (GRAPH, "cron", "0 */6 * * *", GRAPH, "latest", GRAPH_MAX_LATE_S),
            (CONSOLIDATE, "cron", "0 18 * * 0", CONSOLIDATE, "latest", WEEKLY_MAX_LATE_S),
            (LEARN, "cron", "0 19 * * 0", LEARN, "latest", WEEKLY_MAX_LATE_S)]


def _background(purpose, fn):
    """Run fn as background Claude work; the mark lives in a copy of the context, so it cannot leak."""
    from . import budget

    def go():
        budget.set_background(purpose)
        return fn()
    return contextvars.copy_context().run(go)


def _off():
    from waggle.core import offswitch
    return offswitch.is_off()


def _other_running(kind, job):
    """True when another job of this kind is running right now (the one being run is `job`)."""
    from waggle.core import jobs
    me = (job or {}).get("id")
    return any(j["id"] != me for j in jobs.list(state="running", kind=kind, limit=20))


def _guard(kind, job):
    """None to go ahead, or the reason to skip."""
    if _off():
        return "Waggle is switched off"
    if job is not None and _other_running(kind, job):
        return "another run is in progress"
    return None


# ---- handlers --------------------------------------------------------------------------------------------

def hq_job(payload=None, *, settings=None):
    """Job duty.hq: rewrite the HQ dashboard. Local files only, so it runs inside the core."""
    if _off():
        return {"skipped": "Waggle is switched off"}
    from . import hq
    hq.write(settings)
    return {"written": True}


def routines_job(payload=None, job=None, *, settings=None, now=None, run_fn=None):
    """Job duty.routines: what waggle-routines did every 5 minutes (routines.main)."""
    why = _guard(ROUTINES, job)
    if why:
        return {"skipped": why}

    def go():
        from . import objectives, routines
        from . import jobs as legacy_jobs
        routines.ensure_defaults()
        for name, step in (("jobs", legacy_jobs.recover), ("objectives", objectives.supervise)):
            try:  # each step on its own: one failing must not stop the routines
                step()
            except Exception as e:  # noqa: BLE001
                events.log("error", where=f"duty.routines.{name}", error=f"{type(e).__name__}: {e}"[:300])
        res = routines.tick(settings, run_fn=run_fn, now=now)
        return {"ran": res[0], "status": res[1]} if res else {"ran": None}
    return _background("routine", go)


def graph_job(payload=None, job=None, *, settings=None, now=None):
    """Job duty.graph: the knowledge graph's chat extraction and stats; the vault part only when the intake did not
    just do it."""
    why = _guard(GRAPH, job)
    if why:
        return {"skipped": why}
    from . import graph, kstore
    now = now or time.time()
    last = (kstore.get("ingest.last_run", {}) or {}).get("graph", 0)
    out = _background("graph", lambda: graph.refresh(settings, vault_too=now - last > INTAKE_GRAPH_FRESH_S))
    return {"vault": out["vault"] if isinstance(out["vault"], str) else "rebuilt", "chat": out.get("chat"),
            "stats": out.get("stats")}


def consolidate_job(payload=None, job=None, *, settings=None):
    """Job duty.consolidate: the weekly profile tidy-up. Silent: the profile note changes, nothing is sent."""
    why = _guard(CONSOLIDATE, job)
    if why:
        return {"skipped": why}
    from . import learn
    return {"result": _background("consolidate", lambda: learn.consolidate(settings))}


def learn_job(payload=None, job=None, *, settings=None, now=None):
    """Job duty.learn: the weekly traces -> skills run. Sends one text only when it learned something."""
    why = _guard(LEARN, job)
    if why:
        return {"skipped": why}
    from . import learning, notify
    from .config import Settings
    s = settings or Settings.load()
    out = _background("learning", lambda: learning.run(settings=s))
    line = learning.summary_line(out)
    sched = float((payload or {}).get("scheduled_ts") or now or time.time())
    row = None
    if line:
        week = dt.date.fromtimestamp(sched).isocalendar()
        row = notify.deliver("text", line, origin="learn", ref=f"{week[0]}-W{week[1]:02d}", topic="learn", settings=s,
                             now=now)
    return {"drafted": len(out["drafted"]), "sent": bool(line), "outbox": (row or {}).get("id")}


HANDLERS = {HQ: hq_job, ROUTINES: routines_job, GRAPH: graph_job, CONSOLIDATE: consolidate_job, LEARN: learn_job}


def register_jobs():
    from waggle.core import jobs
    for kind, fn in HANDLERS.items():
        jobs.register(kind, fn, detached=kind in DETACHED, max_attempts=1, lease_s=LEASE_S,
                      timeout_s=TIMEOUTS[kind], with_job=kind in DETACHED)
    return list(HANDLERS)


def register_core(now=None):
    """Called by the core process at every start (idempotent): the job kinds and the timers."""
    from waggle.core import timers
    report = {"jobs": register_jobs(), "timers": []}
    for tid, kind, spec, job_kind, policy, max_late in timer_specs():
        timers.add(tid, kind, spec, job_kind, catch_up=policy, max_late_s=max_late, origin="duties", now=now)
        report["timers"].append(tid)
    return report
