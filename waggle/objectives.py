"""Objectives: give Waggle a goal and it works toward it on its own, surviving restarts and reboots.

    objective_create("Find and compare 3 summer AI internships I can apply to, with deadlines")

Lifecycle: planning -> active -> (waiting_approval | blocked | paused) -> done / failed / cancelled
1. plan()     Sonnet decomposes the goal into 2-8 steps: each has an agent (coder, researcher, robotics, planner,
              general...), what "done" means, and which earlier steps it depends on.
2. tick()     a worker runs the next runnable step (dependencies done) with that specialist agent, giving it the goal,
              the plan and the results of the steps it depends on. Every step result is checkpointed to SQLite.
3. verify     a cheap Haiku check compares the result with the step's done-when; a miss is retried once with the
              gap spelled out, then the objective is marked blocked and the user is told why.
4. approvals  a step that hits an approval pauses the objective (waiting_approval); "yes" grants it and resumes.
5. finish     Haiku writes a completion report -> vault note Objectives/<title>.md, project log, phone message.
State lives in ~/.waggle/objectives.db; the 5-minute cron tick (and gateway start) resumes anything in flight,
so a crash, closed window or reboot only costs the step that was running (it is re-run).
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid

from . import agents, claude_cli, events, notify, projects, quota, vault
from .config import Settings, data_path

ACTIVE = ("planning", "active")
OPEN = ("planning", "active", "waiting_approval", "blocked", "paused")
MAX_STEPS = 8
MAX_ATTEMPTS = 2
STALE_STEP_S = 3600


def _db():
    con = sqlite3.connect(data_path("objectives.db"), timeout=15)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS objectives (id TEXT PRIMARY KEY, title TEXT, goal TEXT, project TEXT, status TEXT,
        created REAL, updated REAL, report TEXT, error TEXT, runs INTEGER DEFAULT 0, max_runs INTEGER DEFAULT 20,
        origin TEXT, worker_pid INTEGER, note_path TEXT);
    CREATE TABLE IF NOT EXISTS steps (objective_id TEXT, idx INTEGER, title TEXT, detail TEXT, agent TEXT,
        depends TEXT, done_when TEXT, status TEXT, attempts INTEGER DEFAULT 0, result TEXT, verify TEXT,
        started REAL, finished REAL, PRIMARY KEY (objective_id, idx));
    CREATE TABLE IF NOT EXISTS log (objective_id TEXT, time REAL, kind TEXT, text TEXT);
    """)
    return con


def _log(con, oid, kind, text):
    con.execute("INSERT INTO log VALUES (?,?,?,?)", (oid, time.time(), kind, str(text)[:2000]))


def _set(con, oid, **fields):
    fields["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE objectives SET {cols} WHERE id=?", (*fields.values(), oid))


def _set_step(con, oid, idx, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE steps SET {cols} WHERE objective_id=? AND idx=?", (*fields.values(), oid, idx))


# ---- create + plan -------------------------------------------------------------------------------

PLAN_SYSTEM = """You plan work for a personal AI agent that will execute it step by step with real tools (files, shell,
web research, browser, the user's email/calendar, their notes). Break the goal into 2-8 concrete steps. Each step
is done by one specialist agent: general, coder, researcher, robotics, planner, tutor, monitor. For each step give a
short title, precise instructions, what "done" looks like (checkable), and which earlier steps (by number, from 1)
it needs. Prefer fewer, bigger steps; do not add steps for asking the user things you can find out. The last step
should produce the deliverable the user asked for (a vault note, file, or message). Reply JSON only."""
PLAN_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string", "description": "3-7 word title"},
    "steps": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "detail": {"type": "string"}, "agent": {"type": "string"},
        "done_when": {"type": "string"}, "depends_on": {"type": "array", "items": {"type": "integer"}}},
        "required": ["title", "detail", "agent", "done_when", "depends_on"]}}},
    "required": ["title", "steps"]}


