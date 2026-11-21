"""Scheduled agent routines: "every Monday at 7, check X and tell me", run by a specialist agent.

- Stored in ~/.waggle/routines.json, so they survive restarts; a Hermes cron job (waggle-routines, every 5 min)
  calls tick(), which runs at most one due routine per tick (bounded cost) and recovers interrupted runs.
- Schedules: 5-field cron ("0 7 * * 1"), "every 30m / 2h / 1d", "daily 21:30", "weekly mon 07:00",
  "weekdays 08:00", and one-shots: "once 2026-11-20 19:00" ("remind me at 7", "call me at 7 tomorrow").
- One-shot reminders and "call me at 7" are not routines: they are core timers (followups.add, call.schedule_at).
  A "once" schedule here runs an agent routine once and is then marked done.
- Delivery: telegram (the user's phone), vault (Routines/<name>.md log), or both. Monitor-style routines answer
  NOTHING_NEW when there's nothing worth saying, and then stay quiet, unless the routine is always_report (the
  nightly review feeds the evening note, which always goes out). Results are checked by a cheap verify
  step before they are pushed to the phone.
- Pause / resume / remove / run now; quota-aware (skips when Waggle's share of the Claude budget is used up).
"""
import datetime as dt
import json
import re
import time
import uuid

from . import agents, notify, notifycheck, quota, vault
from .config import Settings, data_path

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
QUIET = "NOTHING_NEW"
DEFAULTS = [
    # Feeds the evening note (brief.evening_note, always sent): reports to the vault, never answers NOTHING_NEW.
    {"name": "nightly-project-review", "schedule": "daily 20:30", "agent": "monitor", "deliver": "vault",
     "always_report": True,
     "task": "Review the user's active projects: check each repo under C:\\Users\\hiheo\\Claude for uncommitted or "
             "unpushed work (git status / git log origin..HEAD), projects whose notes have no next action, and "
             "anything stale for 7+ days. Update project notes with project_update when you find something real. "
             "Report the 1-3 things that matter tomorrow."},
    {"name": "weekly-review", "schedule": "weekly sun 18:00", "agent": "planner", "deliver": "both",
     "task": "Write the user's weekly review: what got done this week (recall + project logs), what's open, and the "
             "top 3 priorities for next week with dates. Save it to the vault as Journal/Weekly Review <date>.md. "
             "Then give a 4-sentence summary."},
]
# Earlier default definitions: a routine still exactly like this was never edited by the user and is upgraded.
OLD_DEFAULTS = {"nightly-project-review": {"schedule": "daily 21:30", "deliver": "both"}}


