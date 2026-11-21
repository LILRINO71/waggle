"""Hermes plugin that turns Hermes into Waggle: a thin adapter around Waggle's turn engine.

Every user turn (pre_llm_call), the hook returns in well under a second:
  own mode (Telegram, when the platform allows silence): a worker thread runs the turn in the engine
      (waggle/chat_engine.py: the chat's long-lived Claude process, streamed into the chat with post_message +
      edit), and the hook returns a [SILENT] block, so Hermes's own model never answers and Hermes sends nothing.
  sync mode (desktop CLI, local voice, Telegram voice notes, or silence not allowed): the hook waits up to
      settings.sync_wait_s for the engine's answer and returns it as the block message (Hermes speaks/sends it);
      past that it acknowledges and the answer follows through post_message or the outbox.
  engine off (settings.engine_enabled = False): the old router path (_legacy_pre_llm_call) below.
Background results and failed deliveries wait in the core outbox (runtime.db); the core's sender
delivers them.
Hermes's local model answers only when the hook could not run (crash, timeout, skip); that reply is labelled
and traced. Before every tool call: guardrails (messaging, money, destructive commands need your OK).
"""
import contextlib
import contextvars
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


_fillers = {}  # session_id -> voice_ack.Filler for the turn in progress


def _voice_active(platform):
    """True when the desktop voice window is in voice mode (spoken replies)."""
    if platform not in ("cli", "", None) or _ctx is None:
        return False
    cli = getattr(getattr(_ctx, "_manager", None), "_cli_ref", None)
    return bool(getattr(cli, "_voice_mode", False))


SILENT = "[SILENT]"  # Hermes delivers nothing for this block message when the platform has allow_silence: true
LATE_ACK = "This one is taking a little longer. I'll post the answer here as soon as it's ready."


# ---- the off switch: while it is set the gateway answers one line and starts no turn ---------------------------

ON_CMD = re.compile(r"^\s*(?:/on|/?waggle\s+on)\s*[.!]?\s*$", re.I)


def _switch_on():
    try:
        from waggle.core import control
        control.on(by="telegram")
    except Exception:
        log.exception("waggle switch-on from the gateway failed")


def _off_reply(text, platform=""):
    """The reply to give while Waggle is switched off (waggle/core/offswitch.py), or None when it is on. "/on" (or
    "waggle on") switches it back on. Only called for messages Hermes already admitted, so only the owner reaches
    it. Any failure here returns None: a broken switch check must never silence the old path."""
    if platform in SKIP_ROUTING_PLATFORMS:
        return None
    try:
        from waggle.core import offswitch
        state = offswitch.read()
        if not state.active():
            return None
        if ON_CMD.match(text or ""):
            threading.Thread(target=_switch_on, daemon=True, name="waggle-switch-on").start()
            return "Switching Waggle back on now."
        return offswitch.gateway_line(state)
    except Exception as e:
        log.warning("waggle off-switch check failed: %s", e)
        return None


def on_pre_llm_call(session_id=None, user_message="", platform="", **kwargs):
    """Entry point for every turn. Never raises: a crash is traced and Hermes's model answers (labelled)."""
    t0 = time.time()
    try:
        off_line = _off_reply(user_message, platform)
        if off_line:
            return {"action": "block", "message": off_line}
        if not Settings.load().engine_enabled:
            return _legacy_pre_llm_call(session_id, user_message, platform, **kwargs)
        return _engine_pre_llm(session_id, user_message, platform, t0)
    except Exception as e:
        log.exception("waggle turn hook crashed")
        events.log("hook_crash", error=f"{type(e).__name__}: {e}"[:300], platform=platform)
        _turn[session_id] = {"t0": t0, "platform": platform, "crashed": f"{type(e).__name__}: {e}"[:200]}
        return None
    finally:
        took = time.time() - t0
        if took > 5:
            events.log("hook_slow", seconds=round(took, 2), platform=platform)


# ---- the engine path ---------------------------------------------------------------------------------------

def _address():
    """The Hermes route key of the chat this hook fires for (stable across Hermes session-id rotation)."""
    try:
        from gateway.session_context import bound_session_key
        return bound_session_key() or ""
    except Exception:
        return ""


def _chat_id(platform, session_id, address):
    return address or f"{platform or 'chat'}:{session_id or 'default'}"


_silence_cache = {}


def _silence_allowed(platform):
    """True when Hermes is configured to deliver nothing for a [SILENT] final on this platform
    (platforms.<platform>.allow_silence: true). Without it a silent block would become an error notice."""
    hit = _silence_cache.get(platform)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    ok = False
    try:
        from hermes_cli.config import load_config_readonly
        pcfg = ((load_config_readonly() or {}).get("platforms") or {}).get(platform) or {}
        ok = pcfg.get("allow_silence") is True or (pcfg.get("extra") or {}).get("allow_silence") is True
    except Exception:
        ok = False
    _silence_cache[platform] = (ok, time.time())
    return ok


def _mode(platform, voice, s):
    if (platform in (s.stream_platforms or []) and not voice and _ctx is not None
            and hasattr(_ctx, "post_message") and _silence_allowed(platform)):
        return "own"
    return "sync"


