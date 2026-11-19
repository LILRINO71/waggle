"""Waggle's own tools, defined once and served two ways:
- registered in Hermes by the plugin (so the local model and Hermes features can call them);
- exposed to Claude agents by the Waggle MCP server (waggle_mcp.py).

Every function takes an args dict and returns a string (JSON or prose). `set_dispatch` provides a way to call
Hermes tools (the plugin passes ctx.dispatch_tool; the MCP server passes the bridge client)."""
import json
import time

from . import call, desktop, events, jobs, learn, live, memory, projects, quota, screen
from .config import Settings

_dispatch = None


def set_dispatch(fn):
    global _dispatch
    _dispatch = fn


def dispatch(name, args):
    if _dispatch is None:
        raise RuntimeError("Hermes tools are not reachable (gateway not running)")
    return _dispatch(name, args)


def vault_search(args, **kw):
    hits = memory.search(args.get("query", ""), k=int(args.get("k", 6)))
    return json.dumps([{"path": h["path"], "score": h["score"], "text": h["text"][:800]} for h in hits])


def remember(args, **kw):
    kind = args.get("kind", "fact")
    if kind not in learn.FILES:
        kind = "fact"
    filed = learn.file_facts([{"kind": kind, "text": args.get("text", "")}])
    return json.dumps({"saved": bool(filed), "note": learn.FILES[kind]})


def start_build(args, **kw):
    s = Settings.load()
    ok, why = quota.can_escalate(s.builder_model, s)
    if not ok:
        return json.dumps({"started": False, "reason": why})
    job_id = jobs.create(args.get("title", "Build"), args["prompt"], args.get("request", args["prompt"]),
                         project=args.get("project"))
    jobs.launch(job_id)
    return json.dumps({"started": True, "job_id": job_id})


def build_status(args, **kw):
    rows = sorted(jobs.load().values(), key=lambda j: j.get("created", 0), reverse=True)[:8]
    return json.dumps([{k: j.get(k) for k in ("id", "title", "status", "workdir", "note")} |
                       {"result": (j.get("result") or "")[:500]} for j in rows])


def waggle_status(args, **kw):
    return json.dumps({"quota": quota.status(), "jobs": len(jobs.load()), "vault": Settings.load().vault,
                       "stats": events.summary()})


def _project_name(args):
    name = args.get("project", "")
    known = [p["name"] for p in projects.all_projects()]
    if name in known:
        return name
    hits = projects.find(name)
    return hits[0] if hits else None


def project_status(args, **kw):
    name = _project_name(args) if args.get("project") else None
    if not name:
        return json.dumps({"projects": projects.all_projects()})
    return json.dumps({"project": name, "state": projects.sections(name), "tasks": projects.tasks(name)})


def project_update(args, **kw):
    name = _project_name(args)
    if not name:
        return json.dumps({"ok": False, "error": "unknown project",
                           "known": [p["name"] for p in projects.all_projects()]})
    action = args.get("action", "log")
    text = args.get("text", "")
    if action == "add_task":
        projects.add_task(name, text)
    elif action == "complete_task":
        done = projects.complete_task(name, text)
        return json.dumps({"ok": bool(done), "completed": done})
    elif action in ("Current focus", "Open problems", "Decisions", "Goal", "Resources"):
        projects._set_section(name, action, text)
        events.log("project_update", project=name, action=action)
    else:
        projects.add_log(name, text)
    return json.dumps({"ok": True, "project": name})


def weather(args, **kw):
    try:
        return live.weather(args.get("location") or None, args.get("day", "tomorrow"))
    except Exception as e:
        return f"Weather service unavailable right now ({type(e).__name__}). Tell the user and try web_search."