def _load():
    p = data_path("routines.json")
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def _save(data):
    data_path("routines.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


# ---- schedules -----------------------------------------------------------------------------------

def _field(spec, lo, hi):
    vals = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step = part.split("/")
            step = int(step)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-"))
        else:
            a = b = int(part)
        vals.update(range(a, b + 1, step))
    return vals


def normalize(schedule):
    """Friendly schedule -> 5-field cron, ('every', seconds) or ('once', timestamp)."""
    s = (schedule or "").strip().lower()
    m = re.fullmatch(r"(?:once|at)\s+(\d{4}-\d{2}-\d{2})[ t](\d{1,2}):(\d{2})", s)
    if m:
        return ("once", dt.datetime.fromisoformat(f"{m.group(1)} {int(m.group(2)):02d}:{m.group(3)}").timestamp())
    m = re.fullmatch(r"every\s+(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)", s)
    if m:
        n, u = int(m.group(1)), m.group(2)[0]
        return ("every", n * {"m": 60, "h": 3600, "d": 86400}[u])
    m = re.fullmatch(r"(daily|every day|weekdays|weekends)\s+(?:at\s+)?(\d{1,2}):(\d{2})", s)
    if m:
        dow = {"daily": "*", "every day": "*", "weekdays": "1-5", "weekends": "0,6"}[m.group(1)]
        return f"{int(m.group(3))} {int(m.group(2))} * * {dow}"
    m = re.fullmatch(r"(?:weekly|every)\s+(mon|tue|wed|thu|fri|sat|sun)\w*\s+(?:at\s+)?(\d{1,2}):(\d{2})", s)
    if m:
        cron_dow = (DAYS[m.group(1)] + 1) % 7  # cron: 0 = Sunday
        return f"{int(m.group(3))} {int(m.group(2))} * * {cron_dow}"
    if len(s.split()) == 5:
        return s
    raise ValueError(f"can't read schedule {schedule!r}; try 'daily 21:30', 'weekly mon 07:00', 'every 2h' or cron")


def next_run(schedule, after=None):
    after = after or time.time()
    norm = normalize(schedule)
    if isinstance(norm, tuple) and norm[0] == "once":
        return norm[1] if norm[1] > after else None
    if isinstance(norm, tuple):
        return after + norm[1]
    minute, hour, dom, mon, dow = norm.split()
    mins, hours = _field(minute, 0, 59), _field(hour, 0, 23)
    doms, mons, dows = _field(dom, 1, 31), _field(mon, 1, 12), _field(dow, 0, 6)
    t = dt.datetime.fromtimestamp(after).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(60 * 24 * 370):
        cron_dow = (t.weekday() + 1) % 7
        if (t.minute in mins and t.hour in hours and t.month in mons and
                (t.day in doms if dom != "*" else True) and (cron_dow in dows if dow != "*" else True)):
            return t.timestamp()
        t += dt.timedelta(minutes=1)
    raise ValueError("schedule never fires")


# ---- CRUD ----------------------------------------------------------------------------------------

def add(name, schedule, task, agent="general", deliver="telegram", enabled=True, always_report=False, source="",
        gate=None):
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:50] or "routine"
    nxt = next_run(schedule)  # validates
    if nxt is None:
        raise ValueError(f"{schedule!r} is in the past")
    if agent not in agents.AGENTS:
        agent = "general"
    data = _load()
    rid = next((k for k, v in data.items() if v["name"] == name), uuid.uuid4().hex[:8])
    prior_gate = data.get(rid, {}).get("gate")
    data[rid] = {"name": name, "schedule": schedule, "task": task, "agent": agent, "deliver": deliver,
                 "enabled": enabled, "created": data.get(rid, {}).get("created", time.time()), "next_run": nxt,
                 "last_run": data.get(rid, {}).get("last_run"), "last_status": data.get(rid, {}).get("last_status"),
                 "runs": data.get(rid, {}).get("runs", 0), "always_report": bool(always_report),
                 "source": source or ""}
    if gate or prior_gate:  # an optional cheap pre-check (see _gate_for): {"question": ...} or {"name": ...}
        data[rid]["gate"] = gate or prior_gate
    _save(data)
    return rid


def ensure_defaults():
    data = _load()
    have = {v["name"]: k for k, v in data.items()}
    for d in DEFAULTS:
        old = OLD_DEFAULTS.get(d["name"])
        rid = have.get(d["name"])
        if rid is None:
            add(d["name"], d["schedule"], d["task"], d["agent"], d["deliver"], always_report=d.get("always_report"))
        elif old and all(data[rid].get(k) == v for k, v in old.items()) and data[rid].get("task") == d["task"]:
            add(d["name"], d["schedule"], d["task"], d["agent"], d["deliver"], enabled=data[rid].get("enabled", True),
                always_report=d.get("always_report"))


def find(key):
    data = _load()
    if key in data:
        return key
    for k, v in data.items():
        if v["name"] == key:
            return k
    return None


def set_enabled(key, enabled):
    rid = find(key)
    if not rid:
        return False
    data = _load()
    data[rid]["enabled"] = enabled
    if enabled:
        data[rid]["next_run"] = next_run(data[rid]["schedule"])
    _save(data)
    return True


def remove(key):
    rid = find(key)
    if not rid:
        return False
    data = _load()
    data.pop(rid)
    _save(data)
    return True


def listing():
    out = []
    for rid, v in _load().items():
        out.append({"id": rid, **{k: v.get(k) for k in ("name", "schedule", "agent", "deliver", "enabled",
                                                         "last_status", "runs")},
                    "task": v["task"][:200],
                    "next": time.strftime("%a %m-%d %H:%M", time.localtime(v["next_run"])) if v.get("next_run") else None,
                    "last": time.strftime("%a %m-%d %H:%M", time.localtime(v["last_run"])) if v.get("last_run") else None})
    return sorted(out, key=lambda r: r["name"])


# ---- running -------------------------------------------------------------------------------------