def create(goal, project=None, title=None, origin="user", plan_now=True, asker=None, settings=None):
    """New objective; plans it right away (one Sonnet call). Returns its id."""
    oid = uuid.uuid4().hex[:8]
    con = _db()
    con.execute("INSERT INTO objectives (id, title, goal, project, status, created, updated, origin) "
                "VALUES (?,?,?,?,?,?,?,?)", (oid, (title or goal)[:80], goal, project, "planning", time.time(),
                                             time.time(), origin))
    _log(con, oid, "created", goal)
    con.commit()
    con.close()
    events.log("objective", id=oid, status="planning", title=(title or goal)[:80])
    if plan_now:
        plan(oid, asker=asker, settings=settings)
    return oid


def plan(oid, asker=None, settings=None, context=""):
    s = settings or Settings.load()
    con = _db()
    obj = con.execute("SELECT * FROM objectives WHERE id=?", (oid,)).fetchone()
    ok, why = quota.can_escalate(s.deep_model, s)
    if not ok:
        _set(con, oid, status="planning", error=f"waiting for budget: {why}")
        con.commit()
        con.close()
        return False
    try:
        res = (asker or claude_cli.ask)(f"{context}\n\nGoal: {obj['goal']}" + (f"\nProject: {obj['project']}"
                                                                               if obj["project"] else ""),
                                        model=s.deep_model, system=PLAN_SYSTEM, schema=PLAN_SCHEMA, purpose="plan",
                                        effort="medium")
        d = res.get("json") or {}
        steps = (d.get("steps") or [])[:MAX_STEPS]
        if not steps:
            raise ValueError("planner returned no steps")
    except Exception as e:
        _set(con, oid, status="failed", error=f"planning failed: {e}")
        _log(con, oid, "error", f"planning failed: {e}")
        con.commit()
        con.close()
        return False
    con.execute("DELETE FROM steps WHERE objective_id=?", (oid,))
    for i, st in enumerate(steps, 1):
        deps = sorted({int(x) for x in st.get("depends_on") or [] if 0 < int(x) < i})
        agent = st.get("agent") if st.get("agent") in agents.AGENTS else "general"
        con.execute("INSERT INTO steps (objective_id, idx, title, detail, agent, depends, done_when, status) "
                    "VALUES (?,?,?,?,?,?,?,?)", (oid, i, st["title"][:120], st["detail"][:3000], agent,
                                                 json.dumps(deps), st.get("done_when", "")[:600], "pending"))
    title = (d.get("title") or obj["title"])[:80]
    _set(con, oid, status="active", title=title, error=None)
    _log(con, oid, "planned", f"{len(steps)} steps")
    con.commit()
    con.close()
    _write_note(oid)
    return True


# ---- running -------------------------------------------------------------------------------------

def runnable(con, oid):
    rows = con.execute("SELECT * FROM steps WHERE objective_id=? ORDER BY idx", (oid,)).fetchall()
    done = {r["idx"] for r in rows if r["status"] in ("done", "skipped")}
    return [r for r in rows if r["status"] == "pending" and set(json.loads(r["depends"] or "[]")) <= done]


def _step_prompt(con, obj, step):
    rows = con.execute("SELECT idx, title, status, result FROM steps WHERE objective_id=? ORDER BY idx",
                       (obj["id"],)).fetchall()
    deps = set(json.loads(step["depends"] or "[]"))
    plan_lines = [f"{r['idx']}. {r['title']} [{r['status']}]" for r in rows]
    dep_results = [f"--- Result of step {r['idx']} ({r['title']}):\n{(r['result'] or '')[:3000]}" for r in rows
                   if r["idx"] in deps]
    retry = ""
    if step["attempts"] and step["verify"]:
        retry = f"\nA previous attempt was judged incomplete: {step['verify']}\nFix exactly that this time.\n"
    return (f"You are working on step {step['idx']} of a longer objective for the user.\n\nOBJECTIVE: {obj['goal']}\n"
            + (f"PROJECT: {obj['project']}\n" if obj["project"] else "")
            + "\nPLAN:\n" + "\n".join(plan_lines)
            + ("\n\nRESULTS YOU BUILD ON:\n" + "\n".join(dep_results) if dep_results else "")
            + f"\n\nYOUR STEP: {step['title']}\n{step['detail']}\nDONE WHEN: {step['done_when']}\n{retry}"
            + "\nDo this step only (later steps are handled separately). End with a concise report of what you did "
              "and the concrete results (facts, file paths, links) the next steps need.")


