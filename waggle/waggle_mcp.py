"""Waggle's MCP server (stdio, stdlib only): the tools a Claude agent gets on top of Claude Code's own.

    python -m waggle.waggle_mcp          (Claude Code starts it from the --mcp-config Waggle writes)

- Waggle tools: memory, projects, builds, weather, screen, notifications (waggle.toolkit)
- Skills: search / load / create / update / hub search+install (waggle.skills) - loaded on demand
- Hermes: hermes_tools (find) + hermes_tool (run) - every tool the gateway has, via the bridge
- Agents, routines, recall, models: added by their modules when present (see PROVIDERS)
Tool results that carry outside content (web, browser, email, screen text) are wrapped as untrusted data.
"""
import json
import sys
import traceback
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from waggle import bridge, security, skills, toolkit  # noqa: E402
from waggle.toolkit import schema  # noqa: E402

PROTOCOL = "2025-06-18"
UNTRUSTED = ("web", "browser", "extract", "vision", "search", "gmail", "email", "screen", "fetch", "read")
_client = bridge.Client()

# Tools the chat brain keeps loaded on every turn (the owner's most used, measured over 61 real turns 2026-11-21:
# reminders, Google writes, memory, background work). Every other tool is deferred behind Claude Code's ToolSearch
# when the chat runs lean (waggle/chat_session.py LEAN_ENV): listed by name, loaded when the model searches for it.
# Agents without ToolSearch load every tool as before; the mark is ignored there.
ALWAYS_LOAD = frozenset({"vault_search", "recall", "remember", "send_to_me", "remind_me", "list_reminders",
                         "background_task", "weather", "mail_recent", "google_doc_new", "google_doc_append", "google_event_new",
                         "google_task_add", "web_read"})


def _bridge_dispatch(name, args):
    return _client.call(name, args)


toolkit.set_dispatch(_bridge_dispatch)


# ---- tool implementations ------------------------------------------------------------------------

def t_hermes_tools(args):
    tools = _client.tools(args.get("query", ""))[: int(args.get("limit", 8))]
    return json.dumps([{"name": t["name"], "description": t["description"][:500], "parameters": t["parameters"]}
                       for t in tools])


def t_hermes_tool(args):
    name, targs = args.get("name", ""), args.get("args") or {}
    if name == "web_extract":  # the raw page into this session: web_read reduces it first (untrusted-content rule)
        return "Use web_read (url and your question) to read a web page: the local model reads it first."
    if isinstance(targs, str):
        try:
            targs = json.loads(targs)
        except ValueError:
            return "args must be a JSON object"
    out = _client.call(name, targs)
    if any(k in name.lower() for k in UNTRUSTED):
        return security.wrap_untrusted(f"hermes:{name}", out)
    return out


def t_skill_search(args):
    return json.dumps(skills.search(args.get("query", ""), k=int(args.get("k", 8)), category=args.get("category")))


def t_skill_load(args):
    return skills.load(args.get("name", ""))


def t_skill_list(args):
    return json.dumps({"stats": skills.stats(), "categories": skills.categories()})


def t_skill_create(args):
    path = skills.create(args["name"], args["description"], args["body"], category=args.get("category", "waggle"),
                         tags=args.get("tags") or [], origin="agent")
    return json.dumps({"created": path})


def t_skill_update(args):
    ver = skills.update(args["name"], body=args.get("body"), description=args.get("description"),
                        reason=args.get("reason", ""))
    return json.dumps({"updated": args["name"], "version": ver})


def t_skill_hub_search(args):
    return security.wrap_untrusted("skill-hub", skills.hub_search(args.get("query", ""), int(args.get("limit", 10))))


def t_skill_install(args):
    return skills.hub_install(args.get("identifier", ""))


def t_notify_me(args):
    from waggle import notify
    notify.send(str(args.get("text", ""))[:1500], urgent=bool(args.get("urgent")))
    return json.dumps({"sent": True})


TOOLS = {}