class HermesDelivery:
    """waggle.delivery.Delivery over Hermes's plugin messages: post_message() + handle.edit().
    `address` (the Hermes session key) lets a thread outside the turn reach the same chat."""
    can_edit = True
    max_len = 4000
    name = "hermes"

    def __init__(self, ctx, address=""):
        self.ctx, self.address, self.last_error = ctx, address, ""

    def _scope(self):
        if not self.address:
            return contextlib.nullcontext()
        try:
            from gateway.session_context import scoped_session_key
            return scoped_session_key(self.address)
        except Exception:
            return contextlib.nullcontext()

    def post(self, text):
        with self._scope():
            h = self.ctx.post_message(text)
        if getattr(h, "ok", False):
            return h
        self.last_error = str(getattr(h, "error", "") or "post refused")
        log.warning("waggle post_message failed: %s", self.last_error)
        return None

    def edit(self, handle, text):
        with self._scope():
            r = handle.edit(text)
        ok = bool((r or {}).get("ok"))
        if not ok:
            self.last_error = str((r or {}).get("error") or "edit refused")
        return ok

    def send_file(self, path, caption=""):
        return False  # files go through send_to_me (hermes send) from the brain

    def send_voice(self, path, caption="", duration=None):
        return False

    def typing(self):
        return None


def _engine_pre_llm(session_id, user_message, platform, t0):
    from waggle import chat_engine, voice_turn
    s = Settings.load()
    m = FEEDBACK.match(user_message or "")
    if m:
        useful = m.group(1).lower() in ("useful", "👍")
        events.log("feedback", finding=m.group(2), useful=useful, platform=platform)
        return {"action": "block", "message": "Noted, thanks. " + ("I'll look for more like that." if useful
                                                                   else "I'll be pickier about those.")}
    text, kind = voice_turn.parse(user_message, s.voice_fixes)
    if kind == "voice_failed":
        events.log("voice_failed", platform=platform)
        return {"action": "block", "message": voice_turn.FAILED_REPLY}
    if platform in SKIP_ROUTING_PLATFORMS or not text:
        return {"context": memory.context_block(text or "")}
    _reindex_soon()
    voice = kind == "voice"
    address = _address()
    chat = _chat_id(platform, session_id, address)
    origin = chat_engine.origin_for_message(platform, text)  # owner's chat platform is "user"; CLI and "[waggle-test]" are "test"
    prev_msg_at = _last_msg_at.get(chat, 0.0)
    _last_msg[session_id] = text
    _last_msg_at[chat] = time.time()
    try:  # the user is here: background Claude work gives way for a minute
        __import__("waggle.budget", fromlist=["x"]).note_interactive()
    except Exception as e:
        log.warning("budget note failed: %s", e)
    if origin == "user":
        try:  # "no, I meant..." / "perfect, thanks" -> feedback on Waggle's previous answer
            traces.judge_followup(chat, text)
        except Exception as e:
            log.warning("feedback capture failed: %s", e)
    threading.Thread(target=_upkeep_after_message, daemon=True, name="waggle-turn-upkeep").start()
    _turn[session_id] = {"t0": t0, "platform": platform, "voice": voice, "owned": True, "chat": chat}

    def direct(reply, route):
        chat_engine.record_direct(chat, text, reply, platform=platform, origin=origin, t0=t0, route=route)
        return {"action": "block", "message": reply}

    mo = OBJECTIVE_CMD.match(text)
    if mo:
        return direct(_start_objective(mo.group(1).strip()), "objective")
    pre = chat_engine.approval_preflight(chat, text, prev_msg_at)
    if pre and pre[0] == "reply":
        return direct(pre[1], "approval")
    note = pre[1] if pre else ""
    _start_reaper()
    if _mode(platform, voice, s) == "own":
        delivery = HermesDelivery(_ctx, address)
        run = contextvars.copy_context().run  # post_message reads this turn's chat from the context
        threading.Thread(target=run, args=(_own_turn, chat, text, delivery, platform, t0, note, address, origin),
                         daemon=True, name="waggle-turn").start()
        return {"action": "block", "message": SILENT}
    return _sync_turn(chat, text, platform, voice, t0, note, address, s, origin)


CRASHED = "Something went wrong answering that ({what}). Please send it again."


def _own_turn(chat, text, delivery, platform, t0, note, address, origin="user"):
    from waggle import chat_engine
    try:
        rec = chat_engine.run_turn(chat, text, delivery, platform=platform, origin=origin, t0=t0, note=note,
                                   address=address)
        if origin == "user":
            learn.learn_async(text, rec.reply)
    except Exception as e:  # never silent: the user gets told (or it waits in the outbox), the trace says why
        log.exception("waggle turn failed")
        rec = chat_engine.TurnRecord(chat=chat, text=text, origin=origin, platform=platform, failure_notice=True,
                                     fallback_reason=f"crash: {type(e).__name__}: {e}"[:300])
        rec.reply = CRASHED.format(what=type(e).__name__)
        try:
            h = delivery.post(rec.reply)
            rec.delivered = h is not None and h is not False
        except Exception:
            rec.delivered = False
        rec.delivery = delivery.name if rec.delivered else "outbox"
        if not rec.delivered:
            rec.outbox_id = chat_engine.queue_reply(rec, address=address)
        rec.total_s = round(time.time() - t0, 2)
        chat_engine.record_turn(rec.settle())
    threading.Thread(target=_refresh_hq, daemon=True).start()