def run_step(oid, idx=None, settings=None, run_fn=None, verify_fn=None):
    """Run one runnable step (or step idx). Returns the step's new status, or None if nothing to run."""
    s = settings or Settings.load()
    con = _db()
    obj = con.execute("SELECT * FROM objectives WHERE id=?", (oid,)).fetchone()
    if not obj or obj["status"] != "active":
        con.close()
        return None
    cands = runnable(con, oid)
    step = next((r for r in cands if idx is None or r["idx"] == idx), None)
    if step is None:
        con.close()
        return None
    if obj["runs"] >= obj["max_runs"]:
        _set(con, oid, status="blocked", error=f"used its {obj['max_runs']} step runs")
        con.commit()
        con.close()
        _tell(oid, f"Objective '{obj['title']}' stopped: it used all {obj['max_runs']} step runs. Say 'resume "
                   f"objective {oid}' to give it more.", urgent=False)
        return "blocked"
    model = agents.model_for(step["agent"], s)
    ok, why = quota.can_escalate(model, s)
    if not ok:
        _log(con, oid, "waiting", f"budget: {why}")
        con.commit()
        con.close()
        return "waiting_budget"
    prompt = _step_prompt(con, obj, step)
    _set_step(con, oid, step["idx"], status="running", started=time.time(), attempts=step["attempts"] + 1)
    _set(con, oid, runs=obj["runs"] + 1)
    _log(con, oid, "step_start", f"{step['idx']}. {step['title']} ({step['agent']})")
    con.commit()
    con.close()

    res = (run_fn or agents.run)(step["agent"], prompt, session=f"objective-{oid}", settings=s)
    con = _db()
    text = (res.text or "").strip()
    if res.approvals:
        _set_step(con, oid, step["idx"], status="pending", result=text[:6000])
        _set(con, oid, status="waiting_approval")
        _log(con, oid, "approval", "; ".join(a["reason"] for a in res.approvals))
        con.commit()
        con.close()
        _tell(oid, f"Objective '{obj['title']}', step {step['idx']} ({step['title']}) needs your OK: "
                   + "; ".join(a["reason"] for a in res.approvals[:3]) + ". Reply \"yes\" to allow it.", urgent=False)
        return "waiting_approval"
    if not res.ok or not text:
        status = _fail_or_retry(con, oid, step, f"agent error: {res.error or 'no result'}")
        con.commit()
        con.close()
        return status
    complete, missing = (verify_fn or agents.verify)(f"{step['title']}: {step['detail']}\nDone when: "
                                                     f"{step['done_when']}", text, settings=s)
    if complete:
        _set_step(con, oid, step["idx"], status="done", result=text[:8000], finished=time.time(), verify="")
        _log(con, oid, "step_done", f"{step['idx']}. {step['title']}")
        status = "done"
    else:
        _set_step(con, oid, step["idx"], result=text[:8000], verify=missing[:600])
        status = _fail_or_retry(con, oid, step, f"incomplete: {missing}")
    con.commit()
    remaining = con.execute("SELECT COUNT(*) FROM steps WHERE objective_id=? AND status NOT IN ('done','skipped')",
                            (oid,)).fetchone()[0]
    con.close()
    if status == "done" and remaining == 0:
        finish(oid, settings=s)
    _write_note(oid)
    return status


def _fail_or_retry(con, oid, step, why):
    attempts = step["attempts"] + 1
    if attempts < MAX_ATTEMPTS:
        _set_step(con, oid, step["idx"], status="pending", verify=why[:600])
        _log(con, oid, "retry", f"step {step['idx']}: {why}")
        return "retry"
    _set_step(con, oid, step["idx"], status="failed", verify=why[:600], finished=time.time())
    _set(con, oid, status="blocked", error=f"step {step['idx']} failed: {why}"[:500])
    _log(con, oid, "blocked", f"step {step['idx']}: {why}")
    obj = con.execute("SELECT title FROM objectives WHERE id=?", (oid,)).fetchone()
    _tell(oid, f"Objective '{obj['title']}' is stuck on step {step['idx']} ({step['title']}): {why[:200]}. "
               f"Tell me how to proceed, or say 'retry objective {oid}'.", urgent=False)
    return "blocked"


