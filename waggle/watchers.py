"""Fact watchers: plain "this changed" signals from real sources. No model is involved.

Each watcher compares what it sees now with what it saw last time (~/.waggle/watchers_state.json) and returns new
signals only. A signal is a dict:
    key      stable id (the same fact never becomes two signals)
    kind     github_ci | github_pr | github_issue | objective | build | routine | cron | upcoming | mail | unpushed
    title    one line, detail: a little more
    topic    what it is about (dedupe across signals: the same topic is not raised twice in a short time)
    urgent   only for facts that are urgent by themselves (a dated item starting within 3 hours)
    told     True when another part of Waggle already told the user (objective/build/routine reports): the
             signal still counts for the evening note but is never sent again
    when     timestamp of the fact; url, project when known

Watchers: GitHub through the `gh` CLI (CI runs failed/fixed, new and merged PRs, new issues) for the user's
recently pushed repos; Waggle's objectives, builds and routines; failing Hermes cron jobs; dated items and mail to
answer from the vault's Upcoming note (maintained by the knowledge side; absent = nothing); unpushed commits.
repo_activity() says what the user worked on today (commits across watch_roots + extra_repos).
"""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import Settings, data_path

GH_CANDIDATES = (r"C:\Program Files\GitHub CLI\gh.exe",)
URGENT_WITHIN_S = 3 * 3600
LOOKAHEAD_DAYS = 2