def _sync_turn(chat, text, platform, voice, t0, note, address, s, origin="user"):
    """Surfaces that show the hook's block message (and speak it, for voice): wait for the whole answer.
    Past settings.sync_wait_s the hook acknowledges and the answer goes to the core outbox."""
    from waggle import chat_engine
    from waggle.delivery import CollectDelivery, plain_text
    filler = None
    if voice and _voice_active(platform):
        from waggle import voice_ack
        filler = voice_ack.Filler(delay=2.0, enabled=True)
    box, lock, done = {}, threading.Lock(), threading.Event()

    def work():
        rec = chat_engine.run_turn(chat, text, CollectDelivery(), platform=platform, origin=origin, voice=voice,
                                   t0=t0, note=note, address=address, stream=False, record=False)
        with lock:
            box["rec"] = rec
            late = box.get("late")
        done.set()
        if late:  # the hook already answered with LATE_ACK: the reply waits in the core outbox
            rec.delivered, rec.delivery, rec.ttfw_s, rec.message_ids = False, "outbox", None, []
            rec.outbox_id = chat_engine.queue_reply(rec, address=address)
            if rec.outbox_id is None:
                rec.delivery = "none"
            rec.total_s = round(time.time() - t0, 2)
            chat_engine.record_turn(rec.settle(), voice=voice, note=note)
        if origin == "user":
            learn.learn_async(text, rec.reply)

    run = contextvars.copy_context().run
    threading.Thread(target=run, args=(work,), daemon=True, name="waggle-turn").start()
    finished = done.wait(s.sync_wait_s)
    with lock:
        if not finished:
            box["late"] = True
        rec = box.get("rec")
    if filler:
        filler.cancel()
    if not finished or rec is None:
        return {"action": "block", "message": LATE_ACK}
    reply = rec.reply if platform in ("cli", "", None) else plain_text(rec.reply)
    rec.delivered, rec.delivery, rec.outbox_id = True, "block", None
    rec.ttfw_s = rec.total_s = round(time.time() - t0, 2)
    chat_engine.record_turn(rec.settle(), voice=voice, note=note)
    threading.Thread(target=_refresh_hq, daemon=True).start()
    return {"action": "block", "message": reply}


def _upkeep_after_message():
    try:
        jobs.resume_paused()
    except Exception as e:
        log.warning("resume_paused failed: %s", e)


# ---- idle chat processes are stopped (their sessions resume on the next message) ---------------------------
# Undelivered replies and background results wait in the core outbox (waggle.core.outbox); the core's sender
# delivers them. The gateway does not drain that outbox: two senders would race for the same rows.

_reaper_started = False


def _reap_loop():
    from waggle import chat_engine
    while True:
        time.sleep(60)
        try:
            chat_engine.pool().reap()
        except Exception as e:
            log.warning("chat session reap failed: %s", e)


def _start_reaper():
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True
    threading.Thread(target=_reap_loop, daemon=True, name="waggle-reaper").start()


# ---- the old router path (engine_enabled = False) ------------------------------------------------------------

def _legacy_pre_llm_call(session_id=None, user_message="", platform="", **kwargs):
    """Voice turns that take a moment get a natural "One sec." (waggle/voice_ack.py) while Waggle works."""
    from waggle import voice_ack
    filler = voice_ack.Filler(delay=2.0, enabled=bool(user_message) and _voice_active(platform))
    _fillers[session_id] = filler
    try:
        return _pre_llm(session_id, user_message, platform, **kwargs)
    finally:
        filler.cancel()
        _fillers.pop(session_id, None)


def _pre_llm(session_id=None, user_message="", platform="", **kwargs):
    m = FEEDBACK.match(user_message or "")
    if m:
        useful = m.group(1).lower() in ("useful", "👍")
        events.log("feedback", finding=m.group(2), useful=useful, platform=platform)
        return {"action": "block", "message": "Noted, thanks. " + ("I'll look for more like that." if useful
                                                                   else "I'll be pickier about those.")}
    # A voice note arrives as '"<transcript>"\n[User sent audio: <path>]': answer what was said, not the wrapper.
    from waggle import voice_turn
    user_message, kind = voice_turn.parse(user_message, Settings.load().voice_fixes)
    if kind == "voice_failed":
        events.log("voice_failed", platform=platform)
        return {"action": "block", "message": voice_turn.FAILED_REPLY}
    _reindex_soon()
    _turn[session_id] = {"t0": time.time(), "platform": platform, "voice": kind == "voice"}
    prev_msg_at = _last_msg_at.get(session_id, 0.0)
    if session_id and user_message:
        _last_msg[session_id] = user_message
        _last_msg_at[session_id] = time.time()
        try:  # the user is here: background Claude work gives way for a minute
            __import__("waggle.budget", fromlist=["x"]).note_interactive()
        except Exception as e:
            log.warning("budget note failed: %s", e)
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
    try:  # people/projects/decisions the message mentions -> their connections (knowledge graph)
        g = __import__("waggle.graph", fromlist=["x"]).context_for(user_message)
        if g:
            ctx += "\n\n" + g
    except Exception as e:
        log.warning("graph context failed: %s", e)
    try:  # "like last time" / "what did you say about..." -> the earlier exchange it refers to
        past = traces.context_for(user_message)
        if past:
            ctx += "\n\n" + past
    except Exception as e:
        log.warning("recall failed: %s", e)
    _restore_convo(session_id)
    recent = _recent(session_id)
    if _turn.get(session_id, {}).get("voice"):
        ctx += "\n\n" + voice_turn.VOICE_HINT
    if len(_convo.get(session_id, [])) < 2:  # new conversation here: carry over what was just said elsewhere
        try:
            other = traces.recent_elsewhere(session_id)
            if other:
                ctx += "\n\n" + other
        except Exception as e:
            log.warning("cross-interface context failed: %s", e)
    if recent:
        ctx += "\n\n<recent_conversation>\n" + recent + "\n</recent_conversation>"
        if re.search(r"approv", recent, re.I):  # codes quoted earlier may have expired or been used since
            ctx += "\n\n" + _approvals_now(session_id)
    _remember_turn(session_id, "User", user_message)

    # "objective: <goal>" -> a persistent background objective, planned now and worked on until done.
    m = OBJECTIVE_CMD.match(user_message)
    if m:
        return _hand_over(session_id, user_message, _start_objective(m.group(1).strip()), {"route": "objective"})
    approval = _approval_turn(session_id, user_message, platform, prev_msg_at)
    if approval:
        reply, meta = approval
        return _hand_over(session_id, user_message, reply, meta)
    _agent_last.pop(session_id, None)  # any other message: an old agent question no longer gets a bare "yes"

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
    d = router.decide(user_message, ctx)  # (route lines no longer go to the vault Journal: they polluted memory)
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
    if _origin((_turn.get(session_id) or {}).get("platform", "")) == "user":
        learn.learn_async(user_message, reply)  # only the owner's own words may teach Waggle (plan decision 10)
    meta = meta or {}
    if meta.get("route") != "act":  # agent turns are traced by the agent thread (with their tool steps)
        _trace(session_id, user_message, reply, **meta)
    threading.Thread(target=_refresh_hq, daemon=True).start()
    return {"action": "block", "message": reply}


