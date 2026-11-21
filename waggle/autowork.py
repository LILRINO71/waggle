"""The work loop: one step toward one enabled goal at a time, inside the background allowance, proven by code.

run_once() (the core job autonomy.work) does, in order, and stops at the first thing that says no:
  1. the switch       autonomy_enabled (goals.py) must be on; at most max_steps_per_day steps a day
  2. the goal         the enabled, unexpired goal that waited longest since its last step
  3. pick a step      one model call (claude_cli.ask, the project's wrapper) inside budget.background +
                      budget.spend: when the allowance refuses, the refusal is recorded and the loop stops. It
                      never retries on another path. The model proposes an action kind, instructions, a working
                      folder and the evidence that will prove the step.
  4. the charter      the proposal is checked against the goal's charter (action kind, every path inside the write
                      roots, an allowed test command); a proposal outside it is recorded as charter_refused and
                      nothing runs
  5. do it            through the objectives engine: an objective with one given step (origin 'autowork', so it
                      never messages the owner and never retries on its own), one run of objectives.run_step. The
                      engine's own check is replaced by the proof below.
  6. the proof        code checks the evidence: the file exists inside the write roots, was written during the step
                      (and holds the expected text); the allowed test command exits 0; a new commit exists in the
                      repository; the URL answers. The agent's report is never evidence. Outcomes:
                        proven          every check passed
                        unproven        the step ran but a check failed (the objective ends 'unproven')
                        failed          the agent errored or nothing ran
                        refused         the background allowance said no (not counted as a step)
                        charter_refused the proposal was outside the charter
                        needs_approval  the agent hit a permission request (the objective ends; the owner sees it
                                        in the evening note)
  7. indicators       every indicator of the goal is measured by code after the step (goals.MEASURES); only
                      proven steps count toward 'proven_steps'.
report() (the core job autonomy.report, before the evening note) turns the day's steps into one line per goal
and records it as a told signal, which the evening note lists under "Did:".
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import autonomy_store as store_
from . import goals
from .config import Settings

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TEST_TIMEOUT_S = 600
AGENT_FOR = {"research": "researcher", "write_note": "general", "edit_code": "coder", "run_tests": "coder",
             "commit_local": "coder"}
EVIDENCE_TYPES = ("file_exists", "tests_pass", "commit_exists", "url_ok")
COUNTED = ("proven", "unproven", "failed", "charter_refused", "needs_approval")  # refused is not a step
REPORT_META = "autonomy.last_report"

PICK_SYSTEM = """You choose the single next step an autonomous assistant takes toward one of its owner's standing
goals, while the owner is away. Pick something small that can be finished in one agent run of at most 15 minutes and
whose result code can check. Stay strictly inside the charter: only its action kinds, only inside its write roots,
no messages, posts, pushes, purchases or accounts. Name the evidence that will prove the step:
  file_exists   {"type":"file_exists","path":<absolute path inside a write root>,"contains":<optional text>}
  tests_pass    {"type":"tests_pass","cmd":["python","-m","pytest",...] or ["npm","test"] or ["node","--test"],
                 "cwd":<folder inside a write root>}
  commit_exists {"type":"commit_exists","repo":<repository folder inside a write root>}
  url_ok        {"type":"url_ok","url":<https URL that should answer>}
