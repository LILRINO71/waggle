"""Morning brief: Waggle reaches out first, every day, without being asked.

At brief_time (07:30; a core timer, outreach.brief, queues it in the outbox, and a brief missed while the PC was
off is still sent up to brief_catchup_hours later, never in the afternoon) it sends a short brief: today's weather, the
next action on your most active projects, today's dated items, builds that finished or are running, anything
waiting for you, and one suggestion. The facts are gathered for free; the local model only phrases them in
Waggle's voice (plain list if it's down).

evening_note() is the daily evening note (evening_time, always sent): what Waggle did today, what it learned,
what you worked on, what it kept for tonight, and what is open tomorrow.
"""
import datetime as dt
import time

from . import events, jobs, live, notify, ollama, projects
from .config import Settings

SYSTEM = """You are Waggle, a calm, sharp personal assistant writing the user's morning brief for their phone.
Use ONLY the facts given. 6 short lines max: a one-line greeting with the weather, then the most important
things for today, then one concrete suggestion for what to do first. No filler, no markdown headers."""


def facts(settings=None, now=None):
    s = settings or Settings.load()
    now = now or dt.datetime.now()
    out = []
    try:
        w = live.weather(None, "today")
        if w and "NO_LOCATION" not in w:
            out.append("Weather: " + " ".join(w.split())[:300])
    except Exception:  # noqa: BLE001
        pass
    nexts = []
    for p in projects.all_projects(s):
        if p.get("status") != "active":
            continue
        open_tasks = [t["text"] for t in projects.tasks(p["name"], s) if not t["done"]]
        if open_tasks:
            nexts.append((p.get("updated", ""), p["name"], open_tasks[0]))
    for _, name, task in sorted(nexts, reverse=True)[:3]:
        out.append(f"Next on {name}: {task}")
    for j in jobs.load().values():
        if j.get("status") in ("running", "queued"):
            out.append(f"Build still running: {j.get('title')}")
        elif j.get("status") == "done" and j.get("finished", 0) > now.timestamp() - 86400:
            out.append(f"Build finished overnight: {j.get('title')}")
    try:
        from . import watchers
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        ups = watchers.upcoming_items(s, day0, day0 + dt.timedelta(days=1))
        if ups:
            out.append("Dated items today: " + "; ".join(
                (f"{i['when']:%#I:%M %p} " if i["has_time"] else "") + i["text"][:80] for i in ups[:5]))
    except Exception:  # noqa: BLE001
        pass
    cal = calendar_today(s)
    if cal:
        out.append("Calendar today: " + cal)
    pending = notify.unseen(mark=False)
    if pending:
        out.append(f"{len(pending)} update(s) waiting: " + "; ".join(n["text"][:80] for n in pending[:3]))
    out.append(f"Today is {now:%A, %B %d}.")
    import re
    from . import vault
    m = re.search(r"\*\*Name:\*\*\s*(.+)", vault.read("Me/Profile", s))
    if m:
        out.insert(0, f"The user's name: {m.group(1).strip()} (greet them by name).")
    return out


def calendar_today(settings=None, asker=None):
    """Today's events. Once a calendar is set up (waggle.calendar_watch, the secret iCal address) they come from its
    store with no Claude call; otherwise from the Google Calendar connector (one Haiku call), or '' when neither."""
    from . import calendar_watch, claude_cli, quota
    s = settings or Settings.load()
    try:
        if calendar_watch.configured():
            return calendar_watch.today_line(s)
    except Exception:  # noqa: BLE001
        return ""
    try:
        if "Google_Calendar" not in claude_cli.available_connectors(()):
            return ""
        ok, _ = quota.can_escalate(s.router_model, s)
        if not ok:
            return ""
        res = (asker or claude_cli.ask)(
            f"List today's events ({dt.date.today():%A %B %d}) on my calendar as 'HH:MM title' separated by '; '. "
            "If there are none, reply exactly NONE.", model=s.router_model, purpose="brief", effort="low",
            connectors=["Google_Calendar"], timeout=90,
            system="You read the user's Google Calendar and reply with only the requested list.")
        text = " ".join((res.get("text") or "").split())
        return "" if not text or text.upper().startswith("NONE") else text[:400]
    except Exception:  # noqa: BLE001
        return ""


def compose(settings=None, chat=None):
    s = settings or Settings.load()
    f = facts(s)
    try:
        text = (chat or ollama.chat)([{"role": "system", "content": SYSTEM},
                                      {"role": "user", "content": "\n".join(f)}], settings=s).strip()
    except Exception:  # noqa: BLE001
        text = ""
    if not text:
        text = "Good morning.\n" + "\n".join("• " + x for x in f)
    events.log("brief", lines=len(f))
    return "🐝 " + text