OBJECTIVE_CMD = re.compile(r"^\s*(?:objective|goal|mission)\s*:\s*(.{8,})$", re.I | re.S)


def _start_objective(goal):
    from waggle import objectives
    try:
        oid = objectives.create(goal, project=(projects.find(goal) or [None])[0], origin="user")
    except Exception as e:
        return f"I couldn't start that objective: {e}"
    st = objectives.status(oid) or {}
    if st.get("status") != "active":
        return f"Objective saved but not planned yet ({st.get('error') or st.get('status')}). I'll retry shortly."
    objectives.kick(oid)
    plan = "\n".join(f"{x['idx']}. {x['title']} ({x['agent']})" for x in st.get("steps", []))
    return (f"On it: **{st.get('title')}** (objective `{oid}`). The plan:\n{plan}\n\nI'm working through it in the "
            f"background and will message you when it's done or if I need you.")


_turn = {}  # session_id -> {"t0", "platform", route...} for the turn in progress
_agent_last = {}  # session_id -> (request, context, model) of the last agent job (for approvals)
_last_msg_at = {}  # session_id -> time of the user's previous message (is an approval request newer than it?)
APPROVAL_CMD = re.compile(r"^\s*(approve|allow|ok|yes|deny|reject|refuse|no)\s+#?([A-Za-z0-9]{4})\s*[.!]*\s*$", re.I)


def _approval_turn(session_id, user_message, platform, prev_msg_at):
    """Approval answers. Returns (reply, trace meta) when the message was one, else None.
    - "approve K7QH" / "deny K7QH": exactly that one action, whichever chat or objective asked for it.
    - a bare "yes" grants only what THIS chat was just asked (the agent's approval request in its previous reply).
    - a bare "yes" while background work waits never approves it: if a request arrived since the user's last
      message here, the reply lists the open requests with their codes instead."""
    m = APPROVAL_CMD.match(user_message)
    if m and (m.group(1).lower() not in ("yes", "ok", "no") or policy.get(m.group(2))):  # "ok then" isn't a code
        return _answer_approval(m.group(1), m.group(2), session_id, platform), {"route": "approval"}
    if not YES.match(user_message):
        return None
    if session_id in _agent_last and policy.pending(session_id):
        granted = policy.grant(session=session_id)
        req, actx, model = _agent_last[session_id]
        note = "The user approved: " + "; ".join(g["summary"] for g in granted) + ". Do those now and finish."
        return (_run_agent(session_id, req, actx, model, platform, note),
                {"route": "act", "model": model, "approved": len(granted)})
    bg = [p for p in policy.pending() if str(p.get("session", "")).startswith(("objective-", "routine-"))]
    if any(p["created"] > prev_msg_at for p in bg):
        return _approval_list(bg), {"route": "approval"}
    return None


def _approvals_now(session_id):
    """What is really open right now, so a reply never points the user at an expired or used code."""
    from waggle import policy
    try:
        live = policy.pending(session_id)
    except Exception as e:
        log.warning("approval check failed: %s", e)
        return ""
    if not live:
        return ("<approvals_now>No approval is open for this chat right now: any code mentioned earlier has expired "
                "or been used, so don't ask the user to approve it. If they still want that action, it has to be "
                "requested again (sending to their own email or here needs no approval).</approvals_now>")
    return "<approvals_now>Open approvals: " + "; ".join(f"{a['code']} ({a['reason']})" for a in live) + \
        "</approvals_now>"


def _approval_list(items):
    from waggle import policy
    lines = "\n".join("• " + policy.describe(p) + _whose(p) for p in items[:6])
    one = items[0]["code"]
    return (f"To be safe I don't approve on a plain yes while background work is waiting. Open requests:\n{lines}\n"
            f'Reply "approve {one}" (or "deny {one}") for the one you mean. If your yes was about something else, '
            "say it again in other words.")


def _whose(p):
    sess = str(p.get("session", ""))
    if sess.startswith("objective-"):
        try:
            from waggle import objectives
            st = objectives.status(sess.split("-", 1)[1]) or {}
            return f" — objective '{st.get('title', sess)}'"
        except Exception:
            return " — an objective"
    return " — routine" if sess.startswith("routine-") else ""


