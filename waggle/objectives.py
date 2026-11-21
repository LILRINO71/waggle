"""Objectives: give Waggle a goal and it works toward it on its own, surviving restarts and reboots.

    objective_create("Find and compare 3 summer AI internships I can apply to, with deadlines")

Lifecycle: planning -> active -> (waiting_approval | blocked | paused) -> done / failed / cancelled
1. plan()     Sonnet decomposes the goal into 2-8 steps: each has an agent (coder, researcher, robotics, planner,
              general...), what "done" means, and which earlier steps it depends on.
2. tick()     a worker runs the next runnable step (dependencies done) with that specialist agent, giving it the goal,
              the plan and the results of the steps it depends on. Every step result is checkpointed to SQLite.
3. verify     a cheap Haiku check compares the result with the step's done-when; a miss is retried once with the
              gap spelled out, then the objective is marked blocked and the user is told why.
4. approvals  a step that hits an approval pauses the objective (waiting_approval) and the user gets one code per
              action; "approve <code>" grants exactly that call once and resumes. Expired or denied -> blocked.
5. budget     every step reserves room in the background budget first (budget.py); when there is none the
              objective is parked (deferred_until) and the supervisor picks it up again after the reset.
6. finish     Haiku (or the local model when Claude can't) writes the report -> vault note Objectives/<title>.md,
              project log, phone message. Exactly once.
State lives in ~/.waggle/objectives.db; the 5-minute cron tick (and gateway start) resumes anything in flight,
so a crash, closed window or reboot only costs the step that was running (it is re-run, told it was interrupted).
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import agents, budget, checklist, claude_cli, events, notify, projects, quota, vault
from .config import Settings, data_path

ACTIVE = ("planning", "active")
# Objectives the autonomous work loop starts (waggle.autowork): they never message the owner themselves (results
# reach the evening note through the loop), their report is written without a model call, and a step whose proof
# fails ends them as 'unproven' (a final state nothing resumes) instead of being retried by the supervisor.
QUIET_ORIGINS = ("autowork", "shadow")
OPEN = ("planning", "active", "waiting_approval", "blocked", "paused")
MAX_STEPS = 8
MAX_ATTEMPTS = 2
STALE_STEP_S = 3600
LEASE_S = 20 * 60  # worker lease, renewed before each step; an agent run is capped at 15 min
MAX_INTERRUPTS = 3  # a step that dies mid-run this often blocks instead of looping
VERIFY_RETRY_S = 15 * 60
MIGRATIONS = {"objectives": (("lease_until", "REAL"), ("deferred_until", "REAL"), ("defer_reason", "TEXT"),
                             ("approval_ids", "TEXT")),
              "steps": (("interrupts", "INTEGER DEFAULT 0"), ("approved", "TEXT"), ("checklist", "TEXT"),
                        ("proof", "TEXT"))}


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
    for table, cols in MIGRATIONS.items():  # databases from before these columns existed
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                except sqlite3.OperationalError:
                    pass  # another process added it first
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
should produce the deliverable the user asked for (a vault note, file, or message). For each step also give 2-5
requirements: concrete things that must be true when it is done and that can be checked from the files it leaves behind
(give the file's path when one must exist) or, with content=true, from its final output. Reply JSON only."""
PLAN_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string", "description": "3-7 word title"},
    "steps": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "detail": {"type": "string"}, "agent": {"type": "string"},
        "done_when": {"type": "string"}, "depends_on": {"type": "array", "items": {"type": "integer"}},
        "requirements": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "boolean"}},
            "required": ["text"]}}},
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


def create_planned(goal, steps, project=None, title=None, origin="autowork"):
    """New objective with its steps given (no planning call): each step {title, detail, agent, done_when,
    depends_on}. It starts active. Returns its id."""
    if not steps:
        raise ValueError("an objective needs at least one step")
    oid = uuid.uuid4().hex[:8]
    now = time.time()
    con = _db()
    con.execute("INSERT INTO objectives (id, title, goal, project, status, created, updated, origin) "
                "VALUES (?,?,?,?,?,?,?,?)", (oid, (title or goal)[:80], goal, project, "active", now, now, origin))
    for i, st in enumerate(steps[:MAX_STEPS], 1):
        deps = sorted({int(x) for x in st.get("depends_on") or [] if 0 < int(x) < i})
        agent = st.get("agent") if st.get("agent") in agents.AGENTS else "general"
        con.execute("INSERT INTO steps (objective_id, idx, title, detail, agent, depends, done_when, status, "
                    "checklist) VALUES (?,?,?,?,?,?,?,?,?)",
                    (oid, i, str(st["title"])[:120], str(st.get("detail", ""))[:3000], agent, json.dumps(deps),
                     str(st.get("done_when", ""))[:600], "pending",
                     json.dumps(checklist.normalize(st.get("requirements")))))
    _log(con, oid, "created", f"{goal} ({len(steps)} given steps, origin {origin})")
    con.commit()
    con.close()
    events.log("objective", id=oid, status="active", title=(title or goal)[:80])
    return oid


