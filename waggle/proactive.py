"""Repo and objective helpers for the initiative watchers, and the old proactive cron entry (now a thin caller).

The old pipeline (the local 4B model re-reading vault notes, a 4-a-day Sonnet check, and delivery by tier where only
"critical" reached the phone) is replaced by fact watchers (watchers.py) and a reasoning step (initiative.py),
driven by core timers (tick.register_core). What stays here: find_repos/_git (used by the watchers), git_watch, stale_watch and
objective_watch (objectives that are blocked, waiting for a yes, or stalled).
"""
import datetime as dt
import hashlib
import json
import subprocess
import time
from pathlib import Path

from . import projects
from .config import Settings, data_path



# ---- state ---------------------------------------------------------------------------------------

def _state():
    p = data_path("proactive_state.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"seen": {}, "rr": 0, "verifications": {}}


def _save(st):
    data_path("proactive_state.json").write_text(json.dumps(st, indent=2), encoding="utf-8")


def fingerprint(f):
    return hashlib.sha1(f"{f.get('project')}|{f.get('source')}|{f['title'].lower()}".encode()).hexdigest()[:12]


# ---- free watchers -------------------------------------------------------------------------------

def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20,
                       encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else None


def find_repos(settings):
    repos = []
    for root in settings.watch_roots:
        rp = Path(root)
        if rp.exists():
            repos += [p.parent for p in rp.glob("*/.git")]
    repos += [Path(p) for p in settings.extra_repos if (Path(p) / ".git").exists()]
    return sorted(set(repos))


def repo_project(repo, settings=None):
    hits = projects.find(repo.name.replace("-", " ").replace("_", " "), settings)
    return hits[0] if hits else None


def git_watch(settings, now=None):
    """Unpushed commits and old uncommitted work: the 'push after major changes' rule."""
    now = now or time.time()
    out = []
    for repo in find_repos(settings):
        status = _git(repo, "status", "--porcelain")
        if status is None:
            continue
        ahead = _git(repo, "rev-list", "--count", "@{u}..HEAD")
        if ahead and ahead.isdigit() and int(ahead) > 0:
            out.append({"source": "git", "project": repo_project(repo, settings), "tier": "important",
                        "confidence": 1.0, "verified": True,
                        "title": f"{repo.name}: {ahead} commit(s) not pushed",
                        "detail": f"Run `git push` in {repo}"})
        if status:
            changed = [l[3:] for l in status.splitlines()]
            newest = 0.0
            for rel in changed[:200]:
                try:
                    newest = max(newest, (repo / rel.strip('"')).stat().st_mtime)
                except OSError:
                    pass
            age_days = (now - newest) / 86400 if newest else 0
            if age_days >= 2:
                out.append({"source": "git", "project": repo_project(repo, settings), "tier": "interesting",
                            "confidence": 1.0, "verified": True,
                            "title": f"{repo.name}: {len(changed)} uncommitted change(s), untouched {age_days:.0f} days",
                            "detail": "Commit and push, or discard if abandoned."})
    return out


def stale_watch(settings, today=None):
    today = today or dt.date.today()
    out = []
    for p in projects.all_projects(settings):
        if p["status"] != "active" or not p["updated"]:
            continue
        try:
            age = (today - dt.date.fromisoformat(p["updated"])).days
        except ValueError:
            continue
        open_tasks = [t for t in projects.tasks(p["name"], settings) if not t["done"]]
        if age >= settings.stale_days and open_tasks:
            out.append({"source": "stale", "project": p["name"], "tier": "interesting", "confidence": 1.0,
                        "verified": True, "title": f"{p['name']}: no progress logged in {age} days",
                        "detail": f"Next up: {open_tasks[0]['text']}"})
    return out


def objective_watch(now=None):
    """Background objectives that need the user: still blocked, waiting for a yes, or stalled with no activity.
    Titles stay fixed per objective+state so each reminder fires at most once a week."""
    from . import objectives
    now = now or time.time()
    try:
        con = objectives._db()
        rows = [dict(r) for r in con.execute(
            "SELECT o.id, o.title, o.status, o.updated, o.error, o.project, "
            "(SELECT MAX(time) FROM log l WHERE l.objective_id=o.id) AS last_log, "
            "(SELECT kind FROM log l WHERE l.objective_id=o.id ORDER BY time DESC LIMIT 1) AS last_kind "
            "FROM objectives o WHERE o.status IN ('blocked','failed','waiting_approval','active','planning')")]
        con.close()
    except Exception:
        return []
    out = []
    for o in rows:
        last = max(o["updated"] or 0, o["last_log"] or 0) or now
        hours = (now - last) / 3600
        name = (o["title"] or o["id"])[:60]
        base = {"source": "objective", "project": o["project"] or "", "confidence": 1.0, "verified": True}
        if o["status"] in ("blocked", "failed") and hours >= 12:
            out.append({**base, "tier": "important", "title": f"Objective '{name}' is still {o['status']}",
                        "detail": f"{(o['error'] or 'It needs you.')[:200]} Fix it and say 'resume objective "
                                  f"{name}', or say 'cancel objective {name}'."})
        elif o["status"] == "waiting_approval" and hours >= 3:
            out.append({**base, "tier": "important", "title": f"Objective '{name}' is waiting for your yes",
                        "detail": f"It paused {int(hours)} h ago for an approval (approvals expire after 24 h). "
                                  "Reply yes to let it continue."})
        elif o["status"] in ("active", "planning") and hours >= 2:
            waiting = o["last_kind"] == "waiting" or (o["error"] or "").startswith("waiting for budget")
            if waiting and hours < 12:
                continue  # parked until the Claude window resets: expected
            out.append({**base, "tier": "important", "title": f"Objective '{name}' has stalled",
                        "detail": f"No progress for {int(hours)} h" + (" (waiting for Claude budget)" if waiting else
                                  "; its worker may have stopped") + ". Check it with 'objective status'."})
    return out


# ---- compatibility --------------------------------------------------------------------------------------

def run(settings=None, now=None, **_):
    """Old entry point: one pass of the watchers and the decision step (see tick.py). Nothing is printed for cron:
    messages go out through the outbox."""
    from . import tick
    tick.look(settings=settings, now=now, steps=("watchers", "decide"))
    return []


def digest(settings=None, asker=None):
    """The 08:00/17:00 digest is gone: the reasoning step sends what matters when it matters and the rest goes into
    the evening note. Kept so the old cron entry stays silent."""
    return ""


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # emoji on a cp1252 Windows console
    except (AttributeError, ValueError):
        pass
    from . import budget
    budget.set_background("proactive")
    if len(sys.argv) > 1 and sys.argv[1] == "consolidate":
        from . import learn
        learn.consolidate()  # silent: the profile note changes, nothing is sent
        return
    from . import tick  # thin caller: the tick decides what is due
    tick.main([])


if __name__ == "__main__":
    main()