def _state():
    try:
        return json.loads(data_path("watchers_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(st):
    data_path("watchers_state.json").write_text(json.dumps(st, indent=1), encoding="utf-8")


def _h(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def signal(key, kind, title, detail="", topic=None, urgent=False, told=False, when=None, url="", project=""):
    return {"key": key, "kind": kind, "title": title[:200], "detail": (detail or "")[:600], "topic": topic or key,
            "urgent": bool(urgent), "told": bool(told), "when": when or time.time(), "url": url, "project": project}


# ---- GitHub -----------------------------------------------------------------------------------------------

def gh_path():
    found = shutil.which("gh")
    if found:
        return found
    return next((p for p in GH_CANDIDATES if Path(p).exists()), None)


def run_gh(args, timeout=40):
    """JSON from a read-only gh command, or None."""
    exe = gh_path()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                           errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "null")
    except ValueError:
        return None


def github_repos(settings, gh=None, now=None):
    """The repos to watch: settings.github_repos, else the user's 10 most recently pushed (last 21 days)."""
    if settings.github_repos:
        return list(settings.github_repos)
    now = now or time.time()
    st = _state()
    cache = st.get("gh_repos") or {}
    if cache.get("list") and now - cache.get("time", 0) < 6 * 3600:
        return cache["list"]
    data = (gh or run_gh)(["repo", "list", "--limit", "40", "--json", "nameWithOwner,pushedAt,isArchived"])
    if not isinstance(data, list):
        return cache.get("list") or []
    cutoff = now - 21 * 86400
    fresh = []
    for r in data:
        try:
            pushed = dt.datetime.fromisoformat(r["pushedAt"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, AttributeError):
            continue
        if not r.get("isArchived") and pushed >= cutoff:
            fresh.append((pushed, r["nameWithOwner"]))
    repos = [n for _, n in sorted(fresh, reverse=True)[:10]]
    st = _state()
    st["gh_repos"] = {"time": now, "list": repos}
    _save(st)
    return repos


def github_watch(settings, gh=None, now=None):
    """CI runs that failed or got fixed, PRs opened/merged, new issues. The first look at a repo only records a
    baseline (plus a still-failing default branch from the last day), so turning this on never floods the phone."""
    gh = gh or run_gh
    now = now or time.time()
    out = []
    st = _state()
    seen = st.setdefault("github", {})
    for repo in github_repos(settings, gh, now):
        short = repo.split("/")[-1]
        rs = seen.get(repo)
        first = rs is None
        rs = rs or {"runs": {}, "prs": {}, "issues": []}
        runs = gh(["run", "list", "-R", repo, "-L", "10", "--json",
                   "databaseId,workflowName,headBranch,status,conclusion,url,createdAt"])
        if isinstance(runs, list):
            latest = {}
            for r in runs:  # newest first: the first completed run per workflow+branch is its current state
                if r.get("status") != "completed":
                    continue
                k = f"{r.get('workflowName')}|{r.get('headBranch')}"
                latest.setdefault(k, r)
            for k, r in latest.items():
                prev = rs["runs"].get(k)
                conc = r.get("conclusion") or ""
                bad = conc in ("failure", "timed_out", "startup_failure")
                wf, branch = k.split("|", 1)
                topic = f"ci:{repo}:{wf}:{branch}"
                if first:
                    try:
                        recent = dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")).timestamp() > now - 86400
                    except (KeyError, ValueError, AttributeError):
                        recent = False
                    if bad and recent and branch in ("main", "master"):
                        out.append(signal(f"gh-run:{r.get('databaseId')}", "github_ci",
                                          f"{short}: '{wf}' is failing on {branch}", f"Latest run {conc}.",
                                          topic, url=r.get("url", "")))
                elif prev and prev.get("id") != r.get("databaseId"):
                    was_bad = prev.get("conclusion") in ("failure", "timed_out", "startup_failure")
                    if bad and not was_bad:
                        out.append(signal(f"gh-run:{r.get('databaseId')}", "github_ci",
                                          f"{short}: '{wf}' failed on {branch}", f"The run ended {conc}.", topic,
                                          url=r.get("url", "")))
                    elif was_bad and conc == "success":
                        out.append(signal(f"gh-run:{r.get('databaseId')}", "github_ci",
                                          f"{short}: '{wf}' is green again on {branch}", "The last failure is fixed.",
                                          topic, url=r.get("url", "")))
                elif prev is None and bad:
                    out.append(signal(f"gh-run:{r.get('databaseId')}", "github_ci", f"{short}: '{wf}' failed on "
                                      f"{branch}", f"The run ended {conc}.", topic, url=r.get("url", "")))
                rs["runs"][k] = {"id": r.get("databaseId"), "conclusion": conc}
        prs = gh(["pr", "list", "-R", repo, "--state", "all", "-L", "10", "--json",
                  "number,title,state,author,url,mergedAt"])
        if isinstance(prs, list):
            for p in prs:
                num, state = str(p.get("number")), p.get("state")
                prev = rs["prs"].get(num)
                who = (p.get("author") or {}).get("login", "")
                if not first and prev is None and state == "OPEN":
                    out.append(signal(f"gh-pr:{repo}#{num}:open", "github_pr", f"{short}: new PR #{num} "
                                      f"'{p.get('title', '')[:80]}'", f"Opened by {who}.", f"pr:{repo}#{num}",
                                      url=p.get("url", "")))
                elif not first and prev == "OPEN" and state == "MERGED":
                    out.append(signal(f"gh-pr:{repo}#{num}:merged", "github_pr", f"{short}: PR #{num} merged",
                                      p.get("title", "")[:120], f"pr:{repo}#{num}", url=p.get("url", "")))
                rs["prs"][num] = state
        issues = gh(["issue", "list", "-R", repo, "--state", "open", "-L", "10", "--json", "number,title,author,url"])
        if isinstance(issues, list):
            known = set(rs["issues"])
            for i in issues:
                num = i.get("number")
                if not first and num not in known:
                    who = (i.get("author") or {}).get("login", "")
                    out.append(signal(f"gh-issue:{repo}#{num}", "github_issue",
                                      f"{short}: new issue #{num} '{i.get('title', '')[:80]}'", f"Opened by {who}.",
                                      f"issue:{repo}#{num}", url=i.get("url", "")))
                known.add(num)
            rs["issues"] = sorted(known)[-200:]
        seen[repo] = rs
    st2 = _state()
    st2["github"] = seen
    _save(st2)
    return out


# ---- Waggle's own work -------------------------------------------------------------------------------------

def work_watch(settings, now=None):
    """Objectives, builds and routines that finished or failed since the last look (already reported by their own
    code, so told=True: they feed the evening note), plus objectives that need the user (not told yet)."""
    now = now or time.time()
    st = _state()
    known = st.setdefault("work", {})
    out = []
    first = not known
    try:
        from . import objectives
        con = objectives._db()
        rows = [dict(r) for r in con.execute("SELECT id, title, status, updated, origin FROM objectives")]
        con.close()
    except Exception:  # noqa: BLE001
        rows = []
    for o in rows:
        k = f"objective:{o['id']}"
        if known.get(k) != o["status"] and o["status"] in ("done", "failed", "cancelled") and not first:
            out.append(signal(f"{k}:{o['status']}", "objective", f"Objective '{(o['title'] or '')[:70]}' "
                              f"{o['status']}", "", k, told=True, when=o["updated"]))
        known[k] = o["status"]
    try:
        from . import jobs
        js = jobs.load()
    except Exception:  # noqa: BLE001
        js = {}
    for jid, j in js.items():
        k = f"build:{jid}"
        if known.get(k) != j.get("status") and j.get("status") in ("done", "failed") and not first:
            out.append(signal(f"{k}:{j.get('status')}", "build", f"Build '{(j.get('title') or '')[:70]}' "
                              f"{j.get('status')}", "", k, told=True, when=j.get("finished")))
        known[k] = j.get("status")
    try:
        from . import routines
        rts = routines._load()
    except Exception:  # noqa: BLE001
        rts = {}
    for rid, r in rts.items():
        k = f"routine:{rid}"
        runs = r.get("runs", 0)
        if known.get(k) is not None and runs > known[k]:
            out.append(signal(f"{k}:{runs}", "routine", f"Routine '{r.get('name')}' ran: {r.get('last_status')}",
                              "", k, told=True, when=r.get("last_run")))
        known[k] = runs
    try:
        from . import proactive
        for f in proactive.objective_watch(now):  # blocked / waiting for a yes / stalled: these need the user
            out.append(signal(f"needs:{_h(f['title'])}:{dt.date.fromtimestamp(now)}", "objective", f["title"],
                              f["detail"], f"needs:{_h(f['title'])}"))
    except Exception:  # noqa: BLE001
        pass
    st = _state()
    st["work"] = known
    _save(st)
    return out


def cron_watch(settings, now=None, jobs_file=None):
    """Hermes cron jobs whose last run failed (they report failures only to a local log today)."""
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    p = Path(jobs_file or home / "cron" / "jobs.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    st = _state()
    seen = st.setdefault("cron", {})
    out = []
    for j in jobs if isinstance(jobs, list) else []:
        name, status, ran = j.get("name") or j.get("id"), j.get("last_status"), j.get("last_run_at")
        if not j.get("enabled", True) or not ran:
            continue
        if status not in (None, "ok", "silent", "suppressed", "skipped") and seen.get(name) != ran:
            why = j.get("last_delivery_error") or j.get("last_error") or status
            out.append(signal(f"cron:{name}:{ran}", "cron", f"Scheduled job '{name}' failed ({status})",
                              str(why)[:300], f"cron:{name}"))
        seen[name] = ran
    st["cron"] = seen
    _save(st)
    return out


def unpushed_watch(settings, now=None):
    """Commits sitting unpushed for 2+ hours (the user's rule: push after every major change)."""
    from . import proactive
    now = now or time.time()
    out = []
    for repo in proactive.find_repos(settings):
        ahead = proactive._git(repo, "rev-list", "--count", "@{u}..HEAD")
        last = proactive._git(repo, "log", "-1", "--format=%ct")
        if ahead and ahead.isdigit() and int(ahead) > 0 and last and last.isdigit() and now - int(last) > 7200:
            out.append(signal(f"unpushed:{repo.name}:{last}", "unpushed", f"{repo.name}: {ahead} commit(s) not pushed",
                              f"Last commit {int((now - int(last)) / 3600)} h ago.", f"unpushed:{repo.name}"))
    return out


# ---- Upcoming note (dates, mail to answer) -----------------------------------------------------------------

DATE_LINE = re.compile(r"(20\d\d-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?")
DEADLINE = re.compile(r"\b(due|deadline|test|exam|quiz|meet|competition|qualifier|submit|application|interview)\b",
                      re.I)


def upcoming_note(settings):
    from . import vault
    names = [settings.upcoming_note] if settings.upcoming_note else ["Upcoming", "Me/Upcoming", "Inbox/Upcoming"]
    for n in names:
        try:
            text = vault.read(n, settings)
        except Exception:  # noqa: BLE001
            text = ""
        if text and text.strip():
            return text
    return ""


def parse_upcoming(text):
    """[{"when": datetime, "has_time", "text", "mail": bool, "done": bool}] from the note's list lines. Lines under a
    heading that mentions mail/reply/email, or that say 'reply', are mail to answer."""
    items, section = [], ""
    for line in (text or "").splitlines():
        if line.lstrip().startswith("#"):
            section = line.lower()
            continue
        if not re.match(r"\s*[-*]\s", line):
            continue
        done = bool(re.match(r"\s*[-*]\s+\[[xX]\]", line))
        body = re.sub(r"^\s*[-*]\s+(\[[ xX]\]\s*)?", "", line).strip()
        mail = any(w in section for w in ("mail", "reply", "inbox")) or bool(re.search(r"\breply\b", body, re.I))
        m = DATE_LINE.search(body)
        when, has_time = None, False
        if m:
            try:
                when = dt.datetime.fromisoformat(m.group(1) + (f" {int(m.group(2).split(':')[0]):02d}:"
                                                               f"{m.group(2).split(':')[1]}" if m.group(2) else ""))
                has_time = bool(m.group(2))
            except ValueError:
                when = None
        clean = DATE_LINE.sub("", body).strip(" :-–") if m else body
        items.append({"when": when, "has_time": has_time, "text": clean[:200], "mail": mail, "done": done})
    return items


def upcoming_watch(settings, now=None, text=None):
    """Dated items coming up (2 days out, the day before, the day of, and 3 hours before a timed one) and mail
    that needs a reply. A timed item within 3 hours is urgent."""
    now = now or time.time()
    nowd = dt.datetime.fromtimestamp(now)
    text = upcoming_note(settings) if text is None else text
    out = []
    for it in parse_upcoming(text):
        if it["done"]:
            continue
        hid = _h(it["text"] + str(it["when"]))
        if it["mail"] and not it["when"]:
            out.append(signal(f"mail:{hid}", "mail", f"Mail to answer: {it['text'][:120]}", "", f"mail:{hid}"))
            continue
        if not it["when"]:
            continue
        start = it["when"]
        days = (start.date() - nowd.date()).days
        secs = (start - nowd).total_seconds()
        if days < 0 or (it["has_time"] and secs < -1800) or days > LOOKAHEAD_DAYS:
            continue
        label = start.strftime("%a %b %d") + (start.strftime(" %H:%M") if it["has_time"] else "")
        if it["has_time"] and 0 <= secs <= URGENT_WITHIN_S:
            stage, urgent = "soon", True
        else:
            stage, urgent = ("today" if days == 0 else "tomorrow" if days == 1 else "in2d"), False
        kind = "mail" if it["mail"] else "upcoming"
        out.append(signal(f"{kind}:{hid}:{stage}", kind, f"{it['text'][:120]} ({label})",
                          "Deadline." if DEADLINE.search(it["text"]) else "", f"{kind}:{hid}:{stage}", urgent=urgent,
                          when=start.timestamp()))
    return out


def upcoming_items(settings, start, end, text=None):
    """Dated items between two datetimes, for the brief and the evening note."""
    text = upcoming_note(settings) if text is None else text
    return [i for i in parse_upcoming(text) if i["when"] and not i["done"] and start <= i["when"] < end]


# ---- what the user worked on --------------------------------------------------------------------------------

def repo_activity(settings, since=None, now=None):
    """{repo: [commit subjects]} for commits since `since` (default: today 00:00) across watch_roots + extra_repos."""
    from . import proactive
    now = now or time.time()
    since = since or dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0).timestamp()
    out = {}
    for repo in proactive.find_repos(settings):
        log = proactive._git(repo, "log", f"--since=@{int(since)}", "--no-merges", "--format=%s", "-n", "30")
        if log:
            out[repo.name] = [line[:100] for line in log.splitlines() if line.strip()]
    return out