def conclude_unproven(oid, why):
    """End a quiet-origin objective whose work could not be proven (or did not run): status 'unproven', which
    the supervisor, the work watcher and resume never pick up again. Returns True when it changed."""
    con = _db()
    n = con.execute("UPDATE objectives SET status='unproven', error=?, updated=?, worker_pid=NULL, "
                    "lease_until=NULL, deferred_until=NULL WHERE id=? AND origin IN (%s) AND status NOT IN "
                    "('done','cancelled','unproven')" % ",".join("?" * len(QUIET_ORIGINS)),
                    (str(why)[:500], time.time(), oid, *QUIET_ORIGINS)).rowcount
    if n:
        _log(con, oid, "unproven", why)
    con.commit()
    con.close()
    return n == 1


def _origin(oid):
    try:
        con = _db()
        row = con.execute("SELECT origin FROM objectives WHERE id=?", (oid,)).fetchone()
        con.close()
        return (row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


def plan(oid, asker=None, settings=None, context=""):
    s = settings or Settings.load()
    con = _db()
    obj = con.execute("SELECT * FROM objectives WHERE id=?", (oid,)).fetchone()
    ok, why = quota.can_escalate(s.deep_model, s)
    if not ok:
        _set(con, oid, status="planning", error=f"waiting for budget: {why}",
             deferred_until=time.time() + (600 if budget.is_background() else 0))
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
    except claude_cli.ClaudeError as e:
        if e.kind in ("budget", "limit", "timeout"):  # not the plan's fault: try again later
            _set(con, oid, status="planning", error=f"waiting for budget: {e}", deferred_until=time.time() + 1800)
            _log(con, oid, "waiting", f"planning: {e}")
            con.commit()
            con.close()
            return False
        _set(con, oid, status="failed", error=f"planning failed: {e}")
        _log(con, oid, "error", f"planning failed: {e}")
        con.commit()
        con.close()
        return False
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
        con.execute("INSERT INTO steps (objective_id, idx, title, detail, agent, depends, done_when, status, "
                    "checklist) VALUES (?,?,?,?,?,?,?,?,?)",
                    (oid, i, st["title"][:120], st["detail"][:3000], agent, json.dumps(deps),
                     st.get("done_when", "")[:600], "pending", json.dumps(checklist.normalize(st.get("requirements")))))
    title = (d.get("title") or obj["title"])[:80]
    _set(con, oid, status="active", title=title, error=None, deferred_until=None)
    _log(con, oid, "planned", f"{len(steps)} steps")
    con.commit()
    con.close()
    _write_note(oid)
    return True


# ---- running -------------------------------------------------------------------------------------
#
# Step states: pending -> running -> verifying -> done | pending (retry) | failed
#   running    claimed by exactly one worker (conditional UPDATE); a dead worker's step goes back to pending and
#              its next run is told it was interrupted (so it checks what already exists instead of redoing it)
#   verifying  the work is finished and its result saved; only the check is left. A crash here never re-runs the
#              work, and a check that can't run (budget, Claude down) waits instead of passing the step unchecked.
# Objective worker: one at a time, held by a lease (worker_pid + lease_until) renewed before every step, so a
# duplicate cron run or a reused PID after a reboot can't start a second worker.

def runnable(con, oid):
    rows = con.execute("SELECT * FROM steps WHERE objective_id=? ORDER BY idx", (oid,)).fetchall()
    done = {r["idx"] for r in rows if r["status"] in ("done", "skipped")}
    return [r for r in rows if r["status"] == "pending" and set(json.loads(r["depends"] or "[]")) <= done]


INTERRUPTED_NOTE = ("\nAn earlier run of this step was interrupted before it finished. Some of its work may already "
                    "exist (files, notes, drafts). Check the current state first and continue from there. Never repeat "
                    "an action that can't be undone (sending, posting, deleting, buying) unless you have confirmed it "
                    "did not already happen.\n")


def step_requirements(step):
    """The step's checklist: [{"id", "text", "path"?, "content"?}] (empty for steps planned without one)."""
    try:
        reqs = json.loads(step["checklist"] or "[]")
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    return [r for r in reqs if isinstance(r, dict) and r.get("text")]


def _named_files(step, reqs, s):
    """Existing files the step's text or requirements name (vault-relative paths are looked up in the vault)."""
    root = vault.root(s)
    text = f"{step['detail']}\n{step['done_when']}\n" + "\n".join(f"{r['text']} {r.get('path') or ''}" for r in reqs)
    out = []
    for rel in VAULT_PATH.findall(text) + ABS_PATH.findall(text):
        p = Path(rel) if ABS_PATH.fullmatch(rel) else root / rel
        if p.is_file() and str(p) not in out:
            out.append(str(p))
    return out[:6]


def _judge_step(con, oid, step, reqs, s):
    """(complete, missing) for a step with a checklist: files by code, the rest by the evidence-only judge, which
    sees the files the step named and (for [content] requirements) its final output, never its report. (None, why)
    when the judge could not run: the step waits, as it does for any check that could not run."""
    root = vault.root(s)
    for r in reqs:  # a requirement's vault-relative path means that file inside the vault
        if r.get("path") and not ABS_PATH.fullmatch(str(r["path"])):
            r["path"] = str(root / str(r["path"]))
    out = checklist.evaluate(reqs, started=step["started"] or 0, settings=s, files=_named_files(step, reqs, s),
                             deliverable=step["result"], objective=oid, undecided_as_unmet=False)
    _set_step(con, oid, step["idx"], proof=json.dumps(out["checks"]))
    if out["undecided"]:
        return None, out["undecided"]
    return out["proven"], out["missing"]


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
    if step["interrupts"]:
        retry += INTERRUPTED_NOTE
    if step["approved"]:
        retry += (f"\nThe user approved exactly these actions for this step: {step['approved']}. Make those calls "
                  "with the same arguments; anything else that needs approval will be asked again.\n")
    reqs = step_requirements(step)
    if reqs:
        retry += ("\nREQUIREMENTS, checked afterwards against the files you leave behind and your final output, never "
                  "against your own report:\n" + "\n".join(f"{r['id']}. {r['text']}" for r in reqs) + "\n")
    root = vault.root()
    return (f"You are working on step {step['idx']} of a longer objective for the user.\n\nOBJECTIVE: {obj['goal']}\n"
            + (f"PROJECT: {obj['project']}\n" if obj["project"] else "")
            + f"THE USER'S NOTES: the Obsidian vault at {root}. A path like Inbox/x.md, Projects/y.md or Research/z.md "
              f"means that file inside the vault ({root}\\Inbox\\x.md), never your working folder.\n"
            + "\nPLAN:\n" + "\n".join(plan_lines)
            + ("\n\nRESULTS YOU BUILD ON:\n" + "\n".join(dep_results) if dep_results else "")
            + f"\n\nYOUR STEP: {step['title']}\n{step['detail']}\nDONE WHEN: {step['done_when']}\n{retry}"
            + "\nDo this step only (later steps are handled separately). End with a concise report of what you did "
              "and the concrete results (facts, file paths, links) the next steps need.")


def _defer(con, oid, reason, until):
    """Budget or verification has to wait: park the objective (no worker spawns) until `until`."""
    _set(con, oid, deferred_until=until, defer_reason=str(reason)[:300])
    _log(con, oid, "waiting", f"{reason} (retry after {time.strftime('%H:%M', time.localtime(until))})")


def run_step(oid, idx=None, settings=None, run_fn=None, verify_fn=None):
    """Run one runnable step (or step idx). Returns the step's new status, or None if nothing to run. A step that ran
    for an objective that is not autonomy's own leaves a session record (autonomy records itself)."""
    status = _run_step(oid, idx, settings, run_fn, verify_fn)
    if status not in (None, "waiting_budget"):
        try:
            if _origin(oid) != "autowork":
                from . import session_records
                con = _db()
                try:
                    row = con.execute("SELECT title FROM objectives WHERE id=?", (oid,)).fetchone()
                finally:
                    con.close()
                session_records.record("objective", oid, status, title=(row["title"] if row else ""), settings=settings)
        except Exception:  # noqa: BLE001
            pass
    return status


def _run_step(oid, idx=None, settings=None, run_fn=None, verify_fn=None):
    s = settings or Settings.load()
    con = _db()
    obj = con.execute("SELECT * FROM objectives WHERE id=?", (oid,)).fetchone()
    if not obj or obj["status"] != "active":
        con.close()
        return None
    now = time.time()
    if (obj["deferred_until"] or 0) > now:
        con.close()
        return "waiting_budget"
    # Work already done and saved, only its check left: verify it, never redo the work.
    ver = con.execute("SELECT idx FROM steps WHERE objective_id=? AND status='verifying' ORDER BY idx",
                      (oid,)).fetchone()
    if ver and (idx is None or ver["idx"] == idx):
        con.close()
        return _verify_and_record(oid, ver["idx"], s, verify_fn)
    step = next((r for r in runnable(con, oid) if idx is None or r["idx"] == idx), None)
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
    with budget.background("objective", oid, step["idx"]):
        model = budget.choose_model(agents.model_for(step["agent"], s), step["agent"], s, oid, step["idx"])
        ok, why = quota.can_escalate(model, s)
    if not ok:
        _, _, until = budget.check(model, "objective", s)
        _defer(con, oid, why, max(until, now + 60))
        con.commit()
        con.close()
        return "waiting_budget"
    try:
        ticket = budget.reserve(model, "objective", oid, step["idx"], s)
    except budget.Deferred as e:
        _defer(con, oid, e.reason, max(e.until, now + 60))
        con.commit()
        con.close()
        return "waiting_budget"
    claimed = con.execute("UPDATE steps SET status='running', started=?, attempts=attempts+1 WHERE objective_id=? "
                          "AND idx=? AND status='pending'", (now, oid, step["idx"])).rowcount
    if not claimed:  # another worker got there first
        con.commit()
        con.close()
        budget.release(ticket)
        return None
    prompt = _step_prompt(con, obj, step)
    _set(con, oid, runs=obj["runs"] + 1, deferred_until=None, defer_reason=None)
    _log(con, oid, "step_start", f"{step['idx']}. {step['title']} ({step['agent']}, {model})")
    con.commit()
    con.close()

    try:
        with budget.using(ticket, "objective", oid, step["idx"]):
            res = (run_fn or agents.run)(step["agent"], prompt, session=f"objective-{oid}", settings=s, model=model,
                                         purpose="objective")
    finally:
        budget.release(ticket)
    con = _db()
    text = (res.text or "").strip()
    if getattr(res, "deferred", 0):  # the budget guard stopped it before it started: not a real attempt
        _set_step(con, oid, step["idx"], status="pending", attempts=step["attempts"])
        _defer(con, oid, res.error, res.deferred)
        con.commit()
        con.close()
        return "waiting_budget"
    if res.approvals:
        _set_step(con, oid, step["idx"], status="pending", result=text[:6000],
                  attempts=step["attempts"])  # waiting for a yes is not a failed attempt
        _set(con, oid, status="waiting_approval",
             approval_ids=json.dumps({"step": step["idx"], "ids": [a.get("id") for a in res.approvals]}))
        _log(con, oid, "approval", "; ".join(f"[{a.get('code', '?')}] {a['reason']}" for a in res.approvals))
        con.commit()
        con.close()
        _tell(oid, approval_message(obj, step, res.approvals), urgent=False)
        return "waiting_approval"
    if not res.ok or not text:
        status = _fail_or_retry(con, oid, step, f"agent error: {res.error or 'no result'}", step["attempts"] + 1)
        con.commit()
        con.close()
        return status
    # Checkpoint: the work is saved before it is judged.
    _set_step(con, oid, step["idx"], status="verifying", result=text[:8000], finished=time.time())
    _log(con, oid, "step_result", f"{step['idx']}. {step['title']}: result saved, verifying")
    con.commit()
    con.close()
    return _verify_and_record(oid, step["idx"], s, verify_fn)


def approval_message(obj, step, approvals):
    """Exactly which objective, which step and which action, with a code per action and its expiry."""
    from . import policy
    lines = []
    for a in approvals[:5]:
        if a.get("code") and a.get("created"):
            lines.append("• " + policy.describe(a))
        else:
            lines.append(f"• {a.get('summary') or ''} ({a['reason']})".replace("•  (", "• ("))
    codes = [a.get("code") for a in approvals if a.get("code")]
    how = (f'Reply "approve {codes[0]}" to allow it once, or "deny {codes[0]}".' if len(codes) == 1 else
           f'Reply "approve <code>" for each one you allow (e.g. "approve {codes[0]}"), or "deny <code>".'
           if codes else 'Reply "approve" to allow it.')
    return (f"Objective '{obj['title']}' (id {obj['id']}), step {step['idx']} ({step['title']}) needs your OK before "
            f"it can:\n" + "\n".join(lines) + f"\n{how} Nothing else in this objective runs until you answer.")


VAULT_PATH = re.compile(r"\b((?:Inbox|Projects|Research|Objectives|Ideas|People|Journal|Me|Routines|Jobs|Agents)"
                        r"/[^\n\"'`*|\[\]]+?\.md)\b")
ABS_PATH = re.compile(r"\b([A-Za-z]:[\\/][^\n\"'`*|<>?]+?\.(?:md|txt|py|json|csv|html))\b")
CREATES = re.compile(r"\b(write|writes|written|create|created|save|saved|exists?|note at|file at|contains?|produce|"
                     r"produced)\b", re.I)
REMOVES = re.compile(r"\b(delete|deleted|remove|removed|no longer|gone|move|moved|rename|renamed)\b", re.I)


def artifact_facts(step, settings=None):
    """Ground truth for the check: do the files this step names actually exist? Vault-relative paths (Inbox/x.md)
    are looked up in the vault. Returns (facts text, files a creating step promised but are missing)."""
    s = settings or Settings.load()
    text = f"{step['detail']}\n{step['done_when']}"
    root = vault.root(s)
    facts, absent = [], []
    seen = set()
    for rel in VAULT_PATH.findall(text) + ABS_PATH.findall(text):
        if rel in seen:
            continue
        seen.add(rel)
        p = Path(rel) if ABS_PATH.fullmatch(rel) else root / rel
        if p.exists():
            facts.append(f"{p} exists ({p.stat().st_size} bytes)")
        else:
            facts.append(f"{p} does NOT exist")
            if CREATES.search(step["done_when"] or "") and not REMOVES.search(text):
                absent.append(str(p))
    return "; ".join(facts[:6]), absent


def _verify_and_record(oid, idx, s, verify_fn=None):
    con = _db()
    step = con.execute("SELECT * FROM steps WHERE objective_id=? AND idx=?", (oid, idx)).fetchone()
    task = f"{step['title']}: {step['detail']}\nDone when: {step['done_when']}"
    reqs = step_requirements(step)
    facts, absent = artifact_facts(step, s)
    report = (step["result"] or "") + (f"\n\n[Checked by Waggle on disk: {facts}]" if facts else "")
    if absent:  # the step promised a file that isn't there: no need to ask a model
        complete, missing = False, (f"{absent[0]} does not exist. Write it there (inside the vault "
                                    f"{vault.root(s)}), not anywhere else.")
    else:
        with budget.background("objective-verify", oid, idx):
            if verify_fn:
                complete, missing = verify_fn(task, report, settings=s)
            elif reqs and checklist.enabled(s):
                complete, missing = _judge_step(con, oid, step, reqs, s)
            else:
                complete, missing = agents.verify(task, report, settings=s, strict=True)
    if complete is None:  # the check couldn't run: keep the saved result and try the check again later
        _defer(con, oid, f"verification of step {idx} waiting: {missing}", time.time() + VERIFY_RETRY_S)
        con.commit()
        con.close()
        return "waiting_budget"
    if complete:
        _set_step(con, oid, idx, status="done", verify="")
        _log(con, oid, "step_done", f"{idx}. {step['title']}")
        status = "done"
    else:
        _set_step(con, oid, idx, verify=(missing or "")[:600])
        status = _fail_or_retry(con, oid, step, f"incomplete: {missing}", step["attempts"])
    con.commit()
    remaining = con.execute("SELECT COUNT(*) FROM steps WHERE objective_id=? AND status NOT IN ('done','skipped')",
                            (oid,)).fetchone()[0]
    con.close()
    if status == "done" and remaining == 0:
        finish(oid, settings=s)
    _write_note(oid)
    return status


def _fail_or_retry(con, oid, step, why, attempts):
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


def _report(goal, body, s, asker=None):
    """Haiku writes the report; if Claude can't (budget, limit) the local model does; last resort the final result."""
    prompt = f"Goal: {goal}\n\n{body}"
    try:
        with budget.background("objective-report"):
            return (asker or claude_cli.ask)(prompt, model=s.router_model, system=REPORT_SYSTEM, purpose="report",
                                             effort="low")["text"].strip()
    except Exception as e:
        budget.log_decision("local", f"report written locally: {e}"[:200])
    try:
        from . import ollama
        text = ollama.chat([{"role": "system", "content": REPORT_SYSTEM}, {"role": "user", "content": prompt[:12000]}],
                           settings=s, timeout=180).strip()
        budget.record(s.local_model, {}, "report", local=True)
        return text
    except Exception:
        return ""


def finish(oid, settings=None, asker=None):
    """Write the report and mark done. Idempotent: a second call (or a second worker) never re-notifies."""
    s = settings or Settings.load()
    con = _db()
    obj = con.execute("SELECT * FROM objectives WHERE id=?", (oid,)).fetchone()
    if not obj or obj["status"] == "done":
        con.close()
        return obj["report"] if obj else None
    rows = con.execute("SELECT idx, title, result FROM steps WHERE objective_id=? ORDER BY idx", (oid,)).fetchall()
    con.close()
    body = "\n\n".join(f"Step {r['idx']} {r['title']}:\n{(r['result'] or '')[:2500]}" for r in rows)
    if obj["origin"] in QUIET_ORIGINS:  # the loop that started it reports; no model call for prose
        report = "; ".join(f"step {r['idx']} {r['title']}: done" for r in rows)[:1500] or "Done."
    else:
        report = _report(obj["goal"], body, s, asker) or ((rows[-1]["result"] or "Done.")[:1500] if rows else
                                                          "Done.")
    con = _db()
    won = con.execute("UPDATE objectives SET status='done', report=?, updated=?, error=NULL, deferred_until=NULL "
                      "WHERE id=? AND status!='done'", (report, time.time(), oid)).rowcount
    if won:
        _log(con, oid, "done", report[:500])
    con.commit()
    con.close()
    if not won:
        return report
    if obj["project"]:
        try:
            projects.add_log(obj["project"], f"Objective **{obj['title']}** done: {report[:200]}")
        except Exception:
            pass
    note = _write_note(oid)
    events.log("objective", id=oid, status="done", title=obj["title"])
    if _worth_a_message_now(oid, obj, report, s) is False:
        return report  # kept for the evening note; the vault note has the full result
    _tell(oid, f"✅ Objective done: {obj['title']}\n\n{report[:2500]}" + (f"\n\n(Full notes: {note})" if note else ""),
          urgent=False)
    return report


def _worth_a_message_now(oid, obj, report, s):
    """For a finished research objective the owner started: False when a local yes/no says the result is not worth a
    message now (or the model could not answer): it is put into the evening note instead. None when this objective
    is not judged (anything but the owner's own research objective)."""
    if _origin(oid) != "user":
        return None
    con = _db()
    try:
        research = con.execute("SELECT 1 FROM steps WHERE objective_id=? AND agent='researcher' LIMIT 1",
                               (oid,)).fetchone()
    finally:
        con.close()
    if not research:
        return None
    from . import initiative, notifycheck
    nc = notifycheck.decide(obj["goal"], report, settings=s, subject=obj["title"])
    if nc["notify"]:
        return True
    initiative.keep_for_evening(f"objective-done:{oid}", "objective",
                                f"Objective done: {obj['title']}: {(report or '').splitlines()[0] if report else ''}",
                                report or "", time.time())
    return False


def _tell(oid, text, urgent=False):
    if _origin(oid) in QUIET_ORIGINS:
        return  # the autonomous loop reports through the evening note, never by its own message
    try:
        from .origin import objective_source  # developer/test objectives never reach the phone
        notify.send(text, urgent=urgent, source=objective_source(oid), origin="objective")
    except Exception:
        pass


# ---- worker + recovery ---------------------------------------------------------------------------

def _alive(pid):
    from .jobs import _alive as alive
    return alive(pid)


def _held(row, now):
    """Is this objective held by a live worker with an unexpired lease?"""
    pid = row["worker_pid"]
    return bool(pid) and (row["lease_until"] or 0) > now and (pid == os.getpid() or _alive(pid))


def claim(oid, now=None):
    """Become the one worker for this objective (compare-and-swap on worker_pid + lease)."""
    now = now or time.time()
    con = _db()
    try:
        row = con.execute("SELECT worker_pid, lease_until FROM objectives WHERE id=?", (oid,)).fetchone()
        if not row:
            return False
        if _held(row, now) and row["worker_pid"] != os.getpid():
            return False
        won = con.execute("UPDATE objectives SET worker_pid=?, lease_until=? WHERE id=? AND worker_pid IS ? AND "
                          "lease_until IS ?", (os.getpid(), now + LEASE_S, oid, row["worker_pid"],
                                               row["lease_until"])).rowcount
        con.commit()
        return won == 1
    finally:
        con.close()


def renew(oid):
    con = _db()
    con.execute("UPDATE objectives SET lease_until=? WHERE id=? AND worker_pid=?", (time.time() + LEASE_S, oid,
                                                                                   os.getpid()))
    con.commit()
    con.close()


def release(oid):
    con = _db()
    con.execute("UPDATE objectives SET worker_pid=NULL, lease_until=NULL WHERE id=? AND worker_pid=?",
                (oid, os.getpid()))
    con.commit()
    con.close()


def recover(con=None, now=None):
    """Steps left 'running' by a dead worker go back to pending, marked as interrupted (their next run checks what
    already exists). A step interrupted MAX_INTERRUPTS times blocks the objective instead of looping forever.
    Steps in 'verifying' are left alone: their work is saved and only the check re-runs."""
    now = now or time.time()
    own = con is None
    con = con or _db()
    n = 0
    for obj in con.execute("SELECT id, title, worker_pid, lease_until FROM objectives "
                           "WHERE status IN ('active','planning')").fetchall():
        if _held(obj, now):
            continue
        for st in con.execute("SELECT idx, title, started, attempts, interrupts FROM steps WHERE objective_id=? AND "
                              "status='running'", (obj["id"],)).fetchall():
            if now - (st["started"] or 0) <= 120:
                continue
            k = (st["interrupts"] or 0) + 1
            if k >= MAX_INTERRUPTS:
                why = f"step {st['idx']} was interrupted {k} times"
                _set_step(con, obj["id"], st["idx"], status="failed", interrupts=k, verify=why)
                _set(con, obj["id"], status="blocked", error=why)
                _log(con, obj["id"], "blocked", why)
                _tell(obj["id"], f"Objective '{obj['title']}' is stuck: {why} (crashes or restarts mid-step). "
                                 f"Say 'retry objective {obj['id']}' to try again.")
            else:  # an interruption is not a failed attempt
                _set_step(con, obj["id"], st["idx"], status="pending", interrupts=k,
                          attempts=max(0, (st["attempts"] or 1) - 1))
                _log(con, obj["id"], "recovered", f"step {st['idx']} re-queued after an interruption")
            n += 1
        if obj["worker_pid"]:
            _set(con, obj["id"], worker_pid=None, lease_until=None)
    if own:
        con.commit()
        con.close()
    return n


def tick(max_steps=1, settings=None, run_fn=None, verify_fn=None, asker=None):
    """Advance in-flight objectives in this process (tests and manual runs; the cron uses supervise)."""
    con = _db()
    recover(con)
    con.commit()
    objs = con.execute("SELECT * FROM objectives WHERE status IN ('planning','active') ORDER BY updated").fetchall()
    con.close()
    done = []
    for obj in objs:
        if len(done) >= max_steps:
            break
        if not claim(obj["id"]):
            continue  # another worker has it
        try:
            with budget.background("objective", obj["id"]):
                if obj["status"] == "planning":
                    plan(obj["id"], asker=asker, settings=settings)
                    continue
                st = run_step(obj["id"], settings=settings, run_fn=run_fn, verify_fn=verify_fn)
        finally:
            release(obj["id"])
        if st:
            done.append((obj["id"], st))
    return done


def _check_approvals(con, now):
    """Objectives waiting for a yes: a denial blocks them, a lost resume is healed, an expired request blocks them
    (with a message) instead of leaving them waiting forever."""
    from . import policy
    for obj in con.execute("SELECT * FROM objectives WHERE status='waiting_approval'").fetchall():
        try:
            info = json.loads(obj["approval_ids"] or "{}")
        except ValueError:
            info = {}
        items = [policy.get(i) for i in info.get("ids", []) if i]
        items = [x for x in items if x]
        if not items:
            continue
        live = [x for x in items if x["state"] == "pending" and x["expires"] > now]
        denied = [x for x in items if x["state"] == "denied"]
        if denied:
            why = f"you said no to: {denied[0]['summary']}"
            _set(con, obj["id"], status="blocked", error=why[:500])
            _log(con, obj["id"], "denied", why)
            _tell(obj["id"], f"Objective '{obj['title']}' stopped: {why}. Tell me another way to do it, or say "
                             f"'cancel objective {obj['id']}'.")
        elif live:
            continue
        elif all(x["state"] in ("granted", "used") for x in items):
            _set(con, obj["id"], status="active")  # granted but the resume was lost (gateway restart)
            _log(con, obj["id"], "approved", "resumed after restart")
        else:
            why = "the approval request expired without an answer"
            _set(con, obj["id"], status="blocked", error=why)
            _log(con, obj["id"], "expired", why)
            _tell(obj["id"], f"Objective '{obj['title']}' is paused: {why}. Say 'retry objective {obj['id']}' to "
                             "ask again.")


def settle_approvals(now=None):
    """Apply approval answers now (after a deny, instead of waiting for the next supervisor run)."""
    con = _db()
    _check_approvals(con, now or time.time())
    con.commit()
    con.close()


def supervise(kick_fn=None, now=None):
    """Cron (every 5 min) + gateway start: re-queue interrupted steps, settle approvals, then make sure every
    in-flight objective that isn't parked on the budget has a live worker. Long steps never run in the cron script."""
    now = now or time.time()
    con = _db()
    recover(con, now)
    _check_approvals(con, now)
    con.commit()
    rows = con.execute("SELECT id, worker_pid, lease_until, deferred_until, defer_reason FROM objectives "
                       "WHERE status IN ('planning','active')").fetchall()
    con.close()
    started, room = [], None
    for r in rows:
        if _held(r, now):
            continue
        if (r["deferred_until"] or 0) > now:
            # Parked on the budget: if there is room again (the user raised the limit), go now, not at the reset.
            if room is None:
                room = budget.check(Settings.load().agent_model, "objective", now=now)[0]
            if not (room and "budget" in (r["defer_reason"] or "")):
                continue
            con = _db()
            _set(con, r["id"], deferred_until=None, defer_reason=None)
            _log(con, r["id"], "resumed", "background budget has room again")
            con.commit()
            con.close()
        (kick_fn or (lambda o: kick(o, user=False)))(r["id"])
        started.append(r["id"])
    return started


def kick(oid, user=True):
    """Start working on an objective now, in a detached process (the chat isn't blocked). user=True when the user
    just started or approved it (it then doesn't wait for them to stop chatting); the supervisor passes False."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=repo + os.pathsep + os.environ.get("PYTHONPATH", ""),
               WAGGLE_USER_KICK="1" if user else "")
    subprocess.Popen([sys.executable, "-m", "waggle.objectives", "work", oid], creationflags=flags, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


SHORT_WAIT_S = 180  # a budget wait this short (chat, busy workers) is slept off in the worker
MAX_WAIT_S = 600


def work(oid, max_steps=MAX_STEPS * MAX_ATTEMPTS, settings=None, run_fn=None, verify_fn=None, user=None,
         sleep=time.sleep):
    """Keep running steps of one objective until it finishes, blocks, waits, or runs out of budget."""
    if not claim(oid):
        return "busy"
    if user is None:
        user = os.environ.get("WAGGLE_USER_KICK") == "1"
    last, waited = None, 0.0
    try:
        with budget.background("objective", oid, user_started=user):
            for _ in range(max_steps + 4):
                renew(oid)
                con = _db()
                row = con.execute("SELECT status FROM objectives WHERE id=?", (oid,)).fetchone()
                con.close()
                if not row:
                    break
                if row["status"] == "planning":
                    if not plan(oid, settings=settings):
                        break
                    continue
                st = run_step(oid, settings=settings, run_fn=run_fn, verify_fn=verify_fn)
                if st is None:  # finished, cancelled or nothing runnable
                    break
                last = st
                if st == "waiting_budget":
                    con = _db()
                    until = con.execute("SELECT deferred_until FROM objectives WHERE id=?", (oid,)).fetchone()[0]
                    con.close()
                    pause = (until or 0) - time.time()
                    if 0 < pause <= SHORT_WAIT_S and waited + pause <= MAX_WAIT_S:
                        renew(oid)
                        sleep(pause + 1)
                        waited += pause + 1
                        continue
                    break
                if st in ("blocked", "waiting_approval"):
                    break
    finally:
        release(oid)
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


# Which status changes the user (or an agent) may make. A finished or cancelled objective never runs again.
ALLOWED_FROM = {"active": ("paused", "blocked", "failed", "planning", "active"),
                "paused": ("planning", "active", "blocked", "waiting_approval"),
                "cancelled": ("planning", "active", "blocked", "waiting_approval", "paused", "failed")}


def set_status(key, status, extra_runs=0):
    oid = find(key)
    if not oid:
        return None
    con = _db()
    row = con.execute("SELECT status, (SELECT COUNT(*) FROM steps WHERE objective_id=id) AS n FROM objectives "
                      "WHERE id=?", (oid,)).fetchone()
    if row["status"] not in ALLOWED_FROM.get(status, ()):
        con.close()
        return None
    if status == "active":
        con.execute("UPDATE steps SET status='pending', attempts=0, interrupts=0 WHERE objective_id=? AND "
                    "status='failed'", (oid,))
        if extra_runs:
            con.execute("UPDATE objectives SET max_runs=max_runs+? WHERE id=?", (extra_runs, oid))
        if not row["n"]:
            status = "planning"  # planning had failed: plan again
    _set(con, oid, status=status, error=None, deferred_until=None, defer_reason=None)
    _log(con, oid, status, "by user")
    con.commit()
    con.close()
    return oid


def resume_after_approval(session, granted=None):
    """The user approved an objective's action: once nothing for it is still pending, make it active again (the
    re-run is told exactly what was approved) and keep going."""
    if not session.startswith("objective-"):
        return None
    from . import policy
    oid = session.split("-", 1)[1]
    con = _db()
    row = con.execute("SELECT status, approval_ids FROM objectives WHERE id=?", (oid,)).fetchone()
    if not row or row["status"] != "waiting_approval":
        con.close()
        return None
    if policy.pending(session):
        con.close()
        return None  # more approvals for this objective are still open
    try:
        info = json.loads(row["approval_ids"] or "{}")
    except ValueError:
        info = {}
    if granted and info.get("step"):
        _set_step(con, oid, info["step"], approved="; ".join(g["summary"] for g in granted)[:1000])
    _set(con, oid, status="active", deferred_until=None)
    _log(con, oid, "approved", "user approved: " + "; ".join(g["summary"] for g in granted or [])[:300])
    con.commit()
    con.close()
    kick(oid)
    return oid


def status(key=None):
    con = _db()
    if key:
        oid = find(key)
        if not oid:
            con.close()
            return None
        obj = dict(con.execute("SELECT * FROM objectives WHERE id=?", (oid,)).fetchone())
        obj["steps"] = [dict(r) for r in con.execute("SELECT idx, title, agent, status, attempts, interrupts, verify, "
                                                     "substr(result,1,600) AS result FROM steps WHERE objective_id=? "
                                                     "ORDER BY idx", (oid,))]
        obj["log"] = [dict(r) for r in con.execute("SELECT time, kind, text FROM log WHERE objective_id=? "
                                                   "ORDER BY time DESC LIMIT 12", (oid,))]
        con.close()
        return obj
    rows = [dict(r) for r in con.execute(
        "SELECT o.id, o.title, o.status, o.updated, o.project, o.note_path, (SELECT COUNT(*) FROM steps s WHERE s.objective_id=o.id"
        " AND s.status='done') AS done, (SELECT COUNT(*) FROM steps s WHERE s.objective_id=o.id) AS total "
        "FROM objectives o ORDER BY (o.status IN ('done','failed','cancelled')), o.updated DESC LIMIT 20")]
    con.close()
    return rows


def ops(now=None):
    """What the Command Center shows: in flight (and why parked), waiting for approval, blocked, recent failures,
    live workers and the last recovery."""
    from . import policy
    now = now or time.time()
    con = _db()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT o.id, o.title, o.status, o.error, o.updated, o.worker_pid, o.lease_until, o.deferred_until, "
            "o.defer_reason, (SELECT COUNT(*) FROM steps s WHERE s.objective_id=o.id AND s.status='done') AS done, "
            "(SELECT COUNT(*) FROM steps s WHERE s.objective_id=o.id) AS total FROM objectives o "
            "WHERE o.status IN ('planning','active','waiting_approval','blocked','paused') ORDER BY o.updated DESC")]
        failures = [dict(r) for r in con.execute(
            "SELECT l.objective_id AS id, o.title, l.time, l.kind, l.text FROM log l JOIN objectives o ON o.id="
            "l.objective_id WHERE l.kind IN ('blocked','retry','error','expired','denied') ORDER BY l.time DESC "
            "LIMIT 8")]
        rec = con.execute("SELECT MAX(time) FROM log WHERE kind='recovered'").fetchone()[0]
    finally:
        con.close()
    for r in rows:
        r["worker_live"] = bool(r["worker_pid"]) and (r["lease_until"] or 0) > now and _alive(r["worker_pid"])
        r["parked"] = (r["deferred_until"] or 0) > now
    waiting = []
    for p in policy.pending():
        sess = str(p.get("session", ""))
        oid = sess.split("-", 1)[1] if sess.startswith("objective-") else ""
        title = next((r["title"] for r in rows if r["id"] == oid), "")
        waiting.append({"code": p["code"], "summary": p["summary"], "reason": p["reason"], "expires": p["expires"],
                        "objective": oid, "title": title, "kind": sess.split("-", 1)[0] if sess else "chat"})
    return {"active": [r for r in rows if r["status"] in ("planning", "active")],
            "blocked": [r for r in rows if r["status"] in ("blocked", "paused")],
            "waiting": waiting, "failures": failures, "workers": [r for r in rows if r["worker_live"]],
            "last_recovery": rec}


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
        try:
            work(sys.argv[2])
        except Exception as e:  # a detached worker has no console: leave a trace the supervisor and user can see
            events.log("objective_worker_error", id=sys.argv[2], error=f"{type(e).__name__}: {e}"[:300])
            raise
    else:
        budget.set_background("objective")
        tick()


if __name__ == "__main__":
    main()
