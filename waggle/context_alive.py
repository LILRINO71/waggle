"""Is the context step alive? (the "hooks alive" check, after LifeOS's HooksAlive: every part of the context is
optional and fails quietly, so a bug that silences it looks like nothing at all.)

Every chat turn records on its trace (extra.context, written by chat_engine.run_turn from the block build_context
made) whether the core block was in the message and whether the lessons step ran. This reads those marks:

  fail  there are recent chat turns carrying the marker field (or, for the daily canary, a turn the canary just
        caused) and none of them carried the core block: the context step stopped working.
  fail  the owner has lessons stored but no recent marked turn ran the lessons step.
  warn  recent chat turns exist and none has ever carried a marker (this code has not answered a turn yet, or the
        marker writing broke: send a message, run the check again).
  ok    otherwise, including when there are no recent chat turns (nothing to judge).

Turns from before the marker existed are ignored (only turns from the first marked turn on count), so the check is
not red on the day this ships. Read-only. Used by doctor.py (check_context) and canary.py (a "context" check after
the round trip)."""
import json
import sqlite3
import time

from .config import data_path

WINDOW_S = 24 * 3600
MIN_TURNS = 2  # fewer recent turns than this are not enough to call anything dead (the canary passes 1)
RECENT_TURNS = 4  # only the newest turns are judged: a break after some good turns must still show


def _read(since):
    """[(id, time, marker dict or None)] of chat-route turns at or after `since`, plus the time of the first marked
    turn ever (None when there is none). Direct SQL: the check must work even when a trace helper is broken."""
    path = data_path("traces.db")
    if not path.exists():
        return [], None
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        first = con.execute("SELECT MIN(time) FROM traces WHERE route='chat' AND extra LIKE '%\"context\": {%'"
                            ).fetchone()[0]
        rows = con.execute("SELECT id, time, extra FROM traces WHERE route='chat' AND time>=? ORDER BY id",
                           (since,)).fetchall()
    except sqlite3.Error:
        return [], None
    finally:
        con.close()
    out = []
    for tid, t, extra in rows:
        try:
            ctx = (json.loads(extra or "{}") or {}).get("context")
        except ValueError:
            ctx = None
        out.append((tid, t, ctx if isinstance(ctx, dict) else None))
    return out, first


def check(now=None, *, window_s=WINDOW_S, min_turns=MIN_TURNS, since=None, strict=False):
    """{"status": ok|warn|fail, "detail", "fix", "turns", "carried", "evaluated"}.
    since: only turns at or after this time (the canary: the turn it just caused). strict: a marker that never
    appeared at all counts as a failure, not a warning."""
    now = time.time() if now is None else now
    start = max(now - window_s, since or 0)
    rows, first = _read(start)
    res = {"status": "ok", "detail": "", "fix": "", "turns": len(rows), "carried": 0, "evaluated": False}
    if len(rows) < min_turns:
        res["detail"] = f"{len(rows)} recent chat turn(s): not enough to judge" if rows else "no recent chat turns"
        return res
    if first is None:
        res.update(evaluated=True,
                   status="fail" if strict else "warn",
                   detail=f"{len(rows)} recent chat turn(s) and none has ever recorded what its context carried",
                   fix="Send Waggle a message and run the check again; if it stays like this, "
                       "chat_engine.build_context or its trace marker (extra.context) broke.")
        return res
    rows = [r for r in rows if r[1] >= first][-RECENT_TURNS:]  # before the marker existed: cannot be judged
    res["turns"] = len(rows)
    if len(rows) < min_turns:
        res["detail"] = "no recent turn since the marker was introduced"
        return res
    res["evaluated"] = True
    marked = [m for _, _, m in rows if m]
    carried = [m for m in marked if m.get("core")]
    res["carried"] = len(carried)
    if not carried:
        why = next((m.get("error") for m in marked if m.get("error")), "")
        res.update(status="fail",
                   detail=f"{len(rows)} recent chat turn(s) and none carried the core context block"
                          + (f" (last error: {why})" if why else "") + ": the context step has silently stopped",
                   fix="Look at events.jsonl for context_error / engine_error rows and at "
                       "chat_engine.build_context; restart the core after fixing it.")
        return res
    try:
        from . import lessons
        stored = lessons.enabled() and bool(lessons.load_all())
    except Exception:  # noqa: BLE001
        stored = False
    if stored and not any(m.get("lessons_checked") for m in marked):
        res.update(status="fail",
                   detail=f"lessons are stored but none of {len(rows)} recent chat turn(s) ran the lessons step",
                   fix="Check chat_engine.build_context (the 'lessons' step) and restart the core.")
        return res
    res["detail"] = f"{len(carried)} of {len(rows)} recent chat turn(s) carried the core context block"
    return res


def as_doctor(now=None):
    """(status, detail, fix) for waggle doctor."""
    r = check(now)
    return r["status"], r["detail"], r["fix"]
