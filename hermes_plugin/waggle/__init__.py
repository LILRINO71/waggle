"""Hermes plugin that turns Hermes into Waggle.

Every user turn:  notifications -> resume paused builds -> vault memory -> route
  trivial            -> local model (with memory)
  route=sonnet       -> Sonnet's answer is the reply (turn blocked before the local model runs)
  route=opus         -> background Claude Code build starts; you get pinged when it's done
  budget/limit hit   -> local model does it anyway
After every turn: the local model files new facts about you into the vault.
Before every tool call: guardrails (messaging, money, destructive commands need your OK).
"""
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path

# Waggle is stdlib-only and loaded from the repo, so Hermes's locked environment is never modified.
_repo = Path(__file__).with_name("repo_path.txt")
if _repo.exists() and _repo.read_text(encoding="utf-8").strip() not in sys.path:
    sys.path.insert(0, _repo.read_text(encoding="utf-8").strip())

from waggle import (call, desktop, events, hq, jobs, learn, live, memory, notify, ollama, projects, quota, router, screen,
                    security, selfbuild, vault)
from waggle.config import Settings

log = logging.getLogger("waggle")
_last_index = 0.0
_index_lock = threading.Lock()
SKIP_ROUTING_PLATFORMS = {"cron", "kanban", "subagent", "delegation"}


def _reindex_soon():
    global _last_index
    if time.time() - _last_index < 120 or not _index_lock.acquire(blocking=False):
        return

    def work():
        global _last_index
        try:
            memory.reindex()
        except Exception as e:
            log.warning("vault reindex failed: %s", e)
        finally:
            _last_index = time.time()
            _index_lock.release()
    threading.Thread(target=work, daemon=True, name="waggle-index").start()


FEEDBACK = re.compile(r"^\s*(not\s+useful|useful|👍|👎)\s+([0-9a-f]{12})\s*$", re.I)


def on_pre_llm_call(session_id=None, user_message="", platform="", **kwargs):
    m = FEEDBACK.match(user_message or "")
    if m:
        useful = m.group(1).lower() in ("useful", "👍")
        events.log("feedback", finding=m.group(2), useful=useful, platform=platform)
        return {"action": "block", "message": "Noted, thanks. " + ("I'll look for more like that." if useful
                                                                   else "I'll be pickier about those.")}
    _reindex_soon()
    if session_id and user_message:
        _last_msg[session_id] = user_message
    extras = []
    news = notify.unseen()
    if news:
        extras.append("Tell the user these updates first:\n" + "\n".join(f"- {n['text']}" for n in news))
    try:
        jobs.resume_paused()
    except Exception as e:
        log.warning("resume_paused failed: %s", e)
    ctx = memory.context_block(user_message or "")
    try:
        ctx += "\n\n" + projects.context_for(user_message or "")
    except Exception as e:
        log.warning("project context failed: %s", e)
    if platform in SKIP_ROUTING_PLATFORMS or not user_message:
        return {"context": "\n\n".join([ctx] + extras)}
    # Whoever answers (Haiku, Sonnet or the local model) sees the last few exchanges, so it's a conversation.
    recent = _recent(session_id)
    if recent:
        ctx += "\n\n<recent_conversation>\n" + recent + "\n</recent_conversation>"
    _remember_turn(session_id, "User", user_message)

    # The last turn asked "which city?" -> this short reply is the city.
    asked = _awaiting_city.pop(session_id, None)
    if asked and time.time() - asked[1] < 900 and _looks_like_place(user_message):
        place = user_message.strip(" .!?")
        try:
            live.remember_city(place)
            data = live.weather(place, asked[0])
            ctx += f"\n\n<live_data source=\"weather\">The user just told you their city ({place}); it is now saved.\n{data}\nAnswer from this data.</live_data>"
        except Exception as e:
            log.warning("weather for %s failed: %s", place, e)
        return {"context": "\n\n".join([ctx] + extras)}
    # "Call me in 10 minutes about X": handled here so it never depends on the small model picking a tool.
    want = call.parse_request(user_message)
    if want:
        return {"context": "\n\n".join([ctx, _call_me(*want)] + extras)}
    # "Open notepad and type hello": done here, deterministically, then the model just confirms.
    opener = desktop.parse_open(user_message)
    if opener and _ctx is not None:
        ok, _, _ = desktop.launch(opener[0], runner=lambda c: None)  # dry check: is it a real app?
        if ok:
            res = desktop.open_and_type(opener[0], opener[1], _dispatch)
            _last_app[session_id] = opener[0]
            return {"context": "\n\n".join([ctx, f"<waggle_action>{res} Confirm to the user in one short "
                                                 "sentence.</waggle_action>"] + extras)}
    # "What's on my screen?": window list + on-screen text in ~2 s; vision model only for how things look.
    # Answered locally from what was read, so no Sonnet round trip and no second look.
    if screen.wants_look(user_message):
        look = (desktop.screen_context(user_message, _dispatch, vision=lambda q: screen.look(q))
                if _ctx is not None else screen.context(user_message))
        return {"context": "\n\n".join([ctx, look] + extras)}
    # Live data first, so whichever brain answers (local or Sonnet) has it.
    if live.needs_live_info(user_message) or live.weather_request(user_message):
        try:
            data = live.prefetch(user_message, search=_web_search)
        except Exception as e:
            log.warning("live prefetch failed: %s", e)
            data = ""
        if data and "no city is saved" in data:
            w = live.weather_request(user_message)
            _awaiting_city[session_id] = ((w[1] if w else "tomorrow"), time.time())
        if data:
            ctx += "\n\n" + data  # ctx goes to the router (Sonnet) and the local model alike
        else:
            extras.append(live.LIVE_NUDGE)
    d = router.decide(user_message, ctx)
    vault.journal(f"route={d['route']} by={d.get('by')} - {user_message[:80]!r}")
    news_text = ("\n\n" + "\n".join(f"(Update: {n['text']})" for n in news)) if news else ""
    if d["route"] == "sonnet" and d.get("reply"):
        return _hand_over(session_id, user_message, d["reply"] + news_text)
    if d["route"] == "opus" and d.get("build_prompt"):
        title = d.get("title") or "Build"
        proj = (projects.find(user_message) or [None])[0]
        job_id = jobs.create(title, d["build_prompt"], user_message, project=proj)
        jobs.launch(job_id)
        msg = (f"On it. I gave this to Opus as a background build: **{title}**.\n"
               f"I'll ping you when it's done (job `{job_id}`). Ask \"build status\" any time.{news_text}")
        return _hand_over(session_id, user_message, msg)
    if d.get("looked_big"):
        extras.append("Claude is unavailable right now (" + d.get("reason", "") + "). Do the best you can locally, "
                      "and tell the user a stronger version can run once the Claude limit resets.")
    return {"context": "\n\n".join([ctx] + extras)}


