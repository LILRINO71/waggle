import json
import time

from waggle import events, hq, jobs, projects, vault
from waggle.config import Settings


def test_hq_dashboard_and_canvas():
    s = Settings.load()
    vault.init(s)
    projects.create("Robot", s, goal="win", next_actions=["Fix intake"])
    projects.create("Old Game", s, status="done")
    events.log("route", route="sonnet", latency_s=9.2, specialist="robotics", answered_by="sonnet")
    events.log("screen_look", seconds=1.4, how="windows")
    events.log("desktop", action="open_type", app="notepad", ok=True)
    jid = jobs.create("Build scouting app", "prompt", "req", s)
    jobs.update(jid, status="running")
    canvas = hq.write(s)

    md = vault.read("Waggle HQ", s)
    assert "cssclasses: [waggle-hq]" in md and "Build scouting app" in md and "[!hq-live]" in md
    assert "[!hq-card|active] [[Robot]]" in md and "Next: Fix intake" in md and "[!hq-card|done] [[Old Game]]" in md
    assert "Answered by Sonnet · robotics" in md and "Looked at your screen (windows" in md and "Opened and typed in notepad" in md

    c = json.loads(canvas.read_text(encoding="utf-8"))
    ids = {n["id"] for n in c["nodes"]}
    assert {"waggle", "me", "feed", "job0", "p0", "p1"} <= ids
    assert all(e["fromNode"] in ids and e["toNode"] in ids for e in c["edges"])


def test_ago():
    now = time.time()
    assert hq.ago(now - 5, now) == "just now" and hq.ago(now - 300, now) == "5 min ago"
    assert hq.ago(now - 7200, now) == "2 h ago" and hq.ago(now - 3 * 86400, now) == "3 d ago"