def _answer_approval(verb, code, session_id, platform):
    """Approve or deny exactly one approval by its code. Expired, used or decided codes do nothing."""
    from waggle import objectives, policy
    item = policy.get(code)
    if not item:
        return f"I don't have an approval with code {code.upper()}. Nothing was done."
    if item["state"] != "pending" or item["expires"] <= time.time():
        state = "expired" if item["state"] == "pending" else item["state"]
        return f"Approval {item['code']} is no longer open ({state}). Nothing was done."
    sess = item.get("session") or ""
    if verb.lower() in ("deny", "reject", "refuse", "no"):
        policy.deny(item["id"])
        if sess.startswith("objective-"):
            objectives.settle_approvals()
        return f"Denied {item['code']}: {item['summary']}. It won't run."
    granted = policy.grant(item["id"])
    if not granted:
        return f"Approval {item['code']} could not be granted (it changed meanwhile). Nothing was done."
    if sess.startswith("objective-"):
        oid = objectives.resume_after_approval(sess, granted)
        if oid:
            title = (objectives.status(oid) or {}).get("title", oid)
            return f"Approved once: {item['summary']}. Continuing objective '{title}'."
        left = policy.pending(sess)
        return (f"Approved once: {item['summary']}. That objective still waits on: "
                + "; ".join(policy.describe(p) for p in left[:3]))
    if sess and sess == session_id and session_id in _agent_last:
        req, actx, model = _agent_last[session_id]
        note = f"The user approved: {item['summary']}. Do that now and finish."
        return _run_agent(session_id, req, actx, model, platform, note) or f"Approved once: {item['summary']}."
    return f"Approved once: {item['summary']}. It will be allowed the next time exactly that action runs."
YES = re.compile(r"^\s*(y|yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|approved?|confirm(ed)?|allow( it)?)"
                 r"([\s,!.]+(do it|go ahead|please|you can|approved?))*[\s!.]*$", re.I)


def _origin(platform):
    """user | test | canary | bench | agent for a message arriving on `platform` (chat_engine.origin_for)."""
    from waggle import chat_engine
    return chat_engine.origin_for(platform)


def _trace(session_id, user_message, reply, route="", model="", specialist="", steps=None, ok=True, error="",
           tokens=None, skills_used=None, **extra):
    t = _turn.get(session_id) or {}
    try:
        traces.record(user_message, session=session_id or "", platform=t.get("platform", ""), route=route,
                      model=model or "", specialist=specialist or "", agent=extra.pop("agent", ""),
                      latency_s=round(time.time() - t.get("t0", time.time()), 2), ok=ok, error=error, reply=reply,
                      tokens=tokens, steps=steps, skills_used=skills_used, extra=extra,
                      origin=_origin(t.get("platform", "")))
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
    if wagents.AGENTS[name]["model"] == "deep" and model in (s.agent_model, s.chat_agent_model):
        model = s.chat_deep_model or s.agent_deep_model  # coder / researcher / robotics work gets the stronger model
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

    if session_id in _fillers:
        _fillers[session_id].now("act")  # "On it." right away: tool jobs take a few seconds
    th = threading.Thread(target=work, daemon=True, name="waggle-agent")
    th.start()
    th.join(s.agent_wait_s)
    with lock:
        if box.get("done"):
            return box["reply"]
        box["late"] = True
    return "On it. This one takes a bit longer, so I'll message you the result when it's done."


_convo = {}  # session_id -> recent (speaker, text) pairs
_CONVO_TURNS = 16  # 8 exchanges
_TURN_CHARS = {"User": 1500, "Waggle": 2500}  # long enough that "that" in a follow-up still points at something


def _remember_turn(session_id, who, text):
    if not session_id or not text:
        return
    _restore_convo(session_id)
    turns = _convo.setdefault(session_id, [])
    turns.append((who, " ".join(str(text).split())[:_TURN_CHARS.get(who, 1500)]))
    del turns[:-_CONVO_TURNS]