# Kept for a turn in flight across a plugin reload; nothing new is queued here since replies are delivered
# directly by _hand_over.
_pending = {}
_pending_lock = threading.Lock()


def _hand_over(session_id, user_message, reply):
    """Deliver a Claude-written reply immediately: the turn ends here with no local-model call.

    (The old placeholder trick made the local model write "OK" first, 3-9 s wasted per message.) Hermes stores
    a vetoed turn's user text as "[Message withheld]", but that no longer matters for context: Waggle keeps its
    own transcript (_convo) and hands it to whichever model answers next."""
    _remember_turn(session_id, "Waggle", reply)
    learn.learn_async(user_message, reply)
    threading.Thread(target=_refresh_hq, daemon=True).start()
    return {"action": "block", "message": reply}


_convo = {}  # session_id -> recent (speaker, text) pairs
_CONVO_TURNS = 12


def _remember_turn(session_id, who, text):
    if not session_id or not text:
        return
    turns = _convo.setdefault(session_id, [])
    turns.append((who, " ".join(str(text).split())[:1200]))
    del turns[:-_CONVO_TURNS]


def _recent(session_id):
    return "\n".join(f"{who}: {text}" for who, text in _convo.get(session_id, []))


def on_transform_llm_output(response_text="", session_id="", **kwargs):
    with _pending_lock:
        reply = _pending.pop(session_id, None)
    final = reply if reply is not None else response_text
    # Never "I can't": a capability refusal becomes a background build of that capability.
    try:
        swap = selfbuild.handle(_last_msg.get(session_id, ""), final or "")
    except Exception as e:
        log.warning("selfbuild failed: %s", e)
        swap = None
    _remember_turn(session_id, "Waggle", swap or final)
    return swap if swap else reply  # None leaves the model's own reply unchanged


def on_post_llm_call(user_message="", assistant_response="", **kwargs):
    if user_message and assistant_response:
        learn.learn_async(user_message, assistant_response)
    threading.Thread(target=_refresh_hq, daemon=True).start()  # Obsidian HQ shows this turn right away


def _refresh_hq():
    try:
        hq.write()
    except Exception as e:
        log.warning("HQ refresh failed: %s", e)


def _dispatch(name, args):
    return _ctx.dispatch_tool(name, args)


_last_app = {}  # session_id -> app the user's desktop work is about
_last_msg = {}  # session_id -> the user's latest message (for the never-"I can't" check)
UNTARGETED_OK = {"list_apps", "list_windows", "wait"}