REPORT_SYSTEM = """Write the completion report of an objective an AI agent just finished for the user: 3-6 sentences
leading with the outcome, then the key results with any file paths or links, then anything the user should do next.
Use only the step results given."""


def finish(oid, settings=None, asker=None):
    s = settings or Settings.load()
    con = _db()
    obj = con.execute("SELECT * FROM objectives WHERE id=?", (oid,)).fetchone()
    rows = con.execute("SELECT idx, title, result FROM steps WHERE objective_id=? ORDER BY idx", (oid,)).fetchall()
    body = "\n\n".join(f"Step {r['idx']} {r['title']}:\n{(r['result'] or '')[:2500]}" for r in rows)
    try:
        report = (asker or claude_cli.ask)(f"Goal: {obj['goal']}\n\n{body}", model=s.router_model,
                                           system=REPORT_SYSTEM, purpose="report", effort="low")["text"].strip()
    except Exception:
        report = (rows[-1]["result"] or "Done.")[:1500] if rows else "Done."
    _set(con, oid, status="done", report=report)
    _log(con, oid, "done", report[:500])
    con.commit()
    con.close()
    if obj["project"]:
        try:
            projects.add_log(obj["project"], f"Objective **{obj['title']}** done: {report[:200]}")
        except Exception:
            pass
    note = _write_note(oid)
    events.log("objective", id=oid, status="done", title=obj["title"])
    _tell(oid, f"✅ Objective done: {obj['title']}\n\n{report[:2500]}" + (f"\n\n(Full notes: {note})" if note else ""),
          urgent=False)
    return report


def _tell(oid, text, urgent=False):
    try:
        notify.send(text, urgent=urgent)
    except Exception:
        pass


# ---- worker + recovery ---------------------------------------------------------------------------

def _alive(pid):
    from .jobs import _alive as alive
    return alive(pid)


def recover(con=None, now=None):
    """Steps left 'running' by a dead worker go back to pending (they re-run; attempts already counted)."""
    now = now or time.time()
    own = con is None
    con = con or _db()
    n = 0
    for obj in con.execute("SELECT id, worker_pid FROM objectives WHERE status IN ('active','planning')").fetchall():
        if obj["worker_pid"] and _alive(obj["worker_pid"]):
            continue
        for st in con.execute("SELECT idx, started FROM steps WHERE objective_id=? AND status='running'",
                              (obj["id"],)).fetchall():
            if now - (st["started"] or 0) > 120:
                _set_step(con, obj["id"], st["idx"], status="pending")
                _log(con, obj["id"], "recovered", f"step {st['idx']} re-queued after an interruption")
                n += 1
    if own:
        con.commit()
        con.close()
    return n


def tick(max_steps=1, settings=None, run_fn=None, verify_fn=None, asker=None):
    """Advance in-flight objectives (cron, every 5 minutes; also kicked right after creation)."""
    con = _db()
    recover(con)
    con.commit()
    objs = con.execute("SELECT * FROM objectives WHERE status IN ('planning','active') ORDER BY updated").fetchall()
    con.close()
    done = []
    for obj in objs:
        if len(done) >= max_steps:
            break
        if obj["worker_pid"] and obj["worker_pid"] != os.getpid() and _alive(obj["worker_pid"]):
            continue  # another worker has it
        if obj["status"] == "planning":
            plan(obj["id"], asker=asker, settings=settings)
            continue
        con = _db()
        _set(con, obj["id"], worker_pid=os.getpid())
        con.commit()
        con.close()
        try:
            st = run_step(obj["id"], settings=settings, run_fn=run_fn, verify_fn=verify_fn)
        finally:
            con = _db()
            _set(con, obj["id"], worker_pid=None)
            con.commit()
            con.close()
        if st:
            done.append((obj["id"], st))
    return done