def call_me_text(minutes, about):
    """Place or schedule a call to the user; returns context telling the model what happened."""
    if not call.configured():
        return ("<waggle_action>Phone calls aren't set up yet. Tell the user in one sentence to run "
                "'Setup Calls.bat' in the waggle folder (free Twilio trial).</waggle_action>")
    msg = about or "You asked me to call you."
    if minutes <= 0.05:
        res = call.call(msg, reason="asked", force=True)
        what = "Calling the user now." if res["ok"] else f"The call failed: {res['why']}."
    else:
        due = call.schedule(msg, minutes)
        what = f"Call scheduled for {time.strftime('%I:%M %p', time.localtime(due)).lstrip('0')}."
    return f"<waggle_action>{what} Confirm it to the user in one short sentence.</waggle_action>"


def call_me(args, **kw):
    return call_me_text(float(args.get("minutes") or 0), args.get("message", ""))


def look_at_screen(args, **kw):
    try:
        q = args.get("question", "What is on my screen?")
        if _dispatch is not None:
            return desktop.screen_context(q, _dispatch, vision=lambda x: screen.look(x))
        return screen.look(q)
    except Exception as e:
        return f"Could not look at the screen ({type(e).__name__}: {e})."


def rate_finding(args, **kw):
    events.log("feedback", finding=args.get("finding_id"), useful=bool(args.get("useful")),
               note=args.get("note", "")[:300])
    return json.dumps({"ok": True})


def schema(name, desc, props, required):
    return {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}


TOOLS = [
    (schema("vault_search", "Search the user's Obsidian vault (their profile, projects, journal, ideas, people) "
            "for notes relevant to a question. Use before answering anything personal.",
            {"query": {"type": "string"}, "k": {"type": "integer"}}, ["query"]), vault_search),
    (schema("remember", "Save a durable fact about the user (like, dislike, preference, goal, fact, person, "
            "project, idea) to their vault. Use when the user says 'remember' or states a lasting preference.",
            {"kind": {"type": "string", "enum": list(learn.FILES)}, "text": {"type": "string"}},
            ["kind", "text"]), remember),
    (schema("start_build", "Hand a big multi-step build (app, website, game, multi-file code) to Claude Opus as "
            "a background job. Write a detailed prompt: goal, audience, features, tech, definition of done.",
            {"title": {"type": "string"}, "prompt": {"type": "string"}, "request": {"type": "string"},
             "project": {"type": "string"}}, ["title", "prompt"]), start_build),
    (schema("build_status", "List recent background build jobs and their status/results.", {}, []), build_status),
    (schema("waggle_status", "Show Claude quota use, job count, vault location and usage stats.", {}, []),
     waggle_status),
    (schema("project_status", "Get the structured state of one of the user's projects (goal, focus, open problems, "
            "next actions, decisions, log), or list all projects if no name is given. Use for 'where were we with X'.",
            {"project": {"type": "string"}}, []), project_status),
    (schema("project_update", "Update a project's state after meaningful progress. action: 'log' (add a dated log "
            "entry), 'add_task', 'complete_task', or replace a section: 'Current focus', 'Open problems', "
            "'Decisions', 'Goal', 'Resources'. Do not log chit-chat.",
            {"project": {"type": "string"}, "action": {"type": "string"}, "text": {"type": "string"}},
            ["project", "action", "text"]), project_update),
    (schema("weather", "Get the weather forecast. Use for ANY weather question. Leave location empty to use the "
            "user's saved home city; if the result says NO_LOCATION, ask the user for their city.",
            {"location": {"type": "string", "description": "City, e.g. 'Fargo, ND'. Empty = saved home city."},
             "day": {"type": "string", "enum": ["today", "tomorrow", "week"]}}, []), weather),
    (schema("call_me", "Phone the user (a real call) now or after some minutes, reading out a short message.",
            {"message": {"type": "string"}, "minutes": {"type": "number", "description": "0 = now"}},
            ["message"]), call_me),
    (schema("look_at_screen", "Read the user's PC screen: open windows plus on-screen text, and a local vision "
            "model for how things look. Stays on the PC.",
            {"question": {"type": "string"}}, ["question"]), look_at_screen),
    (schema("rate_finding", "Record whether a proactive suggestion from Waggle was useful (the user's reaction).",
            {"finding_id": {"type": "string"}, "useful": {"type": "boolean"}, "note": {"type": "string"}},
            ["finding_id", "useful"]), rate_finding),
]