def on_pre_tool_call(tool_name="", args=None, session_id="", **kwargs):
    args = args or {}
    directive = security.check_tool_call(tool_name, args)
    if directive or tool_name != "computer_use":
        return directive
    # cua-driver refuses the Claude window, so "whatever is in front" fails whenever Claude is in front:
    # aim every action at a named app (the one this conversation is working in).
    if args.get("app"):
        _last_app[session_id] = args["app"]
        return None
    if args.get("action") in UNTARGETED_OK or args.get("pid") or args.get("window_id"):
        return None
    if _last_app.get(session_id):
        return {"action": "modify", "args": {"app": _last_app[session_id]}}
    return {"action": "block", "message": "Name the app: call computer_use with action=list_windows to see what's "
            "open, then repeat this with app=<its name> (e.g. app='Notepad'). To start an app, ask Waggle to "
            "'open <app>'."}


# ---- tools ---------------------------------------------------------------------------------------

def t_vault_search(args, **kw):
    hits = memory.search(args.get("query", ""), k=int(args.get("k", 6)))
    return json.dumps([{"path": h["path"], "score": h["score"], "text": h["text"][:800]} for h in hits])


def t_remember(args, **kw):
    kind = args.get("kind", "fact")
    if kind not in learn.FILES:
        kind = "fact"
    filed = learn.file_facts([{"kind": kind, "text": args.get("text", "")}])
    return json.dumps({"saved": bool(filed), "note": learn.FILES[kind]})


def t_start_build(args, **kw):
    s = Settings.load()
    ok, why = quota.can_escalate(s.builder_model, s)
    if not ok:
        return json.dumps({"started": False, "reason": why})
    job_id = jobs.create(args.get("title", "Build"), args["prompt"], args.get("request", args["prompt"]))
    jobs.launch(job_id)
    return json.dumps({"started": True, "job_id": job_id})


def t_build_status(args, **kw):
    rows = sorted(jobs.load().values(), key=lambda j: j.get("created", 0), reverse=True)[:8]
    return json.dumps([{k: j.get(k) for k in ("id", "title", "status", "workdir", "note")} |
                       {"result": (j.get("result") or "")[:500]} for j in rows])


def t_waggle_status(args, **kw):
    return json.dumps({"quota": quota.status(), "jobs": len(jobs.load()), "vault": Settings.load().vault,
                       "stats": events.summary()})


def _project_name(args):
    name = args.get("project", "")
    known = [p["name"] for p in projects.all_projects()]
    if name in known:
        return name
    hits = projects.find(name)
    return hits[0] if hits else None


def t_project_status(args, **kw):
    name = _project_name(args) if args.get("project") else None
    if not name:
        return json.dumps({"projects": projects.all_projects()})
    return json.dumps({"project": name, "state": projects.sections(name), "tasks": projects.tasks(name)})


def t_project_update(args, **kw):
    name = _project_name(args)
    if not name:
        return json.dumps({"ok": False, "error": "unknown project", "known": [p["name"] for p in projects.all_projects()]})
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


def t_weather(args, **kw):
    try:
        return live.weather(args.get("location") or None, args.get("day", "tomorrow"))
    except Exception as e:
        return f"Weather service unavailable right now ({type(e).__name__}). Tell the user and try web_search."


def _call_me(minutes, about):
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


def t_call_me(args, **kw):
    return _call_me(float(args.get("minutes") or 0), args.get("message", ""))


def t_look_at_screen(args, **kw):
    try:
        q = args.get("question", "What is on my screen?")
        if _ctx is not None:
            return desktop.screen_context(q, _dispatch, vision=lambda x: screen.look(x))
        return screen.look(q)
    except Exception as e:
        return f"Could not look at the screen ({type(e).__name__}: {e})."


def t_rate_finding(args, **kw):
    events.log("feedback", finding=args.get("finding_id"), useful=bool(args.get("useful")),
               note=args.get("note", "")[:300])
    return json.dumps({"ok": True})


def _schema(name, desc, props, required):
    return {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}