def supervise(kick_fn=None):
    """Cron (every 5 min) + gateway start: re-queue interrupted steps, then make sure every in-flight objective has
    a live worker. Long steps never run inside the cron script itself."""
    con = _db()
    recover(con)
    con.commit()
    rows = con.execute("SELECT id, worker_pid FROM objectives WHERE status IN ('planning','active')").fetchall()
    con.close()
    started = []
    for r in rows:
        if r["worker_pid"] and _alive(r["worker_pid"]):
            continue
        (kick_fn or kick)(r["id"])
        started.append(r["id"])
    return started


def kick(oid):
    """Start working on an objective now, in a detached process (the chat isn't blocked)."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=repo + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.Popen([sys.executable, "-m", "waggle.objectives", "work", oid], creationflags=flags, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def work(oid, max_steps=MAX_STEPS * MAX_ATTEMPTS, settings=None, run_fn=None, verify_fn=None):
    """Keep running steps of one objective until it finishes, blocks, waits, or runs out of budget."""
    con = _db()
    obj = con.execute("SELECT worker_pid FROM objectives WHERE id=?", (oid,)).fetchone()
    if obj and obj["worker_pid"] and obj["worker_pid"] != os.getpid() and _alive(obj["worker_pid"]):
        con.close()
        return "busy"
    _set(con, oid, worker_pid=os.getpid())
    con.commit()
    con.close()
    last = None
    try:
        for _ in range(max_steps):
            con = _db()
            status = con.execute("SELECT status FROM objectives WHERE id=?", (oid,)).fetchone()["status"]
            con.close()
            if status == "planning":
                if not plan(oid, settings=settings):
                    break
                continue
            st = run_step(oid, settings=settings, run_fn=run_fn, verify_fn=verify_fn)
            if st is None:  # finished, cancelled or nothing runnable
                break
            last = st
            if st in ("blocked", "waiting_approval", "waiting_budget"):
                break
    finally:
        con = _db()
        _set(con, oid, worker_pid=None)
        con.commit()
        con.close()
    return last


# ---- control -------------------------------------------------------------------------------------

def find(key):
    con = _db()
    row = con.execute("SELECT id FROM objectives WHERE id=?", (key,)).fetchone()
    if not row:
        row = con.execute("SELECT id FROM objectives WHERE title LIKE ? AND status IN (%s) ORDER BY updated DESC"
                          % ",".join("?" * len(OPEN)), (f"%{key}%", *OPEN)).fetchone()
    con.close()
    return row["id"] if row else None


def set_status(key, status, extra_runs=0):
    oid = find(key)
    if not oid:
        return None
    con = _db()
    if status == "active":
        con.execute("UPDATE steps SET status='pending', attempts=0 WHERE objective_id=? AND status='failed'", (oid,))
        if extra_runs:
            con.execute("UPDATE objectives SET max_runs=max_runs+? WHERE id=?", (extra_runs, oid))
    _set(con, oid, status=status, error=None)
    _log(con, oid, status, "by user")
    con.commit()
    con.close()
    return oid


def resume_after_approval(session):
    """'yes' was granted for an objective's session: make it active again and keep going."""
    if not session.startswith("objective-"):
        return None
    oid = session.split("-", 1)[1]
    con = _db()
    row = con.execute("SELECT status FROM objectives WHERE id=?", (oid,)).fetchone()
    if row and row["status"] == "waiting_approval":
        _set(con, oid, status="active")
        _log(con, oid, "approved", "user said yes")
        con.commit()
        con.close()
        kick(oid)
        return oid
    con.close()
    return None


def status(key=None):
    con = _db()
    if key:
        oid = find(key)
        if not oid:
            con.close()
            return None
        obj = dict(con.execute("SELECT * FROM objectives WHERE id=?", (oid,)).fetchone())
        obj["steps"] = [dict(r) for r in con.execute("SELECT idx, title, agent, status, attempts, verify, "
                                                     "substr(result,1,600) AS result FROM steps WHERE objective_id=? "
                                                     "ORDER BY idx", (oid,))]
        obj["log"] = [dict(r) for r in con.execute("SELECT time, kind, text FROM log WHERE objective_id=? "
                                                   "ORDER BY time DESC LIMIT 12", (oid,))]
        con.close()
        return obj
    rows = [dict(r) for r in con.execute(
        "SELECT o.id, o.title, o.status, o.updated, o.project, (SELECT COUNT(*) FROM steps s WHERE s.objective_id=o.id"
        " AND s.status='done') AS done, (SELECT COUNT(*) FROM steps s WHERE s.objective_id=o.id) AS total "
        "FROM objectives o ORDER BY (o.status IN ('done','failed','cancelled')), o.updated DESC LIMIT 20")]
    con.close()
    return rows


