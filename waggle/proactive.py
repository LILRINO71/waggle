"""The proactive loop: Waggle notices things and only bothers you when it's worth it.

Pipeline per run (hourly from Hermes cron, no LLM in Hermes itself):
  1. Free watchers gather candidates: unpushed/uncommitted git work, stale projects, failed builds.
  2. The local model reviews one project per run (round robin) and proposes at most two insights,
     each with a confidence and a tier.
  3. Anything "important" or "critical" is checked by Sonnet (budget-gated, a few per day) before
     it is believed. Unverified or low-confidence findings are only logged.
  4. Delivery by tier:
       critical    -> phone now (even in quiet hours)
       important   -> mentioned the next time you talk to Waggle
       interesting -> daily digest
       low         -> logged, never shown
Every finding is deduplicated for a week and logged as an event for the benchmark.
"""
import datetime as dt
import hashlib
import json
import subprocess
import time
from pathlib import Path

from . import claude_cli, events, notify, ollama, projects, quota, vault
from .config import Settings, data_path

TIERS = ["low", "interesting", "important", "critical"]


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


# ---- local review + Sonnet verification -----------------------------------------------------------

REVIEW_SYSTEM = """You review one of the user's projects to find things worth telling them proactively:
risks, contradictions between decisions and plans, forgotten next actions, or a concrete next step that would
unblock progress. Be specific to THIS project; never generic advice. Most of the time there is nothing.
Return JSON: {"insights": [{"title": "one line", "detail": "2-3 sentences with the evidence",
"tier": "low|interesting|important|critical", "confidence": 0.0-1.0}]} with at most 2 insights."""

VERIFY_SYSTEM = """A small local model flagged something about the user's project. Check it against the evidence.
Reject it if it is generic, wrong, already handled in the notes, or not worth interrupting a busy student for.
Return JSON {"verified": bool, "tier": "low|interesting|important|critical", "message": "what to tell the user,
1-3 plain sentences, specific and actionable"}."""
VERIFY_SCHEMA = {"type": "object", "properties": {
    "verified": {"type": "boolean"}, "tier": {"type": "string", "enum": TIERS}, "message": {"type": "string"}},
    "required": ["verified", "tier", "message"]}


def local_review(project, settings, chat=None):
    chat = chat or ollama.chat_json
    note = vault.read(f"Projects/{project}", settings)[:6000]
    try:
        out = chat([{"role": "system", "content": REVIEW_SYSTEM}, {"role": "user", "content": note}],
                   settings=settings, timeout=120)
    except Exception:
        return []
    found = []
    for i in (out.get("insights") or [])[:2] if isinstance(out, dict) else []:
        if not isinstance(i, dict) or not i.get("title"):
            continue
        tier = i.get("tier") if i.get("tier") in TIERS else "low"
        try:
            conf = max(0.0, min(1.0, float(i.get("confidence", 0))))
        except (TypeError, ValueError):
            conf = 0.0
        found.append({"source": "review", "project": project, "tier": tier, "confidence": conf, "verified": False,
                      "title": str(i["title"])[:200], "detail": str(i.get("detail", ""))[:800]})
    return found


def verify(finding, settings, asker=None):
    """Sonnet second opinion. Returns the finding updated, or None if rejected/unaffordable."""
    st = _state()
    day = f"{dt.date.today():%Y-%m-%d}"
    used = st.get("verifications", {}).get(day, 0)
    if used >= settings.max_verifications_per_day:
        return None
    ok, _ = quota.can_escalate(settings.deep_model, settings)
    if not ok:
        return None
    asker = asker or claude_cli.ask
    note = vault.read(f"Projects/{finding['project']}", settings)[:6000] if finding.get("project") else ""
    prompt = f"Project notes:\n{note}\n\nFlagged:\n{finding['title']}\n{finding['detail']}"
    try:
        res = asker(prompt, model=settings.deep_model, system=VERIFY_SYSTEM, schema=VERIFY_SCHEMA,
                    purpose="verify-finding")
    except claude_cli.ClaudeError:
        return None
    finally:
        st = _state()
        st.setdefault("verifications", {})[day] = used + 1
        _save(st)
    v = res.get("json") or {}
    if not v.get("verified"):
        return None
    return {**finding, "verified": True, "tier": v.get("tier", finding["tier"]),
            "detail": v.get("message") or finding["detail"]}


# ---- run ------------------------------------------------------------------------------------------

