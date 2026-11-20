"""Automation scout: notice work the user repeats by hand and offer (or build) the automation.

daily() looks, at most once a day, for
  - repeated requests: the same kind of thing asked on 3+ different days in the last 3 weeks (Telegram turns);
  - repeated manual commits: the same commit message (numbers stripped) on 3+ days across the user's repos;
and turns the strongest candidate nobody has proposed before into ONE signal of kind "automation". The decision
step (initiative.py) decides whether it is worth doing; if it answers "automate", build() hands the goal to the
objectives engine (which plans it, applies the safety policy and asks for approvals as usual) and the user is told
what was set up and how to turn it off. Nothing is built without that decision, at most one a day.
"""
import datetime as dt
import hashlib
import re
import time

from .config import Settings

STOP = set("a an the to of and or for on in at is it my me i you can could would please what how do does with "
           "this that be are was get give tell show about again".split())


STATE_META = "outreach.scout"  # candidates already proposed and the last day it looked, in runtime.db meta


def _state():
    from waggle.core import store
    st = store.loads(store.get_meta(STATE_META))
    return st if isinstance(st, dict) else {"proposed": {}, "last_day": ""}


def _save(st):
    from waggle.core import store
    store.set_meta(STATE_META, store.dumps(st))


def _words(text):
    return {w for w in re.findall(r"[a-z][a-z0-9']+", (text or "").lower()) if w not in STOP and len(w) > 2}


def repeated_requests(rows, min_days=3, min_sim=0.5):
    """Clusters of similar requests asked on min_days+ different days. rows: traces dicts."""
    items = [(r, _words(r.get("request"))) for r in rows if len(_words(r.get("request"))) >= 2]
    used, out = set(), []
    for i, (r, w) in enumerate(items):
        if i in used:
            continue
        group = [i]
        for j in range(i + 1, len(items)):
            if j in used:
                continue
            w2 = items[j][1]
            if len(w & w2) / max(1, len(w | w2)) >= min_sim:
                group.append(j)
        days = {dt.date.fromtimestamp(items[k][0]["time"]) for k in group}
        if len(days) >= min_days:
            used.update(group)
            sample = [items[k][0]["request"][:120] for k in group[:3]]
            out.append({"what": sample[0], "examples": sample, "days": len(days), "count": len(group),
                        "source": "requests"})
    return sorted(out, key=lambda c: (c["days"], c["count"]), reverse=True)


def repeated_commits(log_by_day, min_days=3):
    """log_by_day: {date: [(repo, subject)]}. Same normalized subject on min_days+ days."""
    seen = {}
    for day, entries in log_by_day.items():
        for repo, subj in entries:
            norm = re.sub(r"\d+", "#", subj.lower()).strip()
            seen.setdefault((repo, norm), set()).add(day)
    out = [{"what": f"commit '{norm}' in {repo}", "examples": [norm], "days": len(days), "count": len(days),
            "source": "commits", "repo": repo} for (repo, norm), days in seen.items() if len(days) >= min_days]
    return sorted(out, key=lambda c: c["days"], reverse=True)


def _commit_log(settings, now, days=14):
    from . import proactive
    since = int(now - days * 86400)
    by_day = {}
    for repo in proactive.find_repos(settings):
        log = proactive._git(repo, "log", f"--since=@{since}", "--no-merges", "--format=%ct|%s", "-n", "200")
        for line in (log or "").splitlines():
            ts, _, subj = line.partition("|")
            if ts.isdigit():
                by_day.setdefault(dt.date.fromtimestamp(int(ts)), []).append((repo.name, subj[:100]))
    return by_day


def daily(settings=None, now=None, rows=None, commits=None):
    """At most one new automation candidate a day, as a signal (or [])."""
    from .watchers import signal
    s = settings or Settings.load()
    now = now or time.time()
    if not s.scout_enabled:
        return []
    st = _state()
    today = dt.date.fromtimestamp(now).isoformat()
    if st.get("last_day") == today:
        return []
    st["last_day"] = today
    from . import origin
    if rows is None:
        try:
            from . import traces
            rows = traces.all_traces(since=now - 21 * 86400, limit=2000)
        except Exception:  # noqa: BLE001
            rows = []
    rows = origin.user_turns(rows, s)  # decision 10: only the user's own turns
    if commits is None:
        try:
            commits = _commit_log(s, now)
        except Exception:  # noqa: BLE001
            commits = {}
    cands = repeated_requests(rows) + repeated_commits(commits)
    for c in cands:
        h = hashlib.sha1(c["what"].lower().encode()).hexdigest()[:10]
        if h in st["proposed"]:
            continue
        st["proposed"][h] = now
        _save(st)
        if c["source"] == "requests":
            title = f"You asked something like \"{c['what'][:80]}\" on {c['days']} different days"
            goal = (f"The user keeps asking Waggle for this by hand ({c['count']} times on {c['days']} days), e.g. "
                    + " | ".join(c["examples"]) + ". Set up a Waggle routine (routine_add) that does it for them on a "
                    "sensible schedule and delivers to Telegram. Keep it read-only and cheap.")
        else:
            title = f"The same manual commit ('{c['examples'][0][:60]}') shows up on {c['days']} days"
            goal = (f"In repo {c.get('repo')} the user makes the commit '{c['examples'][0]}' by hand on many days. Find "
                    "what produces it and propose or set up a script or routine that does it, without pushing or "
                    "deleting anything.")
        return [signal(f"automation:{h}", "automation", title, goal, f"automation:{h}", when=now)]
    _save(st)
    return []


def build(sig, message, settings=None):
    """The decision step said 'automate': start it as an objective (planned, policy-checked and approval-gated by
    the objectives engine). Returns {"note", "message"}."""
    from . import objectives
    goal = sig.get("detail") or sig["title"]
    title = ("Automate: " + sig["title"])[:80]
    try:
        oid = objectives.create(goal, title=title, origin="scout", plan_now=False, settings=settings)
    except Exception as e:  # noqa: BLE001
        return {"note": f"objective not created: {e}"[:200], "message": ""}
    text = (message or f"I noticed: {sig['title']}. I'm setting up an automation for it.") + \
        f" To stop it, say 'cancel objective {oid}'."
    return {"note": f"objective {oid} created", "message": text, "objective": oid}