def _write_note(oid):
    """Human-readable mirror in the vault: Objectives/<title>.md (status, plan, results, report)."""
    try:
        obj = status(oid)
        if not obj:
            return None
        icon = {"done": "✅", "running": "⏳", "failed": "❌", "pending": "⬜", "skipped": "➖"}
        lines = ["---", "type: objective", f"status: {obj['status']}", f"id: {oid}", "---",
                 f"# {obj['title']}", "", f"**Goal:** {obj['goal']}", ""]
        if obj.get("project"):
            lines.append(f"**Project:** [[{obj['project']}]]\n")
        if obj.get("report"):
            lines += ["## Report", "", obj["report"], ""]
        if obj.get("error"):
            lines += [f"> [!warning] {obj['error']}", ""]
        lines += ["## Plan", ""]
        for st in obj["steps"]:
            lines.append(f"- {icon.get(st['status'], '•')} **{st['idx']}. {st['title']}** · {st['agent']}"
                         + (f" · _{st['verify']}_" if st.get("verify") else ""))
        lines += ["", "## Step results", ""]
        for st in obj["steps"]:
            if st.get("result"):
                lines += [f"### {st['idx']}. {st['title']}", "", st["result"], ""]
        rel = f"Objectives/{vault.slug(obj['title'])}"
        vault.write(rel, "\n".join(lines))
        con = _db()
        _set(con, oid, note_path=rel)
        con.commit()
        con.close()
        return rel
    except Exception:
        return None


# ---- MCP -----------------------------------------------------------------------------------------

def _t_create(args):
    oid = create(args["goal"], project=args.get("project"), title=args.get("title"), origin="agent")
    st = status(oid)
    if st and st["status"] == "active":
        kick(oid)
    return json.dumps({"id": oid, "status": st["status"] if st else "?", "error": (st or {}).get("error"),
                       "steps": [f"{x['idx']}. {x['title']} ({x['agent']})" for x in (st or {}).get("steps", [])]})


def _t_status(args):
    return json.dumps(status(args.get("id") or None), default=str)


MCP_TOOLS = [
    ({"name": "objective_create", "description": "Start a persistent objective: Waggle plans it into steps and works "
      "through them in the background with specialist agents (survives restarts), verifying each step and reporting "
      "on the user's phone when done. Use for goals that need several steps or more than ~10 minutes (research + "
      "write-up, multi-part tasks, setting something up end to end). Not for quick tasks or code builds of one app "
      "(use start_build for those).",
      "parameters": {"type": "object", "properties": {"goal": {"type": "string", "description": "the full goal, with "
                     "constraints and what the deliverable is"}, "project": {"type": "string"},
                     "title": {"type": "string"}}, "required": ["goal"]}}, _t_create),
    ({"name": "objective_status", "description": "Status of one objective (by id or title words) with its steps, or "
      "a list of recent objectives when no id is given.",
      "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": []}}, _t_status),
    ({"name": "objective_control", "description": "Pause, resume (also retries failed steps and can grant extra step "
      "runs), or cancel an objective.",
      "parameters": {"type": "object", "properties": {"id": {"type": "string"},
                     "action": {"type": "string", "enum": ["pause", "resume", "cancel"]},
                     "extra_runs": {"type": "integer"}}, "required": ["id", "action"]}},
     lambda a: json.dumps({"id": set_status(a["id"], {"pause": "paused", "resume": "active",
                                                       "cancel": "cancelled"}[a["action"]],
                                            int(a.get("extra_runs") or 0)),
                           "kicked": a["action"] == "resume" and bool(find(a["id"])) and (kick(find(a["id"])) or True)})),
]


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "work":
        work(sys.argv[2])
    else:
        tick()


if __name__ == "__main__":
    main()