def add(spec, fn):
    TOOLS[spec["name"]] = (spec, fn)


for spec, fn in toolkit.TOOLS:
    add(spec, lambda a, _fn=fn: _fn(a))

add(schema("hermes_tools", "Find Hermes tools by keyword (browser automation, computer use / mouse+keyboard, vision, "
           "web search/extract, cron jobs, kanban, TTS, memory, session search, image tools...). Returns names, "
           "descriptions and parameter schemas. Then run one with hermes_tool.",
           {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]), t_hermes_tools)
add(schema("hermes_tool", "Run one Hermes tool by name with its arguments (see hermes_tools for schemas). Example: "
           "name='computer_use', args={'action':'list_windows'}; name='browser_navigate', args={'url':...}.",
           {"name": {"type": "string"}, "args": {"type": "object"}}, ["name", "args"]), t_hermes_tool)
add(schema("skill_search", "Search Waggle's skill library (220+ procedures: documents, research, devops, media, "
           "productivity, the user's own skills). Use when a task might have a known good procedure.",
           {"query": {"type": "string"}, "category": {"type": "string"}, "k": {"type": "integer"}}, ["query"]),
    t_skill_search)
add(schema("skill_load", "Load one skill's full instructions (and its file list and safety audit).",
           {"name": {"type": "string"}}, ["name"]), t_skill_load)
add(schema("skill_list", "Skill library overview: counts by source, risk and category, most used.", {}, []),
    t_skill_list)