If nothing useful and provable fits the charter, return action "none". Reply JSON only."""
PICK_SCHEMA = {"type": "object", "properties": {
    "action": {"type": "string"}, "title": {"type": "string"}, "instructions": {"type": "string"},
    "workdir": {"type": "string"}, "why": {"type": "string"},
    "evidence": {"type": "array", "items": {"type": "object"}}},
    "required": ["action", "title", "instructions", "evidence"]}


# ---- records ----------------------------------------------------------------------------------------------------

def _day(now, s):
    from . import budget
    return budget.day_key(now, s)


def record_step(goal_id, outcome, *, title="", action="", objective_id=None, detail="", evidence=None, units=0.0,
                indicators=None, now=None, settings=None):
    s = settings or Settings.load()
    now = store_.now_or(now)
    con = store_.db()
    try:
        cur = con.execute("INSERT INTO steps(goal_id, time, day, title, action, objective_id, outcome, detail, "
                          "evidence, units, indicators) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (goal_id, now, _day(now, s), str(title)[:200], action, objective_id, outcome,
                           str(detail)[:1000], store_.dumps(evidence or []), round(float(units or 0), 4),
                           store_.dumps(indicators or {})))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def steps(goal_id=None, since=0.0):
    con = store_.db()
    try:
        q, args = "SELECT * FROM steps WHERE time>=?", [since]
        if goal_id:
            q, args = q + " AND goal_id=?", [*args, goal_id]
        rows = [dict(r) for r in con.execute(q + " ORDER BY time", args)]
    finally:
        con.close()
    for r in rows:
        r["evidence"] = store_.loads(r["evidence"], [])
        r["indicators"] = store_.loads(r["indicators"], {})
    return rows


def steps_today(now=None, settings=None):
    s = settings or Settings.load()
    con = store_.db()
    try:
        return con.execute(f"SELECT COUNT(*) FROM steps WHERE day=? AND outcome IN ({','.join('?' * len(COUNTED))})",
                           (_day(store_.now_or(now), s), *COUNTED)).fetchone()[0]
    finally:
        con.close()


# ---- indicators -------------------------------------------------------------------------------------------------

def _allowed_test_cmd(cmd):
    """The command list to run for an allowed test command, or None. Only test runners, never a shell."""
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        return None
    head = cmd[0].lower()
    if head in ("python", "python3", "py") and cmd[1:3] == ["-m", "pytest"]:
        return [sys.executable, *cmd[1:]]
    if head == "npm" and cmd[1:2] == ["test"]:
        return cmd
    if head == "node" and cmd[1:2] == ["--test"]:
        return cmd
    return None


def run_tests(cmd, cwd, timeout=TEST_TIMEOUT_S):
    """(exit code or None, last summary line) for an allowed test command."""
    argv = _allowed_test_cmd(cmd)
    if argv is None:
        return None, f"not an allowed test command: {cmd}"
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"{type(e).__name__}: {e}"[:200]
    lines = [x for x in (p.stdout or "").splitlines() + (p.stderr or "").splitlines() if x.strip()]
    summary = next((x for x in reversed(lines) if re.search(r"passed|failed|error|no tests", x)), lines[-1:] or [""])
    return p.returncode, (summary if isinstance(summary, str) else summary[0])[:200]


def _fmt(template, goal_id, s):
    from . import config
    return str(template).format(home=str(Path.home()), vault=s.vault, jobs=s.jobs_dir,
                                waggle_home=str(config.HOME), goal=goal_id)


def measure(goal, indicator, settings=None, now=None):
    """(value or None, detail) for one indicator, by code only."""
    s = settings or Settings.load()
    now = store_.now_or(now)
    m = indicator.get("measure") or {}
    typ = m.get("type")
    week = now - 7 * 86400
    if typ == "proven_steps":
        n = sum(1 for r in steps(goal["id"], week) if r["outcome"] == "proven")
        return float(n), "proven steps in 7 days"
    if typ == "files_changed":
        root = Path(_fmt(m.get("root", ""), goal["id"], s))
        if not root.is_dir():
            return 0.0, "folder does not exist yet"
        n = 0
        for i, p in enumerate(root.rglob("*")):
            if i > 20000:
                break
            try:
                if p.is_file() and p.stat().st_mtime >= week:
                    n += 1
            except OSError:
                continue
        return float(n), f"files changed in 7 days under the goal folder"
    if typ == "commits":
        from . import activity
        snap = activity.workstreams()
        if not snap.get("time"):
            return None, "no activity snapshot yet"
        want = {r.lower() for r in m.get("repos") or []}
        n = sum(w["evidence"].get("commits_7d", 0) for w in snap["workstreams"] if not want or w["key"] in want)
        return float(n), "commits in 7 days (HEAD logs)"
    if typ == "tests_failing":
        cwd = _fmt(m.get("cwd", ""), goal["id"], s)
        code, summary = run_tests(m.get("cmd"), cwd)
        if code is None:
            return None, summary
        if code == 0:
            return 0.0, summary
        nums = [int(x) for x in re.findall(r"(\d+) (?:failed|errors?)\b", summary)]
        return (float(sum(nums)) if nums else None), summary
    return None, f"unknown measure {typ!r}"


def measure_all(goal, settings=None, now=None):
    """Measure every indicator of the goal and store the readings. Returns {name: value}."""
    now = store_.now_or(now)
    out = {}
    con = store_.db()
    try:
        for ind in goal.get("indicators") or []:
            try:
                value, detail = measure(goal, ind, settings, now)
            except Exception as e:  # noqa: BLE001 - one broken measure never stops the others
                value, detail = None, f"{type(e).__name__}: {e}"[:200]
            out[ind["name"]] = value
            con.execute("INSERT INTO readings(goal_id, indicator, time, value, detail) VALUES(?,?,?,?,?)",
                        (goal["id"], ind["name"], now, value, str(detail)[:300]))
        con.commit()
    finally:
        con.close()
    return out


# ---- proof ------------------------------------------------------------------------------------------------------

def _git(args, cwd):
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _fetch(url, timeout=10):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "waggle-proof"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https only, checked by the caller
        return r.status


def before_state(evidence):
    """What must be remembered before the step runs so the proof can see a change (HEAD of each repo)."""
    return {str(ev.get("repo")): _git(["rev-parse", "HEAD"], ev.get("repo")) for ev in evidence
            if ev.get("type") == "commit_exists" and ev.get("repo")}


def prove(evidence, goal, started, before=None, settings=None, fetch=None):
    """Check every piece of evidence by code. Returns (proven, [check results]). No evidence proves nothing."""
    s = settings or Settings.load()
    roots = goals.expand_roots(goal["charter"], goal["id"], s)
    results = []
    for ev in evidence or []:
        typ = ev.get("type")
        res = {"type": typ, "ok": False}
        try:
            if typ == "file_exists":
                p = Path(str(ev.get("path", ""))).expanduser()
                res["path"] = str(p)
                if not goals._inside(p, roots):
                    res["why"] = "outside the write roots"
                elif not p.is_file():
                    res["why"] = "does not exist"
                elif p.stat().st_mtime < started - 2:
                    res["why"] = "was not written during this step"
                elif ev.get("contains") and str(ev["contains"]) not in p.read_text(encoding="utf-8",
                                                                                   errors="replace"):
                    res["why"] = "does not contain the expected text"
                else:
                    res.update(ok=True, why=f"{p.stat().st_size} bytes")
            elif typ == "tests_pass":
                cwd = Path(str(ev.get("cwd", ""))).expanduser()
                if not goals._inside(cwd, roots):
                    res["why"] = "test folder outside the write roots"
                else:
                    code, summary = run_tests(ev.get("cmd"), cwd)
                    res.update(ok=code == 0, why=f"exit {code}: {summary}")
            elif typ == "commit_exists":
                repo = str(ev.get("repo", ""))
                head = _git(["rev-parse", "HEAD"], repo) if goals._inside(repo, roots) else None
                old = (before or {}).get(repo)
                if head is None:
                    res["why"] = "no repository inside the write roots"
                elif head == old:
                    res["why"] = "no new commit"
                elif _git(["cat-file", "-e", f"{head}^{{commit}}"], repo) is None:
                    res["why"] = "HEAD is not a commit"
                else:
                    res.update(ok=True, why=f"new commit {head[:10]}")
            elif typ == "url_ok":
                url = str(ev.get("url", ""))
                if not url.startswith("https://"):
                    res["why"] = "only https URLs are checked"
                else:
                    status = (fetch or _fetch)(url)
                    res.update(ok=200 <= int(status) < 400, why=f"HTTP {status}")
            else:
                res["why"] = "unknown evidence type: not provable"
        except Exception as e:  # noqa: BLE001 - a check that crashes is a failed check
            res["why"] = f"{type(e).__name__}: {e}"[:200]
        results.append(res)
    return bool(results) and all(r["ok"] for r in results), results


# ---- choosing and checking the step -----------------------------------------------------------------------------

def _context(goal, s, now):
    from . import activity
    recent = steps(goal["id"], now - 14 * 86400)[-6:]
    readings = {}
    con = store_.db()
    try:
        for ind in goal["indicators"]:
            row = con.execute("SELECT value FROM readings WHERE goal_id=? AND indicator=? ORDER BY time DESC "
                              "LIMIT 1", (goal["id"], ind["name"])).fetchone()
            readings[ind["name"]] = row[0] if row else None
    finally:
        con.close()
    ws = [{"name": w["name"], "score": w["score"], "topic": w.get("topic", "")}
          for w in activity.workstreams().get("workstreams", [])[:5]]
    c = goal["charter"]
    return (f"GOAL: {goal['title']}\nWHY: {goal['why']}\n"
            f"INDICATORS (measured by code): {json.dumps(readings)}\n"
            f"CHARTER: actions={c['actions']}; write roots={[str(r) for r in goals.expand_roots(c, goal['id'], s)]};"
            f" outward channels=none; money=0; expires {c['expires']}\n"
            f"WHAT THE OWNER IS WORKING ON NOW: {json.dumps(ws)}\n"
            f"RECENT STEPS: {json.dumps([{k: r[k] for k in ('title', 'outcome', 'detail')} for r in recent])}")


IDLE_PICKS = 2          # this many "nothing useful to do" picks in a row ...
IDLE_REST_S = 6 * 3600  # ... and the next look waits this long


def idle_check(goal, now):
    """The cheap check in front of the pick call, as a zero-argument function for gates.evaluate: when the goal's
    last IDLE_PICKS picks both found nothing useful (a failed step with no action, not a model outage) and the
    newest was under IDLE_REST_S ago, another pick would almost surely say the same."""
    def check():
        rows = [r for r in steps(goal["id"], now - 24 * 3600)][-IDLE_PICKS:]
        idle = [r for r in rows if r["outcome"] == "failed" and not r["action"]
                and not str(r["detail"] or "").startswith("model call failed")]
        if len(rows) == IDLE_PICKS and len(idle) == IDLE_PICKS and now - idle[-1]["time"] < IDLE_REST_S:
            ago = int((now - idle[-1]["time"]) / 60)
            return {"wake": False, "p_needed": 0.04, "data": {"why": f"the last {IDLE_PICKS} picks found nothing "
                                                                     f"to do; the newest was {ago} min ago"}}
        return {"wake": True, "p_needed": 1.0}
    return check


def pick_step(goal, settings=None, now=None, asker=None):
    """One model call inside the background allowance. Returns the proposal dict, or {"refused": why} when the
    allowance says no, or {"error": why}."""
    from . import budget, claude_cli
    s = settings or Settings.load()
    model = s.deep_model
    tag = f"goal-{goal['id']}"
    try:
        with budget.background("autowork", objective=tag):
            with budget.spend(model, purpose="autowork-pick", objective=tag, settings=s):
                res = (asker or claude_cli.ask)(_context(goal, s, store_.now_or(now)), model=model,
                                                system=PICK_SYSTEM, schema=PICK_SCHEMA, purpose="autowork-pick",
                                                effort="low")
    except budget.Deferred as e:
        return {"refused": e.reason}
    except claude_cli.ClaudeError as e:
        if getattr(e, "kind", None) in ("budget", "limit"):
            return {"refused": str(e)}
        return {"error": f"model call failed: {e}"[:300]}
    prop = (res or {}).get("json") or {}
    if not isinstance(prop, dict) or not prop.get("action"):
        return {"error": "the model returned no step"}
    return prop


def check_proposal(goal, prop, settings=None, now=None):
    """(ok, why) for a proposed step under the goal's charter: the action kind, every folder and path, the test
    commands and the evidence types."""
    c, gid = goal["charter"], goal["id"]
    action = prop.get("action")
    ok, why = goals.charter_allows(c, action=action, goal_id=gid, settings=settings, now=now)
    if not ok:
        return False, why
    evidence = prop.get("evidence") or []
    if not evidence:
        return False, "the step names no evidence, so it could never be proven"
    paths = [prop["workdir"]] if prop.get("workdir") else []
    for ev in evidence:
        typ = ev.get("type")
        if typ not in EVIDENCE_TYPES:
            return False, f"evidence type {typ!r} cannot be checked"
        if typ == "file_exists":
            paths.append(ev.get("path") or "")
        elif typ == "tests_pass":
            paths.append(ev.get("cwd") or "")
            if _allowed_test_cmd(ev.get("cmd")) is None:
                return False, f"test command not allowed: {ev.get('cmd')}"
            if "run_tests" not in c["actions"] and action != "run_tests":
                return False, "the charter does not allow running tests"
        elif typ == "commit_exists":
            paths.append(ev.get("repo") or "")
            if "commit_local" not in c["actions"]:
                return False, "the charter does not allow commits"
        elif typ == "url_ok" and not str(ev.get("url", "")).startswith("https://"):
            return False, "only https URLs can be checked"
    for p in paths:
        ok, why = goals.charter_allows(c, path=p, goal_id=gid, settings=settings, now=now)
        if not ok:
            return False, why
    return True, "ok"


def _step_text(goal, prop, roots):
    ev = "; ".join(json.dumps(e) for e in prop.get("evidence") or [])
    return (f"{prop.get('instructions', '')}\n\nThis step serves the standing goal '{goal['title']}' and runs while "
            f"the owner is away. Limits: write only inside {', '.join(str(r) for r in roots)}"
            + (f" (work in {prop['workdir']})" if prop.get("workdir") else "")
            + ". Do not send messages, post, push, publish, buy anything or create accounts. "
            f"Waggle checks the result itself, by code: {ev}. Your own report is not taken as proof.")


def _units(objective_ids, since):
    from . import budget
    con = budget._db()
    try:
        q = ",".join("?" * len(objective_ids))
        return float(con.execute(f"SELECT COALESCE(SUM(units),0) FROM usage WHERE time>=? AND objective IN ({q})",
                                 (since, *objective_ids)).fetchone()[0])
    finally:
        con.close()


# ---- the loop ---------------------------------------------------------------------------------------------------

def run_once(settings=None, now=None, asker=None, run_fn=None, fetch=None, goal_id=None, gate=None):
    """At most one step toward one goal. Returns a small report of what happened (and why not, when nothing).
    gate: a zero-argument check replacing the default one in front of the pick call (gates.evaluate), or False."""
    from . import objectives
    s = settings or Settings.load()
    now = store_.now_or(now)
    if not goals.autonomy_enabled():
        return {"skipped": "autonomy is off (python -m waggle.goals autonomy on)"}
    limit = goals.max_steps_per_day()
    done = steps_today(now, s)
    if done >= limit:
        return {"skipped": f"{done} of {limit} steps already taken today"}
    candidates = [g for g in goals.enabled_goals(now) if goal_id in (None, g["id"])]
    if not candidates:
        return {"skipped": "no enabled goal"}
    goal = sorted(candidates, key=lambda g: (g["last_step"] or 0, g["created"] or 0))[0]
    started = time.time()
    tag = f"goal-{goal['id']}"
    if gate is not False:
        from . import gates
        gv = gates.evaluate("autowork.pick", gate or idle_check(goal, now), settings=s, now=now, subject=goal["id"])
        if gv.skip:  # no model call; the goal goes to the back of the line so another one gets the turn
            goals.touch(goal["id"], now)
            return {"goal": goal["id"], "outcome": "gated", "skipped": gv.reason}
    prop = pick_step(goal, s, now, asker)
    if "refused" in prop:
        record_step(goal["id"], "refused", title="choose the next step", detail=prop["refused"], now=now,
                    settings=s)
        return {"goal": goal["id"], "outcome": "refused", "why": prop["refused"]}
    goals.touch(goal["id"], now)
    if "error" in prop or prop.get("action") == "none":
        why = prop.get("error") or prop.get("why") or "no useful provable step inside the charter"
        record_step(goal["id"], "failed", title=prop.get("title") or "choose the next step", detail=why,
                    units=_units([tag], started), now=now, settings=s)
        return {"goal": goal["id"], "outcome": "failed", "why": why}
    prop["title"] = str(prop.get("title") or "next step")[:120]
    ok, why = check_proposal(goal, prop, s, now)
    if not ok:
        record_step(goal["id"], "charter_refused", title=prop.get("title", ""), action=prop.get("action", ""),
                    detail=why, evidence=prop.get("evidence"), units=_units([tag], started), now=now, settings=s)
        return {"goal": goal["id"], "outcome": "charter_refused", "why": why}
    evidence = prop["evidence"]
    roots = goals.expand_roots(goal["charter"], goal["id"], s)
    before = before_state(evidence)
    proof = {}

    def verify(task, report, settings=None):  # the engine's check, done by code instead of a model
        proof["result"] = prove(evidence, goal, started, before, s, fetch)
        okk, checks = proof["result"]
        return okk, "" if okk else "; ".join(f"{c['type']}: {c.get('why')}" for c in checks if not c["ok"])

    oid = objectives.create_planned(
        f"[{goal['title']}] {prop['title']}",
        [{"title": prop["title"][:120], "detail": _step_text(goal, prop, roots),
          "agent": AGENT_FOR.get(prop["action"], "general"),
          "done_when": "checked by Waggle: " + "; ".join(e.get("type", "?") for e in evidence)}],
        title=f"{goal['title']}: {prop['title']}"[:80], origin="autowork")
    st = objectives.run_step(oid, settings=s, run_fn=run_fn, verify_fn=verify)
    info = objectives.status(oid) or {}
    step_row = (info.get("steps") or [{}])[0]
    if st == "done":
        outcome = "proven"
    elif st == "waiting_budget":
        outcome = "refused"
    elif st == "waiting_approval":
        outcome = "needs_approval"
    elif "result" in proof:
        outcome = "proven" if proof["result"][0] else "unproven"
    elif str(step_row.get("verify") or "").startswith("agent error") or st is None:
        outcome = "failed"
    else:  # the engine stopped before the proof (a promised file missing): prove it anyway, by code
        proof["result"] = prove(evidence, goal, started, before, s, fetch)
        outcome = "proven" if proof["result"][0] else "unproven"
    checks = proof.get("result", (False, []))[1]
    if outcome != "proven":
        why = {"refused": f"background allowance: {info.get('defer_reason') or info.get('error') or 'no room'}",
               "needs_approval": "the step needed the owner's approval",
               "failed": step_row.get("verify") or "the agent did not finish"}.get(
            outcome, "; ".join(f"{c['type']}: {c.get('why')}" for c in checks if not c["ok"]) or "not proven")
        objectives.conclude_unproven(oid, why)
    else:
        why = "; ".join(f"{c['type']}: {c.get('why')}" for c in checks)
    sid = record_step(goal["id"], outcome, title=prop["title"], action=prop["action"], objective_id=oid,
                      detail=why, evidence=checks, units=_units([tag, oid], started), now=now, settings=s)
    readings = {}
    if outcome != "refused":  # measured after the step is on record, so 'proven steps' includes it
        readings = measure_all(goal, s)
        con = store_.db()
        try:
            con.execute("UPDATE steps SET indicators=? WHERE id=?", (store_.dumps(readings), sid))
            con.commit()
        finally:
            con.close()
    return {"goal": goal["id"], "outcome": outcome, "objective": oid, "why": why, "indicators": readings}


# ---- evening note -----------------------------------------------------------------------------------------------

def goal_line(goal, rows, s, since):
    """One line for one goal: steps and outcomes since `since`, indicators with their change."""
    n = {k: sum(1 for r in rows if r["outcome"] == k) for k in ("proven", "unproven", "failed", "refused",
                                                                "charter_refused", "needs_approval")}
    taken = sum(v for k, v in n.items() if k != "refused")
    parts = [f"{taken} step{'s' if taken != 1 else ''}"]
    detail = [f"{n['proven']} proven"] + [f"{v} {k.replace('_', ' ')}" for k, v in n.items()
                                          if v and k not in ("proven", "refused")]
    if n["refused"]:
        detail.append(f"{n['refused']} held back by the allowance")
    parts.append("(" + ", ".join(detail) + ")")
    changes = []
    con = store_.db()
    try:
        for ind in goal["indicators"]:
            vals = [r[0] for r in con.execute("SELECT value FROM readings WHERE goal_id=? AND indicator=? AND time>=? "
                                              "AND value IS NOT NULL ORDER BY time", (goal["id"], ind["name"], since))]
            prev = con.execute("SELECT value FROM readings WHERE goal_id=? AND indicator=? AND time<? AND value IS "
                               "NOT NULL ORDER BY time DESC LIMIT 1", (goal["id"], ind["name"], since)).fetchone()
            if vals:
                start = prev[0] if prev else vals[0]
                changes.append(f"{ind['name']} {start:g}->{vals[-1]:g}")
    finally:
        con.close()
    proven = [r["title"] for r in rows if r["outcome"] == "proven"][:2]
    line = f"Goal {goal['title']}: " + " ".join(parts)
    if proven:
        line += "; proven: " + "; ".join(t[:60] for t in proven)
    if changes:
        line += "; " + ", ".join(changes)
    return line


def report(settings=None, now=None):
    """One told signal per goal that did anything since the last report (the evening note lists them)."""
    from . import initiative
    s = settings or Settings.load()
    now = store_.now_or(now)
    from waggle.core import store
    since = float(store.get_meta(REPORT_META, 0) or 0) or now - 86400
    lines = []
    for g in goals.all_goals():
        rows = steps(g["id"], since)
        rows = [r for r in rows if r["time"] < now + 1]
        if not rows:
            continue
        lines.append(goal_line(g, rows, s, since))
    sigs = [{"key": f"autonomy:{i}:{int(now)}", "kind": "objective", "title": line, "told": True,
             "topic": "autonomy"} for i, line in enumerate(lines)]
    if sigs:
        initiative.record(sigs, now)
    store.set_meta(REPORT_META, now)
    return {"lines": lines}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["report"]:
        print("\n".join(report()["lines"]) or "nothing to report")
    else:
        print(json.dumps(run_once(goal_id=argv[0] if argv else None), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
