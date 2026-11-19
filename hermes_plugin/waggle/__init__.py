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
                    policy, security, selfbuild, toolkit, traces, vault)
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
    _turn[session_id] = {"t0": time.time(), "platform": platform}
    if session_id and user_message:
        _last_msg[session_id] = user_message
        try:  # "no, I meant..." / "perfect, thanks" -> feedback on Waggle's previous answer
            traces.judge_followup(session_id, user_message)
        except Exception as e:
            log.warning("feedback capture failed: %s", e)
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
    try:  # "like last time" / "what did you say about..." -> the earlier exchange it refers to
        past = traces.context_for(user_message)
        if past:
            ctx += "\n\n" + past
    except Exception as e:
        log.warning("recall failed: %s", e)
    recent = _recent(session_id)
    if recent:
        ctx += "\n\n<recent_conversation>\n" + recent + "\n</recent_conversation>"
    _remember_turn(session_id, "User", user_message)

    # "yes" to an approval an agent asked for -> grant exactly those calls and finish the job.
    if YES.match(user_message) and session_id in _agent_last and policy.pending(session_id):
        granted = policy.grant(session=session_id)
        req, actx, model = _agent_last[session_id]
        note = "The user approved: " + "; ".join(g["summary"] for g in granted) + ". Do those now and finish."
        return _hand_over(session_id, user_message, _run_agent(session_id, req, actx, model, platform, note),
                          {"route": "act", "model": model, "approved": len(granted)})

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
    if not router.wants_connectors(user_message) and (live.needs_live_info(user_message)
                                                          or live.weather_request(user_message)):
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
    meta = {"route": d["route"], "model": d.get("answered_by") or d.get("agent_model") or d.get("by"),
            "specialist": d.get("specialist")}
    if d["route"] == "sonnet" and d.get("reply"):
        return _hand_over(session_id, user_message, d["reply"] + news_text, meta)
    if d["route"] == "act":
        reply = _run_agent(session_id, user_message, ctx, d.get("agent_model") or "haiku", platform,
                           hard=bool(d.get("hard")))
        if reply is not None:
            return _hand_over(session_id, user_message, reply + news_text, meta)
        extras.append("Claude's tool agent is unavailable right now; do the best you can with your own tools.")
    if d["route"] == "opus" and d.get("build_prompt"):
        title = d.get("title") or "Build"
        proj = (projects.find(user_message) or [None])[0]
        job_id = jobs.create(title, d["build_prompt"], user_message, project=proj)
        jobs.launch(job_id)
        msg = (f"On it. I gave this to Opus as a background build: **{title}**.\n"
               f"I'll ping you when it's done (job `{job_id}`). Ask \"build status\" any time.{news_text}")
        return _hand_over(session_id, user_message, msg, {"route": "opus", "model": "opus"})
    _turn[session_id].update(route=d["route"], model="local", specialist=d.get("specialist"))
    if d.get("looked_big"):
        extras.append("Claude is unavailable right now (" + d.get("reason", "") + "). Do the best you can locally, "
                      "and tell the user a stronger version can run once the Claude limit resets.")
    return {"context": "\n\n".join([ctx] + extras)}


# Kept for a turn in flight across a plugin reload; nothing new is queued here since replies are delivered
# directly by _hand_over.
_pending = {}
_pending_lock = threading.Lock()


def _hand_over(session_id, user_message, reply, meta=None):
    """Deliver a Claude-written reply immediately: the turn ends here with no local-model call.

    (The old placeholder trick made the local model write "OK" first, 3-9 s wasted per message.) Hermes stores
    a vetoed turn's user text as "[Message withheld]", but that no longer matters for context: Waggle keeps its
    own transcript (_convo) and hands it to whichever model answers next."""
    _remember_turn(session_id, "Waggle", reply)
    learn.learn_async(user_message, reply)
    meta = meta or {}
    if meta.get("route") != "act":  # agent turns are traced by the agent thread (with their tool steps)
        _trace(session_id, user_message, reply, **meta)
    threading.Thread(target=_refresh_hq, daemon=True).start()
    return {"action": "block", "message": reply}


_turn = {}  # session_id -> {"t0", "platform", route...} for the turn in progress
_agent_last = {}  # session_id -> (request, context, model) of the last agent job (for approvals)
YES = re.compile(r"^\s*(y|yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|approved?|confirm(ed)?|allow( it)?)"
                 r"([\s,!.]+(do it|go ahead|please|you can|approved?))*[\s!.]*$", re.I)


def _trace(session_id, user_message, reply, route="", model="", specialist="", steps=None, ok=True, error="",
           tokens=None, skills_used=None, **extra):
    t = _turn.get(session_id) or {}
    try:
        traces.record(user_message, session=session_id or "", platform=t.get("platform", ""), route=route,
                      model=model or "", specialist=specialist or "", agent=extra.pop("agent", ""),
                      latency_s=round(time.time() - t.get("t0", time.time()), 2), ok=ok, error=error, reply=reply,
                      tokens=tokens, steps=steps, skills_used=skills_used, extra=extra)
    except Exception as e:
        log.warning("trace failed: %s", e)


