"""Morning brief: Waggle reaches out first, every day, without being asked.

At 7:30 (cron, delivered to Telegram) it sends a short brief: today's weather, the next action on your most
active projects, builds that finished or are running, anything waiting for you, and one suggestion. The facts
are gathered for free; the local model only phrases them in Waggle's voice (plain list if it's down).
"""
import datetime as dt

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


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print(compose())


if __name__ == "__main__":
    main()