def _learned(now):
    """learning.today_learned() when the knowledge side provides it (str or list); else []."""
    try:
        from . import learning
        fn = getattr(learning, "today_learned", None)
        got = fn() if fn else None
    except Exception:  # noqa: BLE001
        got = None
    if not got:
        return []
    if isinstance(got, str):
        return [x.strip(" -•") for x in got.splitlines() if x.strip()][:4]
    return [str(x.get("text") if isinstance(x, dict) else x)[:160] for x in list(got)[:4]]


def _nightly_review(s, now):
    """Today's entry of the nightly-project-review routine (vault log), if it ran."""
    from . import vault
    text = vault.read("Routines/nightly-project-review", s)
    day = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    parts = text.split("\n## ")
    for part in reversed(parts):
        if part.startswith(day):
            body = [x.strip() for x in part.splitlines()[1:] if x.strip()]
            return body[:3]
    return []


def evening_note(settings=None, now=None, activity=None):
    """Tonight's note: what Waggle did, what it learned, what the user worked on, what's open tomorrow.
    Always returns text (it is sent every day, even when little happened). Facts only, no model."""
    from . import followups, initiative, mailwatch, watchers
    s = settings or Settings.load()
    now = now or time.time()
    nowd = dt.datetime.fromtimestamp(now)
    start = nowd.replace(hour=0, minute=0, second=0, microsecond=0)
    lines = [f"🌙 Evening note, {nowd:%A %b %d}"]
    sent = [r for r in notify.rows(since=start.timestamp()) if r["status"] == "sent"
            and r["origin"] not in ("evening",) and r["kind"] in ("text", "voice", "call")]
    work = initiative.signals_between(start.timestamp(), now + 1)
    done = [w["title"] for w in work if w["kind"] in ("objective", "build", "routine") and w["status"] == "told"]
    did = []
    if done:
        did.append("; ".join(done[:4]))
    if sent:
        did.append(f"sent you {len(sent)} message(s)")
    lines.append("Did: " + ("; ".join(did) if did else "nothing beyond watching (no jobs ran, nothing worth a "
                                                           "message)") + ".")
    try:  # what the gates and the notify check did today (waggle/gates.py)
        from . import gates
        gate_line = gates.evening_line(now, s)
    except Exception:  # noqa: BLE001
        gate_line = ""
    if gate_line:
        lines.append(gate_line)
    learned = _learned(now)
    lines.append("Learned: " + ("; ".join(learned) if learned else "nothing new recorded today") + ".")
    try:  # the learning loop's changes, one line each with its undo code (waggle/learnloop.py)
        from . import learnloop
        fixes = learnloop.evening_lines(now, settings=s)
    except Exception:  # noqa: BLE001
        fixes = ""
    if fixes:
        lines.append(fixes)
    if activity is None:
        try:
            activity = watchers.repo_activity(s, start.timestamp(), now)
        except Exception:  # noqa: BLE001
            activity = {}
    if activity:
        lines.append("You worked on: " + "; ".join(f"{k} ({len(v)} commit{'s' if len(v) > 1 else ''})"
                                                    for k, v in list(activity.items())[:5]) + ".")
    review = _nightly_review(s, now)
    if review:
        lines.append("Project review: " + " ".join(review)[:300])
    mail = mailwatch.evening_line(now)  # today's mail the watch kept: texted ones and ones worth a look
    if mail:
        lines.append(mail)
    saved = initiative.evening_items(now, s)
    if saved:
        lines.append("Saved for tonight:\n" + "\n".join(f"• {x['title'][:140]}" for x in saved[:6]))
    tomorrow = (start + dt.timedelta(days=1))
    after = tomorrow + dt.timedelta(days=1)
    open_ = [f"check in: {i['what'][:80]}" for i in followups.upcoming(tomorrow.timestamp(), after.timestamp())]
    try:
        open_ += [f"{i['when']:%#I:%M %p} " * i["has_time"] + i["text"][:80]
                  for i in watchers.upcoming_items(s, tomorrow, after)]
    except Exception:  # noqa: BLE001
        pass
    lines.append("Tomorrow: " + ("; ".join(open_[:6]) if open_ else "nothing scheduled that I know of") + ".")
    initiative.close_evening([x["key"] for x in saved], now)
    return "\n".join(lines)


def main():
    """Thin caller kept for the old waggle-brief cron entry: the core timer outreach.brief decides when it is due."""
    from . import tick
    tick.main([])


if __name__ == "__main__":
    main()
