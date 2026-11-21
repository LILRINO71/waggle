"""Knowledge on the core: intake, the daily digest and the nightly quiz as jobs and timers.

The core process calls register_core() at every start (waggle.core.plugins.MODULES lists this module). It
registers four job kinds and their timers; adding them again at the next start changes nothing.

  knowledge.intake  interval 30 min, catch-up skip. Runs whatever source is due (Claude Code memory mirror, git
                    repos, GitHub, calendar and mail, goals, graph, reindex). Each source keeps its own minimum gap
                    in knowledge.db, so a half-hourly pass does little most times. Sources that did not run (out
                    of budget, no connector) are reported under "skipped" with the reason, not as ran.
  knowledge.digest  cron 21:00, catch-up latest within 20 h, and again at 08:00 (skip). Writes the journal notes
                    from the user's own turns and distils finished days (background budget; at most two model
                    calls; a day already distilled is not distilled again).
  knowledge.quiz    cron 21:30, catch-up latest within 20 h. The knowledge quiz, growth numbers and the "Today I
                    learned" note. Fails (and is retried later) when retrieval is down: a score of 0 from a dead
                    embedder would be recorded as a baseline.
  knowledge.mailwatch  cron every 5 min 06:00-22:55; catch-up latest within 1 h (one run at start, never a burst).
                    Reads new inbox mail and texts the user once per new important message (waggle.mailwatch):
                    over IMAP with the local model (no Claude usage), or, only while IMAP is not set up, one Haiku
                    call every 20 min on its own narrow budget allowance.

The handlers run detached (python -m waggle.core.jobrun), so a stuck repo scan or model call is killed by the
job timeout instead of freezing the core, git and Claude children never flash a window, and the background
budget context of a run cannot leak into a chat turn. Every handler is safe to run twice: ingest keeps a lock
and per-source last-run times, the digest remembers which turns it distilled, the quiz and the notes are
rewritten with the same content. A handler returns a small report, or raises so the job fails and is retried."""
from . import events

INTAKE = "knowledge.intake"
DIGEST = "knowledge.digest"
QUIZ = "knowledge.quiz"
MAIL = "knowledge.mailwatch"
KINDS = (INTAKE, DIGEST, QUIZ, MAIL)
LEAD_LIMIT_S = 20 * 3600  # a missed nightly run is still worth running this long afterwards
LEASE_S = 120.0
MAIL_MAX_LATE_S = 3600  # a missed mail pass runs once at start if it is at most this late
TIMEOUTS = {INTAKE: 1200.0, DIGEST: 900.0, QUIZ: 900.0, MAIL: 600.0}


def _background(purpose, fn):
    """Run fn with its Claude calls marked as background work (the budget's rules apply). The mark lives in a copy
    of the caller's context, so it cannot leak into whatever the same thread does next (a chat turn, a test)."""
    import contextvars

    from . import budget

    def go():
        budget.set_background(purpose)
        return fn()
    return contextvars.copy_context().run(go)


def _late(payload):
    late = float((payload or {}).get("late_s") or 0)
    return {"late_s": int(late)} if late > 120 else {}


def intake(payload=None, *, settings=None, now=None, **ctx):
    """Job knowledge.intake: the sources that are due. Raises only when every source that ran failed.
    A source that returned {"skipped": reason} (budget deferred, no connector, no gh) did not run: it is listed
    under "skipped" with its reason, never under "ran". When the whole intake was skipped (another ingest is
    running) "skipped" is {"intake": reason}."""
    from . import ingest
    report = _background("ingest", lambda: ingest.run_due(now=now, settings=settings,
                                                          exclude=("digest", "learning"), **ctx))
    skipped = {k: str(v["skipped"])[:200] for k, v in report.items()
               if isinstance(v, dict) and v.get("skipped") and not v.get("error")}
    if isinstance(report.get("skipped"), str):
        skipped["intake"] = report["skipped"]
    errors = {k: v["error"] for k, v in report.items() if isinstance(v, dict) and v.get("error")}
    ran = [k for k, v in report.items() if isinstance(v, dict) and k not in skipped]
    if errors and len(errors) == len(ran):
        raise RuntimeError("every intake source failed: " + "; ".join(f"{k}: {e}" for k, e in errors.items())[:400])
    if skipped:
        events.log("knowledge", action="intake_skipped", skipped=skipped)
    return {"ran": ran, "errors": errors, **_late(payload), **({"skipped": skipped} if skipped else {})}


def digest(payload=None, *, settings=None, now=None, asker=None):
    """Job knowledge.digest: today's and the last two days' journal notes, finished days distilled once each."""
    from . import digest as digest_mod
    out = _background("digest", lambda: digest_mod.run(settings, now=now, asker=asker))
    if out.get("skipped") and not out.get("written") and not out.get("distilled"):
        # the model was out of budget or unreachable: the skeleton may still be written, but say it did not finish
        events.log("knowledge", action="digest_deferred", reasons=out["skipped"][:3])
    return {**out, **_late(payload)}


def quiz(payload=None, *, settings=None, now=None, embedder=None):
    """Job knowledge.quiz: the nightly quiz and learning notes. Raises when the quiz could not run."""
    from . import learning
    out = _background("learning", lambda: learning.daily(settings, now=now, embedder=embedder))
    if out["quiz"].get("error"):
        raise RuntimeError("quiz did not run: " + str(out["quiz"]["error"]))
    return {**out, **_late(payload)}


def mailwatch(payload=None, *, settings=None, now=None, asker=None, sleep=None):
    """Job knowledge.mailwatch: one pass of the mail watch (waggle.mailwatch.run_pass). A failed Claude-fallback
    call raises, so the job shows it; a failed IMAP pass does not (no instant retry: the watch counts failures and
    tells the user after three in a row). A deferral, a pacing skip or a run outside waking hours is a quiet skip."""
    from . import mailwatch as mw
    out = _background(mw.PURPOSE, lambda: mw.run_pass(settings=settings, now=now, asker=asker, sleep=sleep))
    if out.get("error") and out.get("path") != "imap":
        raise RuntimeError("mail watch failed: " + str(out["error"])[:300])
    return {**out, **_late(payload)}


HANDLERS = {INTAKE: intake, DIGEST: digest, QUIZ: quiz, MAIL: mailwatch}


def timer_specs():
    """(timer id, kind, spec, job kind, catch_up, max_late_s)."""
    return [(INTAKE, "interval", "30m", INTAKE, "skip", None),
            (DIGEST, "cron", "0 21 * * *", DIGEST, "latest", LEAD_LIMIT_S),
            (QUIZ, "cron", "30 21 * * *", QUIZ, "latest", LEAD_LIMIT_S),
            # a second chance next morning when the evening run found the model's budget spent
            (DIGEST + ".am", "cron", "0 8 * * *", DIGEST, "skip", None),
            # every 5 min in waking hours (IMAP costs nothing; the Claude fallback paces itself to 20 min)
            (MAIL, "cron", "*/5 6-22 * * *", MAIL, "latest", MAIL_MAX_LATE_S)]


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
