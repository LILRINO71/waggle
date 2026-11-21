"""Repo and objective helpers for the initiative watchers, and the old proactive cron entry (now a thin caller).

The old pipeline (the local 4B model re-reading vault notes, a 4-a-day Sonnet check, and delivery by tier where only
"critical" reached the phone) is replaced by fact watchers (watchers.py) and a reasoning step (initiative.py),
driven by core timers (tick.register_core). What stays here: find_repos/_git (used by the watchers), git_watch, stale_watch and
objective_watch (objectives that are blocked, waiting for a yes, or stalled).
"""
import datetime as dt
import hashlib
import json
import os
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
                       encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.strip() if r.returncode == 0 else None


def _git_dirs(repo):
    """(git dir, common dir) of a checkout. In a worktree `.git` is a file that points at its own git dir, and the
    branches and the config live in the main repository's (its `commondir`)."""
    g = Path(repo) / ".git"
    try:
        if g.is_file():
            text = g.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                gd = Path(text[len("gitdir:"):].strip())
                if not gd.is_absolute():
                    gd = (Path(repo) / gd).resolve()
                common = gd
                c = gd / "commondir"
                if c.is_file():
                    rel = Path(c.read_text(encoding="utf-8", errors="replace").strip())
                    common = rel if rel.is_absolute() else (gd / rel).resolve()
                return gd, common
    except OSError:
        pass
    return g, g


def git_signature(repo):
    """A value that changes whenever a commit, a checkout, a reset, a fetch, a push or a change of upstream would
    change what git answers about the repository's refs. Read from a few file stats: no process is started.
    None when the repository can't be read (the caller then asks git as before)."""
    gd, common = _git_dirs(repo)
    parts = []
    try:
        for p in (gd / "HEAD", gd / "logs" / "HEAD", common / "packed-refs", common / "config", common / "FETCH_HEAD"):
            try:
                st = p.stat()
                parts.append((p.name, st.st_mtime_ns, st.st_size))
            except OSError:
                parts.append((p.name, 0, -1))
        if parts[0][2] < 0:                      # no HEAD: not a repository we can read
            return None
        for sub in ("heads", "remotes"):         # the branch tips and what was last seen of the remotes
            base = common / "refs" / sub
            for dirpath, _dirs, files in os.walk(base):
                for name in sorted(files):
                    try:
                        st = os.stat(os.path.join(dirpath, name))
                        parts.append((os.path.relpath(os.path.join(dirpath, name), common), st.st_mtime_ns, st.st_size))
                    except OSError:
                        pass
    except OSError:
        return None
    return hashlib.sha1(repr(parts).encode("utf-8")).hexdigest()


_GIT_REFS = {}            # (repo, args) -> (signature, answer)
_GIT_REFS_MAX = 2000


def _git_refs(repo, *args):
    """`_git` for a question whose answer depends only on the refs and the config (how many commits are not pushed,
    when the last commit was, the log since a fixed moment), never on the working tree. The look around asks these
    of every repository under the watch roots every five minutes: two processes a repository a pass, some sixty
    processes a pass on a machine with thirty checkouts. The answer is kept until the refs change, so a
    repository nobody touched costs a few file stats."""
    sig = git_signature(repo)
    key = (str(repo), args)
    hit = _GIT_REFS.get(key)
    if sig is not None and hit is not None and hit[0] == sig:
        return hit[1]
    out = _git(repo, *args)
    # keep it only if nothing moved while git was answering (a commit landing mid-call must not be remembered)
    if sig is not None and git_signature(repo) == sig:
        if len(_GIT_REFS) >= _GIT_REFS_MAX:
            _GIT_REFS.clear()
        _GIT_REFS[key] = (sig, out)
    return out


def unpushed_count(repo):
    """How many commits of the checked-out branch are on no remote at all, or None when that can't be said.
    Not `@{u}..HEAD`: a branch cut from origin/main has origin/main as its upstream, so every commit of it counted
    as "not pushed" even after it was pushed under its own name (four pushed branches were reported with 5 to 19
    commits each), while a branch with no upstream, the one that really is nowhere else, was never reported.
    A repository with no remote has nowhere to push to: None."""
    if not _git_refs(repo, "remote"):
        return None
    n = _git_refs(repo, "rev-list", "--count", "HEAD", "--not", "--remotes")
    return int(n) if n and n.isdigit() else None


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
        ahead = unpushed_count(repo)
        if ahead:
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