def _restore_convo(session_id):
    """After a gateway restart the in-memory history is empty: rebuild it from this session's traces once."""
    if not session_id or session_id in _convo:
        return
    turns = _convo.setdefault(session_id, [])
    try:
        for req, reply in traces.session_turns(session_id, n=_CONVO_TURNS // 2):
            turns.append(("User", " ".join(req.split())[:_TURN_CHARS["User"]]))
            turns.append(("Waggle", " ".join(reply.split())[:_TURN_CHARS["Waggle"]]))
    except Exception as e:
        log.warning("conversation restore failed: %s", e)


def _recent(session_id):
    return "\n".join(f"{who}: {text}" for who, text in _convo.get(session_id, []))


LOCAL_NOTE = "\n\n(Answered by Waggle's local model: {why}.)"


def _is_silence(text):
    return (text or "").strip().upper() in ("[SILENT]", "SILENT", "NO_REPLY", "NO REPLY")


def on_transform_llm_output(response_text="", session_id="", platform="", **kwargs):
    t = _turn.get(session_id) or {}
    if t.get("owned"):
        return None  # Waggle's own block ([SILENT] or a direct reply): leave it exactly as is
    if Settings.load().engine_enabled and t.get("platform", platform) not in SKIP_ROUTING_PLATFORMS:
        # Hermes's local model answered a user turn: only when the Waggle hook crashed, timed out or was skipped.
        why = ("Waggle's turn handler failed" if t.get("crashed") else
               "Waggle's turn handler didn't run for this message")
        if _is_silence(response_text) or not (response_text or "").strip():
            return "Sorry, that message slipped past my main brain and the local model had no answer. Please send it again."
        return response_text + LOCAL_NOTE.format(why=why)
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


def on_post_llm_call(user_message="", assistant_response="", session_id="", platform="", **kwargs):
    t = _turn.get(session_id) or {}
    if Settings.load().engine_enabled:
        if t.get("owned"):  # the engine traced this turn itself
            _turn.pop(session_id, None)
            return
        if user_message and platform not in SKIP_ROUTING_PLATFORMS:
            # The truth record for a turn Hermes's own model answered: the hook crashed, timed out or was skipped.
            reason = (f"hook crashed: {t['crashed']}" if t.get("crashed") else
                      "hook skipped or timed out (Hermes's model answered)")
            try:
                from waggle import chat_engine
                outcome, why = traces.turn_outcome(answered_by="local", delivered=True,
                                                   failure_notice=not assistant_response)
                traces.record(str(user_message)[:20000], session=session_id or "", platform=platform or "",
                              route="hermes", model="local", reply=assistant_response or "", ok=False,
                              error=reason, chat=t.get("chat"), origin=chat_engine.origin_for(platform),
                              answered_by="local", fallback_reason=reason, delivered=True, delivery="hermes",
                              turn_outcome=outcome, outcome_why=why,
                              total_s=round(time.time() - t["t0"], 2) if t.get("t0") else None)
            except Exception as e:
                log.warning("trace failed: %s", e)
            events.log("local_answered", reason=reason, platform=platform)
        _turn.pop(session_id, None)
        threading.Thread(target=_refresh_hq, daemon=True).start()
        return
    if user_message and assistant_response:
        if _origin(platform or (_turn.get(session_id) or {}).get("platform", "")) == "user":
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


def _prepare_fillers():
    try:
        from waggle import voice_ack
        voice_ack.prepare()
    except Exception as e:
        log.warning("voice fillers not prepared: %s", e)


def _startup_upkeep():
    """Once per gateway start: resume builds a crash or reboot interrupted, refresh skills, default routines."""
    time.sleep(20)
    for name, fn in (("recover jobs", lambda: jobs.recover()), ("routines", _ensure_routines),
                     ("objectives", lambda: __import__("waggle.objectives", fromlist=["x"]).supervise()),
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


# ---- admission: typed Telegram text in an engine chat goes to the core's turn engine ----------------------
# Hermes fires post_gateway_admission once per admitted message, before it starts a turn. For a chat listed in
# settings.engine_chats (off by default) Waggle posts a plain typed text message to the core's /turn and returns
# {"action": "handled"} only when the core answers 202, so the gateway never starts its own turn. A voice note
# of the owner's direct chat goes the same way (kind "voice", the downloaded audio file as media): Hermes
# transcribes AFTER this hook, so the engine transcribes the file itself (waggle/stt.py, the same local model).
# Photos, documents (images included), audio files and stickers go too (kind "photo" / "document" / "audio" /
# "sticker", settings.engine_media): the media items carry Hermes's cached local file when there is one and
# Telegram's file_id, name, mime type and size, so the core saves the file into its own per-chat media folder
# (waggle/media_intake.py: copied, or downloaded through the Bot API's getFile when the local copy is missing).
# The caption (the event's text without any file content Hermes inlined) is the message text: the owner's own words.
# Slash commands never reach this hook (Hermes dispatches them first), so pre_gateway_dispatch sends the ones
# Waggle serves itself (waggle/engine_commands.py: /waggle ...) to the core as kind "command" and drops the
# event (action "skip"). Everything else (other chats and platforms, groups, topics, videos, other /commands,
# approval replies, feedback and objective commands), and any failure to reach the core, returns None: the old
# path answers as before. The admission payload has no message type, so pre_gateway_dispatch records each
# event's type and media first; a message without that record is never routed.

_seen_types = {}  # message id -> (message type, has media), from pre_gateway_dispatch
_seen_audio = {}  # message id -> the audio file paths of that event (voice notes)
_seen_meta = {}  # message id -> {"caption", "items", "emoji"} of a photo / document / audio / sticker event
MEDIA_TYPES = ("photo", "document", "audio", "sticker")
MEDIA_ITEMS_MAX = 10
CAPTION_TEXT_ATTR = "_telegram_caption_text"  # Hermes's name for an event's text without inlined file content
_seen_lock = threading.Lock()
SEEN_MAX = 500
CORE_POST_TIMEOUT_S = 2.0


def _source_dict(src):
    """A message source (a dict, or Hermes's SessionSource) as a plain dict."""
    if isinstance(src, dict):
        return src
    try:
        return dict(src.to_dict())
    except Exception:
        pass
    out = {}
    for k in ("platform", "chat_id", "chat_type", "user_id", "thread_id"):
        v = getattr(src, k, None)
        out[k] = getattr(v, "value", v)
    return out


def _raw_media(raw):
    """What Telegram's own message says about its one attachment: {"file_id", "name", "mime", "size", "duration",
    "emoji"}; empty when there is nothing. Read by attribute name only, so any message-like object works."""
    if raw is None:
        return {}
    try:
        photo = getattr(raw, "photo", None)
        if photo:
            big = list(photo)[-1]  # PhotoSize list: the largest last
            return {"file_id": getattr(big, "file_id", None), "size": getattr(big, "file_size", None),
                    "name": "photo.jpg", "mime": "image/jpeg"}
        sticker = getattr(raw, "sticker", None)
        if sticker:
            return {"emoji": getattr(sticker, "emoji", "") or ""}
        for attr in ("document", "audio"):
            f = getattr(raw, attr, None)
            if f:
                return {"file_id": getattr(f, "file_id", None), "size": getattr(f, "file_size", None),
                        "name": getattr(f, "file_name", None) or getattr(f, "title", None) or "",
                        "mime": getattr(f, "mime_type", None) or "", "duration": getattr(f, "duration", None)}
    except Exception:
        pass
    return {}


def _media_meta(event, mtype):
    """The record the admission hook needs for a photo / document / audio / sticker event, else None."""
    if mtype not in MEDIA_TYPES:
        return None
    raw = getattr(event, "raw_message", None)
    rm = _raw_media(raw)
    if mtype == "sticker":
        return {"caption": "", "items": [], "emoji": rm.get("emoji", "")}
    flags = list(getattr(event, "media_text_inlined", None) or [])
    attrs = getattr(event, "__dict__", {}) or {}
    if attrs.get(CAPTION_TEXT_ATTR) is not None:
        caption = attrs[CAPTION_TEXT_ATTR]  # Hermes's own: the text without the file content it inlined
    elif any(f is True for f in flags):
        caption = getattr(raw, "caption", None) or ""  # file content is in event.text: only Telegram's caption is words
    else:
        caption = getattr(event, "text", "") or ""
    types = list(getattr(event, "media_types", None) or [])
    items = []
    for i, p in enumerate(str(p) for p in (getattr(event, "media_urls", None) or []) if p):
        if Path(p).is_file():
            items.append({"path": p, "kind": mtype, "mime": types[i] if i < len(types) else ""})
    if len(items) <= 1 and rm.get("file_id"):  # one attachment: Telegram's own details, and a way to download it
        base = items[0] if items else {"kind": mtype}
        for k in ("file_id", "size", "name", "duration"):
            if rm.get(k):
                base[k] = rm[k]
        if rm.get("mime") and not base.get("mime"):
            base["mime"] = rm["mime"]
        items = [base]
    return {"caption": str(caption).strip(), "items": items, "emoji": ""}


def on_pre_gateway_dispatch(event=None, **kwargs):
    """Remember what kind of message this is (the admission hook does not get it), and hand the owner's /commands
    that Waggle serves itself to the core. Dispatch is only ever changed for such a command, and only when the
    core accepted it (the event is then skipped); otherwise Hermes answers as before."""
    try:
        mid = getattr(event, "message_id", None)
        if mid is None:
            return None
        mtype = getattr(getattr(event, "message_type", None), "value", None) or str(getattr(event, "message_type", ""))
        paths = [str(p) for p in (getattr(event, "media_urls", None) or []) if p]
        meta = _media_meta(event, str(mtype).lower())
        with _seen_lock:
            _seen_types[str(mid)] = (str(mtype).lower(), bool(paths))
            _seen_audio[str(mid)] = paths
            if meta is not None:
                _seen_meta[str(mid)] = meta
            while len(_seen_types) > SEEN_MAX:
                _seen_types.pop(next(iter(_seen_types)))
            while len(_seen_audio) > SEEN_MAX:
                _seen_audio.pop(next(iter(_seen_audio)))
            while len(_seen_meta) > SEEN_MAX:
                _seen_meta.pop(next(iter(_seen_meta)))
        return _serve_command(event, mid, str(mtype).lower())
    except Exception as e:
        log.warning("waggle pre_gateway_dispatch record failed: %s", e)
    return None


def _serve_command(event, mid, mtype):
    """{"action": "skip"} when the owner's command is one Waggle answers and the core took it, else None."""
    text = (getattr(event, "text", "") or "").strip()
    if mtype not in ("text", "command") or not text.startswith("/") or getattr(event, "media_urls", None):
        return None
    from waggle import engine_commands
    if not engine_commands.serves(text):
        return None
    s = Settings.load()
    src = _source_dict(getattr(event, "source", None))
    platform = str(src.get("platform") or "")
    why = _owner_dm(platform, src, s)
    if why:
        return None
    ok, detail = post_turn(str(src.get("chat_id")), text, mid, kind="command")
    events.log("engine_admission", routed="engine" if ok else "old", reason=detail, platform=platform, command=True)
    return {"action": "skip", "reason": "waggle command answered by the core"} if ok else None


def _owner_dm(platform, src, s):
    """"" when this message comes from the owner's own direct chat on an engine chat, else why not."""
    if platform != "telegram":
        return "not telegram"
    chat_id = str(src.get("chat_id") or "")
    wanted = {str(c).strip() for c in (getattr(s, "engine_chats", None) or [])}
    if not chat_id or chat_id not in wanted:
        return "engine off for this chat"
    if (src.get("chat_type") or "dm") != "dm" or src.get("thread_id"):
        return "not the direct chat"
    if src.get("user_id") and str(src.get("user_id")) != chat_id:
        return "sender is not the chat's owner"
    return ""


def engine_route(platform, source, message_id, text, settings=None):
    """(route, reason): route "engine" when this message may go to the core's turn engine, else "old"."""
    s = settings or Settings.load()
    src = source or {}
    why = _owner_dm(platform, src, s)
    if why:
        return "old", why
    chat_id = str(src.get("chat_id") or "")
    with _seen_lock:
        seen = _seen_types.pop(str(message_id), None)
        paths = _seen_audio.pop(str(message_id), None) or []
        meta = _seen_meta.pop(str(message_id), None)
    if seen is None:
        return "old", "message type unknown"
    t = (text or "").strip()
    if seen[0] in MEDIA_TYPES:
        if not getattr(s, "engine_media", True):
            return "old", "media off"
        if meta is None:
            return "old", "media details unknown"
        if seen[0] != "sticker":
            if not meta["items"]:
                return "old", "no usable file"
            if len(meta["items"]) > MEDIA_ITEMS_MAX:
                return "old", "too many files"
            if (meta["caption"] or "").startswith("/"):
                return "old", "command caption"
        with _seen_lock:
            _seen_meta[str(message_id)] = meta  # the admission hook takes it from here
        return "engine", seen[0]
    if seen[0] == "voice":
        if t or len(paths) != 1:
            return "old", "voice note with a caption or several files"
        if not _is_audio_file(paths[0]):
            return "old", "voice file missing"
        with _seen_lock:
            _seen_audio[str(message_id)] = paths  # the admission hook takes it from here
        return "engine", "voice"
    if seen[0] != "text" or seen[1]:
        return "old", f"not typed text ({seen[0]}{', media' if seen[1] else ''})"
    if not t:
        return "old", "empty"
    if t.startswith("/"):
        return "old", "command"
    from waggle import voice_turn
    if voice_turn.parse(t, s.voice_fixes)[1] != "text":
        return "old", "voice"
    if FEEDBACK.match(t) or OBJECTIVE_CMD.match(t):
        return "old", "feedback or objective command"
    if APPROVAL_CMD.match(t) or YES.match(t):
        if _engine_owns_approval(f"telegram:{chat_id}", t):
            return "engine", ""  # the engine asked for it: its own preflight grants and tells the brain
        return "old", "approval reply"
    return "engine", ""


def _is_audio_file(path):
    try:
        from waggle import stt
        return stt.audio_file_problem(path) == ""
    except Exception:
        return False


def _engine_owns_approval(chat_key, text):
    """True when this approval reply answers an approval the engine's chat asked for (its session is the chat
    key): "approve CODE" for one of them, or a plain yes while one of them is pending."""
    from waggle import policy
    try:
        m = APPROVAL_CMD.match(text)
        if m:
            item = policy.get(m.group(2))
            return bool(item and item.get("session") == chat_key and item.get("state") == "pending")
        return any(p.get("session") == chat_key for p in policy.pending(chat_key))
    except Exception:
        return False


def post_turn(chat_id, text, message_id, timeout=CORE_POST_TIMEOUT_S, audio=None, kind=None, media=None):
    """POST the message to the core's /turn. (accepted, detail): accepted only on HTTP 202. `audio`: the file
    of a voice note (sent as kind "voice" with an empty text; the engine transcribes it). `kind` and `media`:
    "photo" / "document" / "audio" / "sticker" with the media items, "command" with the command as text."""
    import urllib.error
    import urllib.request
    from waggle.core import settings as core_settings
    try:
        token = core_settings.path("core.token").read_text(encoding="utf-8").strip()
    except OSError:
        return False, "no core token (core not running?)"
    if not token:
        return False, "empty core token"
    body = {"chat": f"telegram:{chat_id}", "address": str(chat_id), "text": "" if audio else text,
            "kind": kind or ("voice" if audio else "text"),
            "media": list(media) if media is not None else ([{"path": str(audio), "kind": "audio"}] if audio else []),
            "platform": "telegram", "origin": "user", "dedupe_key": f"telegram:{chat_id}:{message_id}"}
    req = urllib.request.Request(f"http://127.0.0.1:{core_settings.port()}/turn", method="POST",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read(4096)
    except urllib.error.HTTPError as e:
        return False, f"core answered {e.code}"
    except Exception as e:  # refused, timed out, reset: not accepted
        return False, f"{type(e).__name__}: {e}"[:200]
    if status != 202:
        return False, f"core answered {status}"
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    return True, f"turn {data.get('turn_id', '?')}" + (" (duplicate)" if data.get("duplicate") else "")


def on_gateway_admission(session_key="", platform="", source=None, message_id=None, text="", **kwargs):
    """post_gateway_admission: hand typed text in an engine chat to the core; anything else stays on the old path."""
    t0 = time.time()
    try:
        off_line = _off_reply(text, platform)
        if off_line:  # Waggle is switched off: one line, no turn, nothing for the core to do
            return {"action": "handled", "reply": off_line}
        route, why = engine_route(platform, source, message_id, text)
        if route != "engine":
            if why not in ("not telegram", "engine off for this chat"):
                events.log("engine_admission", routed="old", reason=why, platform=platform)
            return None
        audio = None
        if why == "voice":
            with _seen_lock:
                audio = (_seen_audio.pop(str(message_id), None) or [None])[0]
            if not audio:
                return None
        chat_id = str((source or {}).get("chat_id"))
        if why in MEDIA_TYPES:
            with _seen_lock:
                meta = _seen_meta.pop(str(message_id), None)
            if meta is None:
                return None
            if why == "sticker":
                ok, detail = post_turn(chat_id, "", message_id, kind="sticker",
                                       media=[{"kind": "sticker", "emoji": meta.get("emoji", "")}])
            else:
                ok, detail = post_turn(chat_id, meta["caption"], message_id, kind=why, media=meta["items"])
            events.log("engine_admission", routed="engine" if ok else "old", reason=detail, platform=platform,
                       ms=round((time.time() - t0) * 1000), media=why)
            return {"action": "handled"} if ok else None
        ok, detail = post_turn(chat_id, text, message_id, audio=audio)
        events.log("engine_admission", routed="engine" if ok else "old", reason=detail, platform=platform,
                   ms=round((time.time() - t0) * 1000), voice=bool(audio))
        return {"action": "handled"} if ok else None
    except Exception as e:  # never block a message: the old path answers
        log.warning("waggle admission hook failed: %s", e)
        return None


def _waggle_command(raw=""):
    """/waggle for chats the engine does not serve (the CLI, other chats): the same plain-text answer."""
    try:
        from waggle import engine_commands
        return engine_commands.serve(("/waggle " + str(raw or "")).strip())
    except Exception:
        return json.dumps(quota.status(), indent=2)


def register(ctx):
    global _ctx
    _ctx = ctx
    toolkit.set_dispatch(_dispatch)
    _wake_starts_conversation()
    _start_bridge()
    threading.Thread(target=_prepare_fillers, daemon=True, name="waggle-fillers").start()
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
    for hook, fn in (("pre_gateway_dispatch", on_pre_gateway_dispatch), ("post_gateway_admission", on_gateway_admission)):
        try:  # typed text in an engine chat goes to the core (settings.engine_chats; empty = nothing changes)
            ctx.register_hook(hook, fn)
        except Exception as e:  # an older Hermes without these hooks: everything stays on the old path
            log.warning("waggle could not register %s: %s", hook, e)
    ctx.register_command("waggle", _waggle_command, description="Waggle quota and status")