TOOLS = [
    (_schema("vault_search", "Search the user's Obsidian vault (their profile, projects, journal, ideas, people) "
             "for notes relevant to a question. Use before answering anything personal.",
             {"query": {"type": "string"}, "k": {"type": "integer"}}, ["query"]), t_vault_search),
    (_schema("remember", "Save a durable fact about the user (like, dislike, preference, goal, fact, person, "
             "project, idea) to their vault. Use when the user says 'remember' or states a lasting preference.",
             {"kind": {"type": "string", "enum": list(learn.FILES)}, "text": {"type": "string"}},
             ["kind", "text"]), t_remember),
    (_schema("start_build", "Hand a big multi-step build (app, website, game, multi-file code) to Claude Opus as "
             "a background job. Write a detailed prompt: goal, audience, features, tech, definition of done.",
             {"title": {"type": "string"}, "prompt": {"type": "string"}, "request": {"type": "string"}},
             ["title", "prompt"]), t_start_build),
    (_schema("build_status", "List recent background build jobs and their status/results.", {}, []), t_build_status),
    (_schema("waggle_status", "Show Claude Pro quota use, job count, vault location and usage stats.", {}, []),
     t_waggle_status),
    (_schema("project_status", "Get the structured state of one of the user's projects (goal, focus, open problems, "
             "next actions, decisions, log), or list all projects if no name is given. Use for 'where were we with X'.",
             {"project": {"type": "string"}}, []), t_project_status),
    (_schema("project_update", "Update a project's state after meaningful progress. action: 'log' (add a dated log "
             "entry), 'add_task', 'complete_task', or replace a section: 'Current focus', 'Open problems', "
             "'Decisions', 'Goal', 'Resources'. Do not log chit-chat.",
             {"project": {"type": "string"}, "action": {"type": "string"}, "text": {"type": "string"}},
             ["project", "action", "text"]), t_project_update),
    (_schema("weather", "Get the weather forecast. Use for ANY weather question. Leave location empty to use the "
             "user's saved home city; if the result says NO_LOCATION, ask the user for their city.",
             {"location": {"type": "string", "description": "City, e.g. 'Fargo, ND'. Empty = saved home city."},
              "day": {"type": "string", "enum": ["today", "tomorrow", "week"]}}, []), t_weather),
    (_schema("call_me", "Phone the user (a real call) now or after some minutes, reading out a short message.",
             {"message": {"type": "string"}, "minutes": {"type": "number", "description": "0 = now"}},
             ["message"]), t_call_me),
    (_schema("look_at_screen", "Take a screenshot of the user's PC screen and have a local vision model answer a "
             "question about it (read errors, describe windows). Stays on the PC.",
             {"question": {"type": "string"}}, ["question"]), t_look_at_screen),
    (_schema("rate_finding", "Record whether a proactive suggestion from Waggle was useful (the user's reaction).",
             {"finding_id": {"type": "string"}, "useful": {"type": "boolean"}, "note": {"type": "string"}},
             ["finding_id", "useful"]), t_rate_finding),
]


_ctx = None
_awaiting_city = {}  # session_id -> (day, asked_at)


def _looks_like_place(text):
    """A short reply like 'Menifee' or 'Menifee, CA' (not a new question)."""
    t = (text or "").strip(" .!")
    return 0 < len(t) <= 40 and "?" not in t and len(t.split()) <= 4 and not live.needs_live_info(t)


def _web_search(query):
    """Run Hermes's own web_search tool (keyless free tier) on the user's behalf."""
    if _ctx is None:
        return None
    out = _ctx.dispatch_tool("web_search", {"query": query[:300]})
    events.log("web_search", query=query[:200], chars=len(out or ""), preview=(out or "")[:300])
    return out


def _wake_starts_conversation():
    """Saying "waggle" starts a back-and-forth conversation instead of a single command.

    Hermes's wake word captures ONE utterance, then you must say the wake word again. Its push-to-talk mode
    already does real conversation (keeps listening after each reply, lets you interrupt, ends after three
    silent turns or when you say "stop"). This switches the wake word into that mode. Runtime wrap from the
    plugin, no Hermes files edited; if Hermes renames these internals it quietly does nothing."""
    try:
        from hermes_cli.cli_voice_mixin import CLIVoiceMixin
    except Exception:
        return False
    original = getattr(CLIVoiceMixin, "_on_wake_word", None)
    if original is None or getattr(original, "_waggle_wrapped", False):
        return False

    def on_wake(self, *a, **k):
        busy = getattr(self, "_agent_running", False) or getattr(self, "_voice_recording", False)
        out = original(self, *a, **k)
        if not busy and getattr(self, "_voice_mode", False):  # the wake was accepted and a capture started
            lock = getattr(self, "_voice_lock", None)
            if lock is not None:
                with lock:
                    self._voice_continuous = True
            else:
                self._voice_continuous = True
        return out

    on_wake._waggle_wrapped = True
    CLIVoiceMixin._on_wake_word = on_wake
    return True


def register(ctx):
    global _ctx
    _ctx = ctx
    _wake_starts_conversation()
    vault.init()
    for schema, handler in TOOLS:
        ctx.register_tool(name=schema["name"], toolset="waggle", schema=schema, handler=handler)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    # hold_stream: the user only ever sees the final reply, never the local model's "OK" placeholder.
    try:
        ctx.register_hook("transform_llm_output", on_transform_llm_output, hold_stream=True)
    except TypeError:  # older Hermes without hold_stream
        ctx.register_hook("transform_llm_output", on_transform_llm_output)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_command("waggle", lambda raw: json.dumps(quota.status(), indent=2),
                         description="Waggle quota and status")
