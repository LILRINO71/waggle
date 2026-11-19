"""Scheduled agent routines: "every Monday at 7, check X and tell me", run by a specialist agent.

- Stored in ~/.waggle/routines.json, so they survive restarts; a Hermes cron job (waggle-routines, every 5 min)
  calls tick(), which runs at most one due routine per tick (bounded cost) and recovers interrupted runs.
- Schedules: 5-field cron ("0 7 * * 1"), "every 30m / 2h / 1d", "daily 21:30", "weekly mon 07:00",
  "weekdays 08:00".
- Delivery: telegram (the user's phone), vault (Routines/<name>.md log), or both. Monitor-style routines answer
  NOTHING_NEW when there's nothing worth saying, and then stay quiet. Results are checked by a cheap verify
  step before they are pushed to the phone.
- Pause / resume / remove / run now; quota-aware (skips when Waggle's share of the Claude budget is used up).
"""
import datetime as dt
import json
import re
import time
import uuid

from . import agents, notify, quota, vault
from .config import Settings, data_path

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
QUIET = "NOTHING_NEW"
DEFAULTS = [
    {"name": "nightly-project-review", "schedule": "daily 21:30", "agent": "monitor", "deliver": "both",
     "task": "Review the user's active projects: check each repo under C:\\Users\\hiheo\\Claude for uncommitted or "
             "unpushed work (git status / git log origin..HEAD), projects whose notes have no next action, and "
             "anything stale for 7+ days. Update project notes with project_update when you find something real. "
             "Report the 1-3 things that matter tomorrow."},
    {"name": "weekly-review", "schedule": "weekly sun 18:00", "agent": "planner", "deliver": "both",
     "task": "Write the user's weekly review: what got done this week (recall + project logs), what's open, and the "
             "top 3 priorities for next week with dates. Save it to the vault as Journal/Weekly Review <date>.md. "
             "Then give a 4-sentence summary."},
]


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
    """Friendly schedule -> 5-field cron, or ('every', seconds)."""
    s = (schedule or "").strip().lower()
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

def add(name, schedule, task, agent="general", deliver="telegram", enabled=True):
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:50] or "routine"
    nxt = next_run(schedule)  # validates
    if agent not in agents.AGENTS:
        agent = "general"
    data = _load()
    rid = next((k for k, v in data.items() if v["name"] == name), uuid.uuid4().hex[:8])
    data[rid] = {"name": name, "schedule": schedule, "task": task, "agent": agent, "deliver": deliver,
                 "enabled": enabled, "created": data.get(rid, {}).get("created", time.time()), "next_run": nxt,
                 "last_run": data.get(rid, {}).get("last_run"), "last_status": data.get(rid, {}).get("last_status"),
                 "runs": data.get(rid, {}).get("runs", 0)}
    _save(data)
    return rid


def ensure_defaults():
    data = _load()
    have = {v["name"] for v in data.values()}
    for d in DEFAULTS:
        if d["name"] not in have:
            add(d["name"], d["schedule"], d["task"], d["agent"], d["deliver"])


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

def run_one(rid, settings=None, run_fn=None, verify_fn=None, now=None):
    """Run one routine now; returns a status string."""
    s = settings or Settings.load()
    data = _load()
    r = data[rid]
    now = now or time.time()
    model = agents.model_for(r["agent"], s)
    ok, why = quota.can_escalate(model, s)
    if not ok:
        data[rid]["next_run"] = now + 3600  # try again in an hour
        data[rid]["last_status"] = f"skipped: {why}"
        _save(data)
        return "skipped"
    data[rid].update(running_since=now)
    _save(data)
    task = (f"{r['task']}\n\n(This is the scheduled routine '{r['name']}'. If nothing needs the user's attention, "
            f"reply exactly {QUIET}.)")
    res = (run_fn or agents.run)(r["agent"], task, session=f"routine-{rid}", settings=s)
    text = (res.text or "").strip()
    status = "ok" if res.ok else f"error: {res.error[:120]}"
    quiet = text.strip().upper().startswith(QUIET)
    if res.ok and not quiet and text:
        if r["deliver"] in ("vault", "both"):
            vault.append(f"Routines/{r['name']}", f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n\n{text}\n")
        if r["deliver"] in ("telegram", "both"):
            complete, missing = (verify_fn or agents.verify)(r["task"], text, settings=s)
            note = "" if complete else f"\n\n(Waggle's check: may be incomplete: {missing[:200]})"
            notify.send(f"🔁 {r['name']}\n\n{text[:3200]}{note}")
    elif not res.ok and r["deliver"] in ("telegram", "both"):
        notify.send(f"🔁 Routine '{r['name']}' failed: {res.error[:300]}")
    data = _load()
    if rid in data:
        data[rid].pop("running_since", None)
        data[rid].update(last_run=now, last_status="quiet" if quiet else status,
                         runs=data[rid].get("runs", 0) + 1, next_run=next_run(r["schedule"], after=now))
        _save(data)
    return "quiet" if quiet else status


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
                  if r.get("enabled") and not r.get("running_since") and r.get("next_run", 0) <= now))
    if not due:
        return None
    return due[0][1], run_one(due[0][1], settings, run_fn, verify_fn, now)


def main():
    ensure_defaults()
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
