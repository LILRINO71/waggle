"""Knowledge on the core: intake, the daily digest and the nightly quiz as jobs and timers.

The core process calls register_core() at every start (waggle.core.plugins.MODULES lists this module). It
registers three job kinds and three timers; adding them again at the next start changes nothing.

  knowledge.intake  interval 30 min, catch-up skip. Runs whatever source is due (Claude Code memory mirror, git
                    repos, GitHub, calendar and mail, goals, graph, reindex). Each source keeps its own minimum gap
                    in knowledge.db, so a half-hourly pass does little most times.
  knowledge.digest  cron 21:00, catch-up latest within 20 h, and again at 08:00 (skip). Writes the journal notes
                    from the user's own turns and distils finished days (background budget; at most two model
                    calls; a day already distilled is not distilled again).
  knowledge.quiz    cron 21:30, catch-up latest within 20 h. The knowledge quiz, growth numbers and the "Today I
                    learned" note. Fails (and is retried later) when retrieval is down: a score of 0 from a dead
                    embedder would be recorded as a baseline.

The handlers run detached (python -m waggle.core.jobrun), so a stuck repo scan or model call is killed by the
job timeout instead of freezing the core, git and Claude children never flash a window, and the background
budget context of a run cannot leak into a chat turn. Every handler is safe to run twice: ingest keeps a lock
and per-source last-run times, the digest remembers which turns it distilled, the quiz and the notes are
rewritten with the same content. A handler returns a small report, or raises so the job fails and is retried."""
from . import events

INTAKE = "knowledge.intake"
DIGEST = "knowledge.digest"
QUIZ = "knowledge.quiz"
KINDS = (INTAKE, DIGEST, QUIZ)
LEAD_LIMIT_S = 20 * 3600  # a missed nightly run is still worth running this long afterwards
LEASE_S = 120.0
TIMEOUTS = {INTAKE: 1200.0, DIGEST: 900.0, QUIZ: 900.0}


def _background(purpose):
    """Mark this process's Claude calls as background work (the budget's rules apply)."""
    from . import budget
    budget.set_background(purpose)


def _late(payload):
    late = float((payload or {}).get("late_s") or 0)
    return {"late_s": int(late)} if late > 120 else {}


def intake(payload=None, *, settings=None, now=None, **ctx):
    """Job knowledge.intake: the sources that are due. Raises only when every source that ran failed."""
    from . import ingest
    _background("ingest")
    report = ingest.run_due(now=now, settings=settings, exclude=("digest", "learning"), **ctx)
    errors = {k: v["error"] for k, v in report.items() if isinstance(v, dict) and v.get("error")}
    ran = [k for k, v in report.items() if isinstance(v, dict)]
    if errors and len(errors) == len(ran):
        raise RuntimeError("every intake source failed: " + "; ".join(f"{k}: {e}" for k, e in errors.items())[:400])
    return {"ran": ran, "errors": errors, **_late(payload),
            **({"skipped": report["skipped"]} if "skipped" in report else {})}


def digest(payload=None, *, settings=None, now=None, asker=None):
    """Job knowledge.digest: today's and the last two days' journal notes, finished days distilled once each."""
    from . import digest as digest_mod
    _background("digest")
    out = digest_mod.run(settings, now=now, asker=asker)
    if out.get("skipped") and not out.get("written") and not out.get("distilled"):
        # the model was out of budget or unreachable: the skeleton may still be written, but say it did not finish
        events.log("knowledge", action="digest_deferred", reasons=out["skipped"][:3])
    return {**out, **_late(payload)}


def quiz(payload=None, *, settings=None, now=None, embedder=None):
    """Job knowledge.quiz: the nightly quiz and learning notes. Raises when the quiz could not run."""
    from . import learning
    _background("learning")
    out = learning.daily(settings, now=now, embedder=embedder)
    if out["quiz"].get("error"):
        raise RuntimeError("quiz did not run: " + str(out["quiz"]["error"]))
    return {**out, **_late(payload)}


HANDLERS = {INTAKE: intake, DIGEST: digest, QUIZ: quiz}


def timer_specs():
    """(timer id, kind, spec, job kind, catch_up, max_late_s)."""
    return [(INTAKE, "interval", "30m", INTAKE, "skip", None),
            (DIGEST, "cron", "0 21 * * *", DIGEST, "latest", LEAD_LIMIT_S),
            (QUIZ, "cron", "30 21 * * *", QUIZ, "latest", LEAD_LIMIT_S),
            # a second chance next morning when the evening run found the model's budget spent
            (DIGEST + ".am", "cron", "0 8 * * *", DIGEST, "skip", None)]


def register_jobs():
    from waggle.core import jobs
    for kind, fn in HANDLERS.items():
        jobs.register(kind, fn, detached=True, max_attempts=2, lease_s=LEASE_S, timeout_s=TIMEOUTS[kind])
    return list(HANDLERS)


def register_core(now=None):
    """Called by the core process at every start: the job kinds and the timers (idempotent). Returns both."""
    from waggle.core import timers
    report = {"jobs": register_jobs(), "timers": []}
    for tid, kind, spec, job_kind, policy, max_late in timer_specs():
        timers.add(tid, kind, spec, job_kind, catch_up=policy, max_late_s=max_late, origin="knowledge", now=now)
        report["timers"].append(tid)
    return report