def _gate_for(r, s):
    """The cheap pre-check of a routine, as a zero-argument function for gates.evaluate, or None. r["gate"] is
    {"name": <a function registered with gates.register>} or {"question": <a yes/no question for the local model>}
    (a plain string is a question). A routine that always reports (it feeds the evening note) never has one."""
    from . import gates
    g = r.get("gate")
    if not g or r.get("always_report"):
        return None
    g = {"question": g} if isinstance(g, str) else g
    fn = gates.registered(g.get("name")) if isinstance(g, dict) and g.get("name") else None
    if fn:
        return lambda: fn(routine=r, settings=s)
    q = g.get("question") if isinstance(g, dict) else None
    if q and getattr(s, "gates_local", True) and gates.ollama_ready(s):
        return gates.local_check(q, f"The routine '{r['name']}' is about to run this task:\n{r['task'][:1500]}",
                                 settings=s)
    return None


def run_one(rid, settings=None, run_fn=None, verify_fn=None, now=None):
    """Run one routine now; returns a status string."""
    s = settings or Settings.load()
    data = _load()
    r = data[rid]
    now = now or time.time()
    check = _gate_for(r, s)
    if check is not None:
        from . import gates
        gv = gates.evaluate(f"routine.{r['name']}", check, settings=s, now=now,
                            quiet_hold=r.get("deliver") in ("telegram", "both"), subject=r["name"])
        if gv.skip:  # nothing to look at: no Claude run, the schedule moves on
            _finish_run(data[rid], now, f"skipped by the gate: {gv.reason}"[:200])
            _save(data)
            return "gate-skipped"
        if gv.hold:  # would reach the owner in quiet hours: wait for the morning
            data[rid]["next_run"] = max(gv.until or now + 3600, now + 300)
            data[rid]["last_status"] = f"held: {gv.reason}"[:200]
            _save(data)
            return "held"
    model = agents.model_for(r["agent"], s)
    ok, why = quota.can_escalate(model, s)
    if not ok:
        data[rid]["next_run"] = now + 3600  # try again in an hour
        data[rid]["last_status"] = f"skipped: {why}"
        _save(data)
        return "skipped"
    from . import budget
    try:  # routines are background work: they need room in the background budget (atomic with other workers)
        ticket = budget.reserve(model, "routine", None, None, s)
    except budget.Deferred as e:
        data[rid]["next_run"] = max(now + 300, min(e.until, now + 6 * 3600))
        data[rid]["last_status"] = f"deferred: {e.reason}"
        _save(data)
        return "skipped"
    data[rid].update(running_since=now)
    _save(data)
    if r.get("always_report"):
        task = (f"{r['task']}\n\n(This is the scheduled routine '{r['name']}'. Always reply with a short report of at "
                "most 3 lines, even when little changed; never reply NOTHING_NEW.)")
    else:
        task = (f"{r['task']}\n\n(This is the scheduled routine '{r['name']}'. If nothing needs the user's "
                f"attention, reply exactly {QUIET}.)")
    try:
        with budget.using(ticket, "routine"):
            res = (run_fn or agents.run)(r["agent"], task, session=f"routine-{rid}", settings=s, purpose="routine")
    finally:
        budget.release(ticket)
    text = (res.text or "").strip()
    status = "ok" if res.ok else f"error: {res.error[:120]}"
    quiet = text.strip().upper().startswith(QUIET)
    if r.get("always_report") and res.ok and (quiet or not text):
        text, quiet = "Checked the repos and project notes: nothing changed that needs you tomorrow.", False
    if res.ok and not quiet and text:
        if r["deliver"] in ("vault", "both"):
            vault.append(f"Routines/{r['name']}", f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n\n{text}\n")
        if r["deliver"] in ("telegram", "both"):
            # Did the result meet the task, and is it worth a message now? No (or the local model failed): the
            # evening note carries it, and the full text is kept in the vault. A routine that always reports
            # already feeds the evening note, so it is not asked.
            nc = ({"notify": True} if r.get("always_report") else
                  notifycheck.decide(r["task"], text, settings=s, subject=r["name"]))
            if not nc["notify"]:
                if r["deliver"] == "telegram":
                    vault.append(f"Routines/{r['name']}", f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n\n{text}\n")
                from . import initiative
                initiative.keep_for_evening(f"routine-result:{rid}:{int(now)}", "routine",
                                            f"Routine {r['name']}: {text.splitlines()[0] if text else ''}", text, now)
            else:
                complete, missing = (verify_fn or agents.verify)(r["task"], text, settings=s)
                note = ("\n\n(Not double-checked: no budget for the check.)" if complete and str(missing).startswith(
                    "(not verified") else "" if complete else f"\n\n(Waggle's check: may be incomplete: {missing[:200]})")
                notify.send(f"🔁 {r['name']}\n\n{text[:3200]}{note}", origin="routine", source=r.get("source"))
    elif not res.ok and r["deliver"] in ("telegram", "both"):
        notify.send(f"🔁 Routine '{r['name']}' failed: {res.error[:300]}", origin="routine", source=r.get("source"))
    data = _load()
    if rid in data:
        data[rid].pop("running_since", None)
        _finish_run(data[rid], now, "quiet" if quiet else status)
        _save(data)
    return "quiet" if quiet else status


def _finish_run(r, now, status):
    nxt = next_run(r["schedule"], after=now)
    r.update(last_run=now, last_status=status, runs=r.get("runs", 0) + 1, next_run=nxt)
    if nxt is None:  # a one-shot: done, kept for the record
        r.update(enabled=False, last_status=status if status.startswith("done") else f"done ({status})")


def tick(settings=None, run_fn=None, verify_fn=None, now=None):
    """Called by cron every few minutes: recover crashed runs, then run the most overdue routine."""
    now = now or time.time()
    data = _load()
    changed = False
    for rid, r in data.items():
        if r.get("running_since") and now - r["running_since"] > 3600:  # crashed mid-run (PC slept, gateway died)
            r.pop("running_since")
            r["last_status"] = "interrupted; retrying"
            r["next_run"] = now
            changed = True
    if changed:
        _save(data)
    due = sorted(((r["next_run"], rid) for rid, r in data.items()
                  if r.get("enabled") and not r.get("running_since") and r.get("next_run") is not None
                  and r["next_run"] <= now))
    if not due:
        return None
    return due[0][1], run_one(due[0][1], settings, run_fn, verify_fn, now)


def main():
    from . import budget
    budget.set_background("routine")
    ensure_defaults()
    try:
        from . import jobs
        jobs.recover()
    except Exception:
        pass
    try:
        from . import objectives
        objectives.supervise()
    except Exception:
        pass
    tick()


# ---- MCP -----------------------------------------------------------------------------------------

def _t_add(args):
    rid = add(args["name"], args["schedule"], args["task"], args.get("agent", "general"),
              args.get("deliver", "telegram"))
    r = _load()[rid]
    return json.dumps({"id": rid, "next": time.strftime("%a %Y-%m-%d %H:%M", time.localtime(r["next_run"]))})


MCP_TOOLS = [
    ({"name": "routine_add", "description": "Schedule a recurring task run by a Waggle agent and delivered to the "
      "user. schedule: 'daily 21:30', 'weekdays 07:00', 'weekly mon 07:00', 'every 2h', or 5-field cron. agent: "
      "general, monitor (reports only changes), researcher, planner, coder, robotics, tutor. deliver: telegram, "
      "vault or both. Use for reminders that repeat, recurring research, monitoring.",
      "parameters": {"type": "object", "properties": {
          "name": {"type": "string"}, "schedule": {"type": "string"}, "task": {"type": "string"},
          "agent": {"type": "string"}, "deliver": {"type": "string", "enum": ["telegram", "vault", "both"]}},
          "required": ["name", "schedule", "task"]}}, _t_add),
    ({"name": "routine_list", "description": "List scheduled routines with next/last run and status.",
      "parameters": {"type": "object", "properties": {}, "required": []}}, lambda a: json.dumps(listing())),
    ({"name": "routine_pause", "description": "Pause a routine (by name or id).",
      "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
     lambda a: json.dumps({"paused": set_enabled(a["name"], False)})),
    ({"name": "routine_resume", "description": "Resume a paused routine.",
      "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
     lambda a: json.dumps({"resumed": set_enabled(a["name"], True)})),
    ({"name": "routine_remove", "description": "Delete a routine.",
      "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
     lambda a: json.dumps({"removed": remove(a["name"])})),
]


if __name__ == "__main__":
    main()