def _run_agent(session_id, request, ctx, model, platform, approved_note="", hard=False):
    """Run a Claude tool agent. Returns the reply text, or None to let the local model try.
    Jobs that outlast agent_wait_s keep running and send their result to the phone."""
    from waggle import agent as wagent
    from waggle import agents as wagents
    s = Settings.load()
    _agent_last[session_id] = (request, ctx, model)
    name = wagents.choose(request, hard)
    if wagents.AGENTS[name]["model"] == "deep" and model == s.agent_model:
        model = s.agent_deep_model  # coder / researcher / robotics work gets the stronger model
    box, lock = {}, threading.Lock()
    prompt = request + (f"\n\n({approved_note})" if approved_note else "")

    def work():
        try:
            res = wagent.run(prompt, model=model, context=ctx, session=session_id or "", agent_name=name,
                             agent_rules=wagents.instructions(name, s), settings=s,
                             timeout=1800 if name in ("researcher", "coder") else 900)
        except Exception as e:
            res = wagent.AgentResult(error=f"{type(e).__name__}: {e}")
        if not res.text and res.error and ("limit" in res.error.lower() or "not found" in res.error.lower()):
            reply = None
        else:
            reply = (res.text or f"That didn't work: {res.error[:200]}") + wagent.approval_message(res)
        skills_used = [json.loads(st["input"]).get("name", "") for st in res.steps
                       if st["tool"].endswith("skill_load") and st["input"].startswith("{")] if res.steps else []
        _trace(session_id, request, reply or res.error, route="act", model=res.model, steps=res.steps, ok=res.ok,
               error=res.error, tokens=res.usage, skills_used=skills_used, agent=name, turns=res.turns,
               approvals=len(res.approvals))
        with lock:
            box["reply"], box["done"] = reply, True
            late = box.get("late")
        if late and reply:
            _remember_turn(session_id, "Waggle", reply)
            notify.send(reply[:3500])

    th = threading.Thread(target=work, daemon=True, name="waggle-agent")
    th.start()
    th.join(s.agent_wait_s)
    with lock:
        if box.get("done"):
            return box["reply"]
        box["late"] = True
    return "On it. This one takes a bit longer, so I'll message you the result when it's done."


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


def on_post_llm_call(user_message="", assistant_response="", session_id="", **kwargs):
    if user_message and assistant_response:
        learn.learn_async(user_message, assistant_response)
        t = _turn.pop(session_id, None) or {}
        if t.get("route") and t.get("platform") not in SKIP_ROUTING_PLATFORMS:
            _turn[session_id] = t
            _trace(session_id, user_message, assistant_response, route=t["route"], model="local",
                   specialist=t.get("specialist") or "")
            _turn.pop(session_id, None)
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
    return _targeting(tool_name, args, session_id)


def _targeting(tool_name, args, session_id):
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


# ---- tools: defined once in waggle.toolkit (also served to Claude agents over MCP) -------------------

TOOLS = toolkit.TOOLS
_call_me = toolkit.call_me_text


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


def _list_tools():
    """Every Hermes tool whose requirements are met, as {name, description, parameters}."""
    from tools.registry import registry
    out = []
    for d in registry.get_definitions(set(registry.get_all_tool_names()), quiet=True):
        f = d.get("function", d)
        out.append({"name": f.get("name"), "description": (f.get("description") or "")[:1500],
                    "parameters": f.get("parameters") or {"type": "object", "properties": {}}})
    return out


def _bridge_pre(name, args):
    """Same computer-use targeting as for the local model (session 'bridge')."""
    if name != "computer_use":
        return None
    return _targeting(name, args, "bridge")


def _startup_upkeep():
    """Once per gateway start: resume builds a crash or reboot interrupted, refresh skills, default routines."""
    time.sleep(20)
    for name, fn in (("recover jobs", lambda: jobs.recover()), ("routines", _ensure_routines),
                     ("skills", lambda: __import__("waggle.skills", fromlist=["x"]).ensure_index())):
        try:
            fn()
        except Exception as e:
            log.warning("startup upkeep (%s) failed: %s", name, e)


def _ensure_routines():
    from waggle import routines
    routines.ensure_defaults()


def _start_bridge():
    """Let Claude agents use Hermes's tools through this gateway (127.0.0.1 + token, see waggle/bridge.py).
    Only the gateway hosts it: an interactive `hermes chat` window would otherwise steal the port file."""
    if "gateway" not in " ".join(sys.argv).lower():
        return
    try:
        from waggle import bridge
        port, _ = bridge.start(_dispatch, _list_tools, pre=_bridge_pre)
        log.info("waggle bridge on 127.0.0.1:%s", port)
        threading.Thread(target=_startup_upkeep, daemon=True, name="waggle-upkeep").start()
    except Exception as e:
        log.warning("waggle bridge failed to start: %s", e)


def register(ctx):
    global _ctx
    _ctx = ctx
    toolkit.set_dispatch(_dispatch)
    _wake_starts_conversation()
    _start_bridge()
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
