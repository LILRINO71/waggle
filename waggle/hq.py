"""Waggle HQ: a live, visual home screen inside Obsidian.

Regenerated after every message and every minute (cron), with no model calls:
  - "Waggle HQ.md"      dashboard: status pills, what's running now, live activity feed, today's numbers,
                        project cards with their next action, what Waggle learned about you, ideas.
  - "Waggle HQ.canvas"  a visual map: Waggle in the middle, live jobs + recent activity around it, every
                        project as a colour-coded card linked to its note, and your profile.
Styling comes from .obsidian/snippets/waggle.css (installed by install.py).
"""
import datetime as dt
import json
import math
import time

from . import events, jobs, projects, quota, vault
from .config import Settings, data_path

ICON = {"route": "💬", "screen_look": "🖥️", "desktop": "🖱️", "call": "📞", "job": "🔨", "selfbuild": "🛠️",
        "web_search": "🔎", "notify": "🔔", "finding": "💡", "review": "🧐", "project_update": "📁",
        "feedback": "👍", "learn": "🧠", "ingest": "📥"}
STATUS_COLOR = {"active": "4", "planned": "6", "paused": "3", "done": "5"}  # canvas colours
JOB_COLOR = {"running": "4", "queued": "3", "paused": "2", "done": "5", "failed": "1"}


def ago(t, now=None):
    s = int((now or time.time()) - t)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def describe(e):
    k = e.get("kind")
    if k == "route":
        if e.get("route") == "act":
            return f"Did it with tools ({e.get('specialist') or 'agent'}) · routed in {e.get('latency_s', 0):.0f} s"
        who = {"local": "the local model", "sonnet": "Claude", "opus": "an Opus build"}.get(e.get("route"),
                                                                                          e.get("route"))
        if e.get("route") == "sonnet" and e.get("answered_by"):
            who = str(e["answered_by"]).capitalize()
        spec = f" · {e['specialist']}" if e.get("specialist") not in (None, "general") else ""
        return f"Answered by {who}{spec} in {e.get('latency_s', 0):.0f} s"
    if k == "screen_look":
        return f"Looked at your screen ({e.get('how', 'vision')}, {e.get('seconds', 0):.0f} s)"
    if k == "desktop":
        return f"{'Opened' if e.get('action') == 'open' else 'Opened and typed in'} {e.get('app', 'an app')}" + \
            ("" if e.get("ok", True) else " (failed)")
    if k == "call":
        return "Called your phone" if e.get("ok") else f"Call not placed: {e.get('why', '')}"
    if k == "job":
        return f"Build {e.get('status')}: {e.get('title') or e.get('job', '')}"
    if k == "selfbuild":
        return ("Started building a new tool: " if e.get("started") else "Skipped a tool build: ") + \
            (e.get("request") or "")[:60]
    if k == "web_search":
        return f"Searched the web: {(e.get('query') or '')[:60]}"
    if k == "notify":
        return f"Notified you: {(e.get('text') or '')[:70]}"
    if k == "finding":
        return f"Idea ({e.get('tier')}): {(e.get('title') or '')[:70]}"
    if k == "review":
        return "Second opinion on an answer" + (" (fixed it)" if e.get("ran") and not e.get("ok") else "")
    if k == "project_update":
        return f"{'Added' if e.get('action') == 'create' else 'Updated'} project {e.get('project')}"
    if k == "learn":
        what = (e.get("text") or "")[:70]
        if e.get("action") == "filed":
            return f"Learned: {what}"
        if e.get("action") == "rejected":
            return f"Set aside: {what} ({(e.get('reason') or '')[:40]})"
        return f"{(e.get('action') or 'learning').replace('_', ' ')}: {what}".strip(": ")
    if k == "ingest":
        j = e.get("jobs") or {}
        mem, rep = j.get("memory") or {}, j.get("repos") or {}
        bits = []
        if mem.get("added") or mem.get("updated"):
            bits.append(f"{mem.get('added', 0) + mem.get('updated', 0)} memory notes")
        if rep.get("new_commits"):
            bits.append(f"{rep['new_commits']} new commits")
        if (j.get("digest") or {}).get("distilled"):
            bits.append("today's conversations")
        return "Read your sources" + (": " + ", ".join(bits) if bits else " (nothing new)")
    return k or "event"