def run(settings=None, now=None, chat=None, asker=None, deliver_phone=None):
    """One pass. Returns the lines to send to the phone right now (critical only)."""
    s = settings or Settings.load()
    now = now or time.time()
    st = _state()
    week = 7 * 86400
    st["seen"] = {k: v for k, v in st.get("seen", {}).items() if now - v < week}

    candidates = git_watch(s, now) + stale_watch(s) + objective_watch(now)
    active = [p["name"] for p in projects.all_projects(s) if p["status"] == "active"]
    if active:
        project = active[st.get("rr", 0) % len(active)]
        st["rr"] = st.get("rr", 0) + 1
        candidates += local_review(project, s, chat)
    _save(st)

    phone_now = []
    for f in candidates:
        fp = fingerprint(f)
        if fp in _state().get("seen", {}):
            continue
        if not f["verified"]:
            if f["confidence"] < 0.6 or TIERS.index(f["tier"]) < TIERS.index("interesting"):
                events.log("finding", fp=fp, delivered=False, tier=f["tier"], reason="low confidence",
                           title=f["title"], project=f.get("project"))
                _mark(fp, now)
                continue
            if TIERS.index(f["tier"]) >= TIERS.index("important"):
                checked = verify(f, s, asker)
                if not checked:
                    events.log("finding", fp=fp, delivered=False, tier=f["tier"], reason="not verified",
                               title=f["title"], project=f.get("project"))
                    _mark(fp, now)
                    continue
                f = checked
        f["id"] = fp
        _deliver(f, s, phone_now)
        _mark(fp, now)
    return phone_now


def _mark(fp, now):
    st = _state()
    st.setdefault("seen", {})[fp] = now
    _save(st)


def _deliver(f, s, phone_now):
    text = f"{f['title']}. {f['detail']} (id {f['id']})"
    tier = f["tier"]
    if tier == "critical":
        phone_now.append("🚨 " + text)
        notify.send(text, urgent=True, settings=s, phone=False)  # the cron run itself delivers to the phone
        from . import call
        if f.get("verified") and call.configured() and not notify.in_quiet_hours(s):
            call.call(f"{f['title']}. {f['detail']}", reason="critical", settings=s)
    elif tier == "important":
        notify.send(text, urgent=False, settings=s, phone=False)  # mentioned on your next message
    elif tier == "interesting":
        vault.append("Ideas/Digest", f"- [ ] {dt.date.today():%Y-%m-%d} {text}", s)
        st = _state()
        st.setdefault("digest", []).append({"id": f["id"], "text": text, "source": f.get("source"),
                                            "verified": f.get("verified", False)})
        _save(st)
    events.log("finding", fp=f["id"], delivered=tier != "low", tier=tier, title=f["title"],
               project=f.get("project"), source=f.get("source"))


FILTER_SYSTEM = """You filter a personal assistant's digest for a busy student. Keep only items that are specific
to their project and actionable; drop generic advice ("may need tuning", "consider testing") and duplicates.
Return JSON {"keep": [ids]}."""
FILTER_SCHEMA = {"type": "object", "properties": {"keep": {"type": "array", "items": {"type": "string"}}},
                 "required": ["keep"]}


def digest(settings=None, asker=None):
    """Unsent digest items as one message (empty = nothing to send).
    Unverified AI suggestions get one Sonnet filter call for the whole batch; factual items pass."""
    s = settings or Settings.load()
    st = _state()
    items = st.get("digest", [])
    st["digest"] = []
    _save(st)
    text = vault.read("Ideas/Digest", s)
    if "- [ ] " in text:
        vault.write("Ideas/Digest", text.replace("- [ ] ", "- [x] "), s)
    unverified = [i for i in items if not i.get("verified")]
    if unverified:
        keep = set()
        ok, _ = quota.can_escalate(s.router_model, s)
        if ok:
            try:
                res = (asker or claude_cli.ask)(
                    "\n".join(f"{i['id']}: {i['text']}" for i in unverified), model=s.router_model,
                    system=FILTER_SYSTEM, schema=FILTER_SCHEMA, purpose="digest-filter")
                keep = set((res.get("json") or {}).get("keep", []))
            except claude_cli.ClaudeError:
                keep = set()
        for i in unverified:
            if i["id"] not in keep:
                events.log("finding", fp=i["id"], delivered=False, tier="interesting", reason="digest filter",
                           title=i["text"][:200])
        items = [i for i in items if i.get("verified") or i["id"] in keep]
    if not items:
        return ""
    body = "\n".join("• " + i["text"] for i in items[:12])
    return f"🐝 Waggle digest ({len(items)} item(s))\n{body}\nReply 'useful <id>' or 'not useful <id>'."


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # emoji on a cp1252 Windows console
    except (AttributeError, ValueError):
        pass
    if len(sys.argv) > 1 and sys.argv[1] == "consolidate":
        from . import learn
        learn.consolidate()  # silent: the profile note changes, nothing is sent
        out = ""
    elif len(sys.argv) > 1 and sys.argv[1] == "digest":
        out = digest()
    else:
        out = "\n".join(run())
    if out:
        print(out)


if __name__ == "__main__":
    main()
