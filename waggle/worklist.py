"""One answer to "what are you working on?" (Step 4): every path that runs work, read in one place.

  night shift     waggle/nightshift.py   approved objectives, worked 23:00-07:00 (running, queued, parked, waiting
                                         for the owner's answer)
  objectives      waggle/objectives.py   multi-step goals started from chat (open ones)
  background      waggle/chat_jobs.py    background_task jobs from chat (queued or running)
  autonomy goals  waggle/goals.py        enabled goals of the hourly work loop
  routines        waggle/routines.py     enabled recurring routines and their next run
  core jobs       waggle/core/jobs.py    whatever the core is running right now (detached workers)

Read-only; each source that fails to read is skipped and named, never guessed."""
import json
import time


def _night():
    from . import nightshift
    out = []
    for t in nightshift.tasks(("running", "queued", "parked", "delivered")):
        if t["status"] == "delivered" and t.get("answer"):
            continue
        state = {"running": "working on it now", "queued": "queued for the night shift",
                 "parked": "stopped, needs you", "delivered": "delivered, waiting for your answer"}[t["status"]]
        out.append({"source": "night shift", "title": t["title"], "state": state,
                    "detail": t.get("pr_url") or (t.get("gap") or "")[:160]})
    return out


def _objectives():
    from . import objectives
    return [{"source": "objective", "title": o["title"], "state": o["status"],
             "detail": f"{o.get('done', 0)} of {o.get('total', 0)} steps"}
            for o in objectives.status() or [] if o["status"] in objectives.OPEN]


def _background():
    from . import chat_jobs
    return [{"source": "background task", "title": j["task"], "state": j["state"], "detail": ""}
            for j in chat_jobs.status(limit=20) if j["state"] in ("queued", "running")]


def _goals():
    from . import goals
    if not goals.autonomy_enabled():
        return []
    return [{"source": "autonomy goal", "title": g.get("title") or g.get("name") or g.get("id"), "state": "enabled",
             "detail": ""} for g in goals.enabled_goals()]


def _routines():
    from . import routines
    return [{"source": "routine", "title": r["name"], "state": "scheduled", "detail": f"next {r['next']}"}
            for r in routines.listing() if r.get("enabled") and r.get("next")]


def _core():
    from waggle.core import jobs
    return [{"source": "core job", "title": j["kind"], "state": "running",
             "detail": f"since {time.strftime('%H:%M', time.localtime(j['started']))}" if j.get("started") else ""}
            for j in jobs.list(state="running", limit=20) if j.get("detached")]


SOURCES = (("night shift", _night), ("objectives", _objectives), ("background tasks", _background),
           ("autonomy goals", _goals), ("routines", _routines), ("core jobs", _core))


def items():
    """(items, unreadable source names)."""
    out, bad = [], []
    for name, fn in SOURCES:
        try:
            out.extend(fn())
        except Exception:  # noqa: BLE001 - one broken source must not hide the others
            bad.append(name)
    return out, bad


def answer():
    """A short plain-text answer, active work first."""
    rows, bad = items()
    order = {"working on it now": 0, "running": 0, "active": 1, "planning": 1, "queued": 2,
             "queued for the night shift": 2, "stopped, needs you": 3, "waiting_approval": 3, "blocked": 3,
             "delivered, waiting for your answer": 4}
    rows.sort(key=lambda r: order.get(r["state"], 5))
    if not rows:
        text = "Nothing is running or queued right now."
    else:
        text = "\n".join(f"- {r['source']}: {r['title']} ({r['state']}{'; ' + r['detail'] if r['detail'] else ''})"
                         for r in rows[:15]) + (f"\n(+{len(rows) - 15} more)" if len(rows) > 15 else "")
    if bad:
        text += f"\n(could not read: {', '.join(bad)})"
    return text


def _t_work_status(args):
    rows, bad = items()
    return json.dumps({"summary": answer(), "items": rows[:30], "unreadable": bad}, ensure_ascii=False)


MCP_TOOLS = [({"name": "work_status", "description": "What Waggle is working on right now, across every path: the "
               "night shift (approved objectives), objectives, background tasks, autonomy goals, routines and "
               "running core jobs. Use for 'what are you working on', 'what's running', 'what did the night "
               "shift do'.", "parameters": {"type": "object", "properties": {}, "required": []}}, _t_work_status)]