def _events(limit=400):
    p = data_path("events.jsonl")
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def snapshot(settings=None, now=None):
    """Everything the dashboard shows, as plain data (easy to test)."""
    s = settings or Settings.load()
    now = now or time.time()
    ev = _events()
    feed = [e for e in ev if e.get("kind") in ICON][-12:][::-1]
    today = dt.date.fromtimestamp(now).isoformat()
    todays = [e for e in ev if dt.date.fromtimestamp(e.get("time", 0)).isoformat() == today]
    routes = [e for e in todays if e.get("kind") == "route"]
    js = sorted(jobs.load().values(), key=lambda j: j.get("created", 0), reverse=True)
    try:
        q = quota.status(s)
    except Exception:  # noqa: BLE001
        q = {"window_pct": None, "waggle_units": 0}
    projs = []
    for p in projects.all_projects(s):
        try:
            tasks = [t for t in projects.tasks(p["name"], s) if not t.get("done")]
        except Exception:  # noqa: BLE001
            tasks = []
        projs.append({**p, "next": tasks[0]["text"] if tasks else ""})
    learned = [l[2:] for l in vault.read("Me/Learned", s).splitlines() if l.startswith("- ")][-6:][::-1]
    from . import call
    try:
        from . import objectives
        objs = [o for o in objectives.status() if o["status"] not in ("cancelled",)][:6]
    except Exception:
        objs = []
    try:
        from . import prompts, routines
        rts = routines.listing()
        meta = prompts.versions("router")
        active = meta.get("active", 0)
        scores = (meta.get("versions", {}).get(str(active)) or {}).get("scores", {}) if active else {}
        learning = {"router_version": active, "scores": scores,
                    "learned_events": [e for e in ev if e.get("kind") == "learning"][-3:][::-1]}
    except Exception:  # noqa: BLE001
        rts, learning = [], {}
    return {
        "now": now, "feed": feed,
        "active_jobs": [j for j in js if j.get("status") in ("running", "queued", "paused")],
        "recent_jobs": [j for j in js if j.get("status") in ("done", "failed")][:4],
        "today": {"messages": len(routes), "local": sum(r.get("route") == "local" for r in routes),
                  "claude": sum(r.get("route") != "local" for r in routes),
                  "median_s": (sorted(r.get("latency_s", 0) for r in routes)[len(routes) // 2] if routes else 0),
                  "screen": sum(e.get("kind") == "screen_look" for e in todays),
                  "calls": sum(e.get("kind") == "call" and e.get("ok") for e in todays)},
        "quota_pct": q.get("window_pct"), "projects": projs, "learned": learned,
        "calls_ready": call.configured(), "routines": rts, "learning": learning, "objectives": objs,
        "growth": _growth(),
    }


def _growth(days=7):
    try:
        from . import kstore
        return kstore.growth(days)
    except Exception:  # noqa: BLE001
        return []


def render_md(snap, settings=None):
    s = settings or Settings.load()
    now = snap["now"]
    t = snap["today"]
    pill = lambda label, ok: f"`{'🟢' if ok else '🟡'} {label}`"  # noqa: E731
    q = snap["quota_pct"]
    lines = ["---", "cssclasses: [waggle-hq]", "---",
             "# 🐝 Waggle HQ",
             f"<span class='hq-sub'>Live · updated {dt.datetime.fromtimestamp(now):%I:%M %p, %a %b %d}</span>", "",
             " ".join([pill("Online", True), pill(f"Local model {s.local_model}", True),
                       pill(f"Claude window {q:.0f}%" if q is not None else "Claude window ?", (q or 0) < 80),
                       pill("Calls ready" if snap["calls_ready"] else "Calls: run Setup Calls.bat",
                            snap["calls_ready"])]), ""]
    lines += ["## ⚡ Right now"]
    if snap["active_jobs"]:
        for j in snap["active_jobs"]:
            lines += [f"> [!hq-live] 🔨 {j.get('title')}",
                      f"> **{j.get('status')}** · started {ago(j.get('created', now), now)} · `{j.get('workdir', '')}`", ""]
    else:
        lines += ["> [!hq-idle] Idle", "> Nothing running. Ask for a build and it shows up here live.", ""]
    lines += ["## 📊 Today",
              f"> [!hq-stats] {t['messages']} messages",
              f"> **{t['local']}** answered locally (free) · **{t['claude']}** by Claude · median **{t['median_s']:.0f} s**"
              f" · 🖥️ {t['screen']} screen looks · 📞 {t['calls']} calls", ""]
    lines += ["## 🛰️ Activity"]
    for e in snap["feed"] or []:
        lines.append(f"- {ICON.get(e.get('kind'), '•')} {describe(e)} <span class='hq-time'>{ago(e.get('time', now), now)}</span>")
    if not snap["feed"]:
        lines.append("- Nothing yet today.")
    # One outer callout whose content is a CSS grid of nested project cards (waggle.css).
    lines += ["", "## 📁 Projects", "> [!hq-grid] Projects"]
    for i, p in enumerate(snap["projects"]):
        nxt = f"Next: {p['next']}" if p["next"] else "No open next action"
        if i:
            lines.append(">")
        lines += [f"> > [!hq-card|{p.get('status', 'active')}] [[{p['name']}]]",
                  f"> > `{p.get('status', 'active')}` · {nxt}"]
    if snap.get("objectives"):
        icon = {"done": "✅", "active": "⏳", "planning": "📝", "blocked": "⛔", "failed": "❌",
                "waiting_approval": "✋", "paused": "⏸️"}
        lines += ["", "## 🎯 Objectives"]
        lines += [f"- {icon.get(o['status'], '•')} "
                  + (f"[[{o['note_path']}|{o['title']}]]" if o.get("note_path") else f"**{o['title']}**")
                  + f" · {o['done']}/{o['total']} steps · "
                  f"{o['status'].replace('_', ' ')}" for o in snap["objectives"]]
    if snap.get("routines"):
        lines += ["", "## 🔁 Routines"]
        lines += [f"- {'⏸️ ' if not r['enabled'] else ''}**{r['name']}** · {r['schedule']} · next {r['next'] or '–'}"
                  + (f" · last: {r['last_status']}" if r.get("last_status") else "") for r in snap["routines"]]
    lr = snap.get("learning") or {}
    if lr:
        sc = lr.get("scores") or {}
        lines += ["", "## 🧭 Getting better",
                  f"- Router prompt **v{lr.get('router_version', 0)}**" +
                  (f" · benchmark {sc.get('tuning', 0):.0%} tuning / {sc.get('holdout', 0):.0%} held-out" if sc else
                   " (built-in)")]
        lines += [f"- {e.get('action', '').replace('_', ' ')} {ago(e.get('time', now), now)}"
                  for e in lr.get("learned_events", [])]
        lines += ["- Full report: [[Learning]] · live view: [Command Center](http://127.0.0.1:8897)"]
    if snap.get("growth"):
        lines += ["", "## 📈 Growth", "| Day | Notes | Facts | Learned | Quiz | Skills used |",
                  "|---|---|---|---|---|---|"]
        for g in snap["growth"]:
            q = g.get("quiz_score")
            lines.append(f"| {g['day'][5:]} | {g.get('notes', '')} | {g.get('facts_active', '')} | "
                         f"{g.get('facts_filed', 0)} | {'' if q is None else f'{q:.0%}'} | {g.get('skills_used', 0)} |")
        lines.append("- Details: [[Today I learned]] · [[Upcoming]]")
    lines += ["", "## 🧠 Recently learned about you"]
    lines += [f"- {l}" for l in snap["learned"]] or ["- Nothing new yet."]
    lines += ["", "## ✅ Finished builds"]
    lines += [f"- {'✅' if j.get('status') == 'done' else '❌'} {j.get('title')} · {ago(j.get('created', now), now)}"
              for j in snap["recent_jobs"]] or ["- None yet."]
    lines += ["", "[[Profile|👤 Profile]] · [[Projects|📁 All projects]] · [[Ideas|💡 Ideas]] · "
                  "[[Waggle HQ.canvas|🗺️ Map]]"]
    return "\n".join(lines) + "\n"


def render_canvas(snap, base=""):
    """Obsidian canvas JSON: Waggle in the centre, live work above, projects in a ring, you on the left."""
    nodes, edges = [], []

    def text(id_, x, y, w, h, body, color=None):
        n = {"id": id_, "type": "text", "x": x, "y": y, "width": w, "height": h, "text": body}
        if color:
            n["color"] = color
        nodes.append(n)

    def edge(a, b, color=None, label=None):
        e = {"id": f"e-{a}-{b}", "fromNode": a, "toNode": b}
        if color:
            e["color"] = color
        if label:
            e["label"] = label
        edges.append(e)

    t = snap["today"]
    text("waggle", -170, -90, 340, 180,
         f"# 🐝 Waggle\n**{t['messages']}** messages today\n{t['local']} local · {t['claude']} Claude", "6")
    nodes.append({"id": "me", "type": "file", "file": f"{base}/Me/Profile.md" if base else "Me/Profile.md", "x": -760, "y": -160, "width": 360, "height": 320,
                  "color": "5"})
    edge("waggle", "me", "5", "knows you")
    for i, j in enumerate(snap["active_jobs"][:4]):
        jid = f"job{i}"
        text(jid, -380 + i * 400, -470, 360, 150, f"### 🔨 {j.get('title')}\n**{j.get('status')}**",
             JOB_COLOR.get(j.get("status"), "3"))
        edge("waggle", jid, "4", "building")
    feed = "\n".join(f"- {ICON.get(e.get('kind'), '•')} {describe(e)}" for e in snap["feed"][:7]) or "- quiet"
    text("feed", 420, -160, 420, 320, "### 🛰️ Live activity\n" + feed, "4")
    edge("waggle", "feed", "4")
    ps = snap["projects"]
    r = 760
    for i, p in enumerate(ps):
        a = math.pi * (0.15 + 0.7 * (i / max(1, len(ps) - 1)))  # spread along the lower arc
        x, y = int(r * math.cos(a)) - 150, int(r * 0.55 * math.sin(a)) + 260
        pid = f"p{i}"
        body = f"### [[{p['name']}]]\n`{p.get('status', 'active')}`" + (f"\n{p['next'][:80]}" if p["next"] else "")
        text(pid, x, y, 300, 140, body, STATUS_COLOR.get(p.get("status", "active"), "4"))
        edge("waggle", pid, STATUS_COLOR.get(p.get("status", "active"), "4"))
    return {"nodes": nodes, "edges": edges}


def write(settings=None):
    s = settings or Settings.load()
    snap = snapshot(s)
    vault.write("Waggle HQ", render_md(snap, s), s)
    canvas = vault.own(s) / "Waggle HQ.canvas"
    canvas.write_text(json.dumps(render_canvas(snap, vault.own_rel(s)), indent=1, ensure_ascii=False), encoding="utf-8")
    return canvas


def main():
    write()


if __name__ == "__main__":
    main()