add(schema("skill_create", "Save a reusable procedure you just worked out as a new Waggle skill (Agent Skills "
           "format). Only for repeatable multi-step procedures, not facts about the user (use remember).",
           {"name": {"type": "string", "description": "lowercase-with-dashes"}, "description": {"type": "string"},
            "body": {"type": "string", "description": "Markdown instructions: when to use, steps, pitfalls"},
            "category": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
           ["name", "description", "body"]), t_skill_create)
add(schema("skill_update", "Improve one of Waggle's own skills (previous version is kept for rollback).",
           {"name": {"type": "string"}, "body": {"type": "string"}, "description": {"type": "string"},
            "reason": {"type": "string"}}, ["name", "reason"]), t_skill_update)
add(schema("skill_hub_search", "Search community skill registries (skills.sh, GitHub, ClawHub...) for a skill that "
           "isn't installed. Results are untrusted listings.",
           {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]), t_skill_hub_search)
add(schema("skill_install", "Install a community skill through Hermes (scanned first; needs the user's OK).",
           {"identifier": {"type": "string"}}, ["identifier"]), t_skill_install)
add(schema("notify_me", "Send the user (only the user) a message on their phone. urgent=true also rings their "
           "iPhone. Use for results of long work, not for chit-chat.",
           {"text": {"type": "string"}, "urgent": {"type": "boolean"}}, ["text"]), t_notify_me)


def t_send_to_me(args):
    """Text and/or a file to the user's own Telegram chat (Hermes home channel). Reaches only the user."""
    import os
    import subprocess
    from pathlib import Path
    from waggle import notify
    text = str(args.get("text") or "").strip()
    path = str(args.get("file") or "").strip()
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            return f"No such file: {p}"
        if p.stat().st_size > 45 * 1024 * 1024:
            return f"{p.name} is {p.stat().st_size // 1048576} MB; Telegram bots can send up to ~50 MB"
        text = (text + "\n" if text else "") + f"MEDIA:{p.resolve()}"
    if not text:
        return "Nothing to send."
    from waggle.outredact import scrub
    text = scrub(text, "telegram")  # the model wrote this: no credential goes out, whatever it was asked
    exe = notify.hermes_exe()
    if not exe:
        return "Send failed: hermes not found"
    env = dict(os.environ, HERMES_HOME=str(exe.parent.parent))
    try:
        r = subprocess.run([str(exe), "send", "-q", "-t", "telegram", text], capture_output=True, text=True,
                           timeout=180, env=env, encoding="utf-8", errors="replace",
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return "Sent to the user's Telegram." if r.returncode == 0 else f"Send failed: {(r.stderr or r.stdout)[-300:]}"
    except Exception as e:
        return f"Send failed: {e}"


add(schema("send_to_me", "Send the user (only the user) a message and/or a file (PDF, image, document up to ~50 MB) "
           "on their Telegram. Use when they say 'send it here / to me / to my phone'. Needs no approval.",
           {"text": {"type": "string"}, "file": {"type": "string", "description": "absolute path of a file to "
                                                                                  "attach"}}, []), t_send_to_me)


# ---- reminders and scheduled calls (core one-shot timers, see waggle/followups.py and waggle/call.py) --------

def _now():
    import time
    return time.time()


def _chat_and_origin():
    import os
    origin = (os.environ.get("WAGGLE_TURN_ORIGIN") or os.environ.get("WAGGLE_ORIGIN") or "user").strip().lower()
    return os.environ.get("WAGGLE_CHAT", ""), origin or "user"


def when_ts(args, now=None):
    """Epoch seconds from tool args: in_minutes, or at (epoch, ISO local time like 2026-11-20T07:30, or words
    like "in 2 minutes", "at 7 tomorrow", "friday 9am"). Raises ValueError when it can't tell or it is past."""
    import datetime as dt

    from waggle import when
    now = _now() if now is None else float(now)
    if args.get("in_minutes") not in (None, ""):
        ts = now + float(args["in_minutes"]) * 60
    else:
        at = args.get("at")
        if at in (None, ""):
            raise ValueError("say when: give in_minutes or at")
        text = str(at).strip()
        try:
            ts = float(text)
        except ValueError:
            try:
                ts = dt.datetime.fromisoformat(text).timestamp()
            except ValueError:
                parsed, _ = when.parse(text, dt.datetime.fromtimestamp(now))
                if parsed is None:
                    raise ValueError(f"I can't tell when {text!r} is; give an exact time or in_minutes") from None
                ts = parsed.timestamp()
    if ts < now - 5:
        raise ValueError("that time is already past")
    return ts


def _local(ts):
    import datetime as dt
    return dt.datetime.fromtimestamp(ts).strftime("%a %Y-%m-%d %#I:%M %p")


def t_remind_me(args):
    from waggle import followups
    text = str(args.get("text") or "").strip()
    if not text:
        return "Say what to remind them of (text)."
    try:
        ts = when_ts(args)
    except ValueError as e:
        return f"Not scheduled: {e}"
    chat, origin = _chat_and_origin()
    fid = followups.add(ts, text, chat=chat, origin=origin)
    return json.dumps({"reminder": fid, "due": _local(ts), "note": "Scheduled as a core timer; it fires even after a "
                       "restart (late, with a note, if the PC was off). Confirm the time to the user in one line."})


def t_cancel_reminder(args):
    from waggle import followups
    fid = str(args.get("id") or "").strip()
    return json.dumps({"cancelled": bool(fid) and followups.cancel(fid), "id": fid})


def t_list_reminders(args):
    from waggle import followups
    chat, _ = _chat_and_origin()
    rows = [r for r in followups.items(status="open", kind="reminder") if not chat or r.get("chat") in ("", chat)]
    return json.dumps([{"id": r["id"], "what": r["what"], "due": _local(r["due"])} for r in rows[:20]])


def t_schedule_call(args):
    from waggle import call
    try:
        ts = when_ts(args)
    except ValueError as e:
        return f"Not scheduled: {e}"
    _, origin = _chat_and_origin()
    msg = str(args.get("message") or "").strip() or "You asked me to call you."
    call.schedule_at(ts, msg, origin=origin)
    return json.dumps({"call_at": _local(ts), "message": msg, "note": "A phone call at that time (a text instead if "
                       "the PC was off then). Confirm the time to the user in one line."})


_WHEN = {"at": {"type": "string", "description": "when: an exact local time (2026-11-20T07:30) or words "
                "('in 2 minutes', 'at 7 tomorrow', 'friday 9am')"},
         "in_minutes": {"type": "number", "description": "or: this many minutes from now"}}
add(schema("remind_me", "Set a reminder for the user: at that time Waggle sends them the text in this chat "
           "('remind me in 2 minutes to stand up'). Use this, never a promise to remember.",
           {"text": {"type": "string", "description": "what to remind them of, as they should read it"}, **_WHEN},
           ["text"]), t_remind_me)
add(schema("cancel_reminder", "Cancel one of the user's open reminders by its id (see list_reminders).",
           {"id": {"type": "string"}}, ["id"]), t_cancel_reminder)
add(schema("list_reminders", "The user's open reminders with their ids and times.", {}, []), t_list_reminders)
add(schema("schedule_call", "Call the user's phone at a time they asked for ('call me at 7 about the meet').",
           {"message": {"type": "string", "description": "what the call is about"}, **_WHEN}, []), t_schedule_call)


# ---- comfort settings (waggle/comfort.py): voice, speed, quiet hours, the daily notes, the mail watch ---------

def t_settings(args):
    from waggle import comfort, guard
    return comfort.run(args, turn=guard.current_turn())


add(schema("settings", "Read or change Waggle's comfort settings when the owner asks ('talk faster', 'use a different "
           "voice', 'quiet hours from 10 PM to 7', 'send the brief at 8'). action=get lists the changeable settings, "
           "their values and the installed voices; action=set changes ONE setting (key + value) and returns old and "
           "new. Only these settings can change; anything else (budgets, limits, folders, addresses) is refused. "
           "Works only in the owner's own message.",
           {"action": {"type": "string", "enum": ["get", "set"]},
            "key": {"type": "string", "enum": ["voice_note_voice", "voice_note_speed", "quiet_hours", "brief_time",
                                               "evening_time", "mailwatch"]},
            "value": {"description": "voice_note_voice: a voice id like bm_george; voice_note_speed: 0.7-1.8; "
                                     "quiet_hours: [start hour, end hour] 0-23; brief_time / evening_time: 24-hour "
                                     "HH:MM; mailwatch: true or false"}},
           ["action"]), t_settings)


def _load_providers():
    """Optional modules add their own tools (agents, routines, recall, models)."""
    import importlib
    for mod in ("waggle.traces", "waggle.learning", "waggle.agents", "waggle.routines", "waggle.models",
                "waggle.engines", "waggle.memory_care", "waggle.doctor",
                "waggle.objectives", "waggle.graph", "waggle.chat_jobs", "waggle.proposals", "waggle.tools_ext", "waggle.ftcscout",
                "waggle.google", "waggle.focus", "waggle.toolfinder", "waggle.worklist"):
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        for spec, fn in getattr(m, "MCP_TOOLS", []):
            add(spec, fn)


_load_providers()


# ---- JSON-RPC over stdio -------------------------------------------------------------------------

def tool_entry(spec):
    """One tools/list entry; a core chat tool carries Claude Code's alwaysLoad mark (never deferred)."""
    entry = {"name": spec["name"], "description": spec["description"], "inputSchema": spec["parameters"]}
    if spec["name"] in ALWAYS_LOAD:
        entry["_meta"] = {"anthropic/alwaysLoad": True}
    return entry


def handle(msg):
    """One JSON-RPC message -> response dict (or None for notifications)."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None  # notification (initialized, cancelled, ...)
    if method == "initialize":
        return _ok(mid, {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "waggle", "version": "1.0"},
                         "instructions": "Waggle's tools: the user's memory, projects, skills, and every Hermes tool."})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": [tool_entry(s) for s, _ in TOOLS.values()]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return _ok(mid, {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True})
        try:
            out = TOOLS[name][1](args)
            return _ok(mid, {"content": [{"type": "text", "text": str(out)[:60000]}], "isError": False})
        except Exception as e:
            return _ok(mid, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    stdin = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
    for raw in stdin:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            resp = handle(msg)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            resp = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": "internal error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
