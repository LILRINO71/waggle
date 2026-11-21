"""Focus blocks: the owner says "focus on calculus for 45 minutes" and Waggle watches, quietly, for drifting off.

Off unless the owner starts a block (chat or voice: MCP tools focus_start / focus_stop; Telegram:
`/waggle focus 45 calculus`, `/waggle focus stop`). There is no background cost while no block runs: the timers
exist only during a block.

During a block, every 90 seconds (core timer focus.check):
  1. the foreground window comes from ActivityWatch (waggle/aw_sensor.py current(); raw title and URL live in memory
     for this one decision and are never stored, logged or sent anywhere except the local model below);
  2. rules judge it first:  ON   the title/app/host names the topic, or the window belongs to a project the topic
                                 needs (a calculus topic accepts school sites, notes, an AI chat)
                            OFF  a leisure kind (video, social, anime, games) that does not name the topic
                            ?    anything else
     Only the unclear ones go to the local model (Ollama, the same call shape as mail triage: no num_ctx, no
     options but temperature 0 from the schema; 2 s cap; a failure pauses it for two minutes). The model answers
     related / unrelated / unsure; only "unrelated" counts as drift. A failure or "unsure" counts as on task.
  3. drift = two OFF checks in a row. Then a short nudge, at most one every 3 minutes: spoken on the PC voice when the
     owner is at the PC (waggle/presence.py) and the voice runs, else one Telegram text. Dry butler, a few words.
When the block ends (or `focus_stop`), one summary line: minutes on task, drift count.
No Claude call anywhere in this module.

Block state: runtime.db meta "focus.block" (topic, times, counters; no window text). Timers: focus.check every 90 s
and a one-shot focus.end at the end time, both job kind focus.check; register_core() re-creates them after a core
restart while a block is active.
"""
import hashlib
import json
import re
import time

from . import events
from .config import Settings

KEY = "focus.block"
JOB = "focus.check"
CHECK_TIMER, END_TIMER = "focus.check", "focus.end"
CHECK_EVERY_S = 90
DRIFT_CHECKS = 2
NUDGE_GAP_S = 180
DEFAULT_MINUTES = 30
MAX_MINUTES = 240
LOCAL_TIMEOUT_S = 2.0
LOCAL_BACKOFF_S = 120.0
MAX_TOPIC = 60

FILLER = set("""focus focusing on for the my a an and of to some at in with me i want need work working study studying
studies session block time minutes minute mins min hours hour hrs hr please start begin keep stay sir""".split())
# What a topic's own project may also use. A calculus session may use notes and an AI chat; an FTC one, CAD and code.
RELATED = {"school": {"school", "notes", "assistant"},
           "ftc": {"ftc", "onshape", "cad", "laser", "coding", "assistant", "notes"},
           "waggle": {"waggle", "coding", "assistant", "notes"},
           "onshape": {"onshape", "cad", "ftc", "laser", "assistant"},
           "cad": {"cad", "onshape", "ftc", "laser", "assistant"},
           "laser": {"laser", "cad", "onshape", "ftc"}}
NUDGES = ["Sir, that is not {topic}.", "Pardon me, sir. {topic} was the plan.", "Sir, {topic} is still waiting.",
          "Might I suggest {topic}, sir?", "{topic}, sir. The rest can wait."]

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["related", "unrelated", "unsure"]}},
          "required": ["verdict"]}

_cache = {}      # (app, hash of title) -> "on" / "off"; memory only, cleared at each block start
_backoff = {"until": 0.0}


# ---- the block's state --------------------------------------------------------------------------------------------

def _store():
    from waggle.core import store
    return store


def load():
    raw = _store().get_meta(KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def save(block):
    _store().set_meta(KEY, json.dumps(block) if block else None)


def claim():
    """Take the active block out of the store (atomically): only one caller ever gets it to finish it."""
    store = _store()
    with store.transaction() as con:
        row = con.execute("SELECT v FROM meta WHERE k=?", (KEY,)).fetchone()
        if not row or not row[0]:
            return None
        con.execute("DELETE FROM meta WHERE k=?", (KEY,))
    try:
        return json.loads(row[0])
    except ValueError:
        return None


# ---- reading what the owner asked ------------------------------------------------------------------------------

_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.I)
_LEAD = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+")
_TAIL = re.compile(r"\s+(\d+(?:\.\d+)?)\s*$")


def parse_request(text):
    """(minutes, topic) from free text: '45 calculus', 'calculus for 45 minutes', 'for an hour on physics',
    '1.5 hours ftc', 'half an hour of reading'. A bare number counts only first or last; no number means
    DEFAULT_MINUTES; no topic means ''."""
    t = " ".join(str(text or "").split())
    minutes = None
    for rx, mins in ((r"\bhalf an hour\b", 30.0), (r"\b(an|one) hour\b", 60.0), (r"\ba quarter hour\b", 15.0)):
        if re.search(rx, t, re.I):
            minutes, t = mins, re.sub(rx, " ", t, count=1, flags=re.I)
            break
    if minutes is None:
        m = _UNIT.search(t)
        if m:
            minutes = float(m.group(1)) * (60 if m.group(2).lower().startswith("h") else 1)
            t = t[:m.start()] + " " + t[m.end():]
        else:
            m = _LEAD.match(t)
            if m and float(m.group(1)) <= MAX_MINUTES:
                minutes, t = float(m.group(1)), t[m.end():]
            else:
                m = _TAIL.search(t)
                if m and 5 <= float(m.group(1)) <= MAX_MINUTES:
                    minutes, t = float(m.group(1)), t[:m.start()]
    minutes = DEFAULT_MINUTES if minutes is None else minutes
    words = [w for w in re.findall(r"[A-Za-z0-9'+#.-]+", t) if w.lower().strip(".'-") not in FILLER]
    topic = " ".join(words).strip(" .-")[:MAX_TOPIC]
    return max(1, min(MAX_MINUTES, int(round(minutes)))), topic


def topic_profile(topic):
    """{'tokens': words to look for in a window, 'project': the project the topic belongs to or ''}."""
    from . import aw_sensor
    low = (topic or "").lower()
    tokens = sorted({w for w in re.findall(r"[a-z0-9]+", low) if len(w) >= 3 and w not in FILLER})
    project = aw_sensor.title_project(low, skip=aw_sensor.LEISURE)
    if not project:
        project = next((t for t in tokens if t in RELATED), "")
    return {"tokens": tokens, "project": project}


# ---- the judgement ---------------------------------------------------------------------------------------------

def rule_verdict(block, win):
    """'on', 'off' or '?' from rules alone. win: aw_sensor.current() (raw, in memory)."""
    from . import aw_sensor
    text = f"{win['title']} {win['host']} {win['app']}".lower()
    if any(tok in text for tok in block.get("tokens") or []):
        return "on", ""
    project, _ws, _host = aw_sensor.classify(win["app"], win["title"], win["host"])
    if aw_sensor.is_leisure(project):
        return "off", project
    if win["app"] in aw_sensor.NOISE_APPS:
        return "on", project  # the desktop, the task manager, a settings page: neither work nor play
    topic_project = block.get("project") or ""
    if project and topic_project and project in RELATED.get(topic_project, {topic_project}):
        return "on", project
    return "?", project


def _local_model(settings):
    from waggle.core import settings as core_settings
    return (str(core_settings._config_value("focus_local_model", "") or "").strip()
            or str(core_settings._config_value("mailwatch_local_model", "") or "").strip() or settings.local_model)


def ask_local(topic, win, settings, timeout=LOCAL_TIMEOUT_S):
    """The local model's verdict: 'related', 'unrelated' or 'unsure'. Raises on any failure. The window text goes to
    Ollama on this PC and nowhere else; it is not logged."""
    from . import ollama
    prompt = (f"The owner is trying to focus on: {topic or 'their work'}.\nTheir foreground window right now: program "
              f"\"{win['app']}\", title \"{win['title'][:160]}\"" + (f", site {win['host']}" if win["host"] else "")
              + ".\nIs this window plausibly part of that focus? Video sites, games, social media and anime are "
                "unrelated unless the title is about the topic. Ordinary tools (a new tab, email, a file manager, "
                "a calculator, a terminal, notes) are unsure. Answer related, unrelated or unsure.")
    out = ollama.chat_json([{"role": "user", "content": prompt}], schema=SCHEMA, model=_local_model(settings),
                           timeout=timeout, settings=settings)
    v = str((out or {}).get("verdict") or "").lower()
    if v not in ("related", "unrelated", "unsure"):
        raise ValueError("no verdict")
    return v


def judge(block, win, settings, now, local=None):
    """('on' | 'off' | 'away' | 'blind', project) for one check. `local(topic, win, settings)` is a test seam."""
    if win is None:
        return "blind", ""
    if win["afk"]:
        return "away", ""
    v, project = rule_verdict(block, win)
    if v != "?":
        return v, project
    ck = (win["app"], hashlib.sha1(win["title"].encode("utf-8", "replace")).hexdigest()[:12])
    if ck in _cache:
        return _cache[ck], project
    if now < _backoff["until"]:
        return "on", project  # the model is not answering: the benefit of the doubt
    try:
        verdict = (local or ask_local)(block.get("topic", ""), win, settings)
    except Exception:  # noqa: BLE001 - down, still loading, too slow, unreadable: unsure
        _backoff["until"] = now + LOCAL_BACKOFF_S
        return "on", project
    out = "off" if verdict == "unrelated" else "on"
    _cache[ck] = out
    return out, project


# ---- speaking up -----------------------------------------------------------------------------------------------

def deliver(text, block_id, kind, settings=None, now=None, at_pc=None, voice_ready=None, hand_over=None, send=None):
    """Say it on the PC voice when the owner is at the PC (and the voice is running), else one Telegram text.
    Returns 'voice' or 'telegram'. The test seams replace the presence read and the two channels."""
    s = settings or Settings.load()
    now = time.time() if now is None else now
    try:
        if at_pc is None or voice_ready is None:
            from . import pcvoice, presence
            if at_pc is None:
                at_pc = presence.at_pc()
            if voice_ready is None:
                voice_ready = bool(pcvoice.setting("pc_voice_speak_messages")) and pcvoice.running()
    except Exception:  # noqa: BLE001 - cannot tell: Telegram
        at_pc, voice_ready = False, False
    if at_pc and voice_ready:
        from . import pcspeak
        (hand_over or pcspeak.hand_over)(f"focus-{block_id}-{int(now)}", pcspeak.spoken_form(text), now)
        return "voice"
    if send:
        send(text)
    else:
        from . import notify
        notify.deliver("text", text, origin="focus", ref=f"{block_id}:{kind}:{int(now)}", asked=True, topic="focus",
                       settings=s, now=now)
    return "telegram"


def summary(block, now, early=False):
    """One line: minutes on task and the drift count."""
    started = block["started"]
    total = max(1, int((min(now, block["ends"]) - started) / 60 + 0.5))
    on = int(block.get("on_s", 0) / 60 + 0.5)
    drifts = int(block.get("drifts", 0))
    topic = block.get("topic") or "your work"
    head = (f"Focus block {'ended early' if early else 'done'}, sir: {min(on, total)} of {total} "
            f"minute{'s' if total != 1 else ''} on {topic}")
    tail = ("no drifting" if not drifts else "1 drift" if drifts == 1 else f"{drifts} drifts")
    blind = block.get("blind_s", 0)
    if blind > 0.3 * (total * 60):
        tail += f", though I could not see the screen for {round(blind / 60)} minutes"
    return f"{head}, {tail}."


# ---- the check -------------------------------------------------------------------------------------------------

def tick(settings=None, now=None, get=None, local=None, **seams):
    """One 90-second check. Returns a small report (no window text)."""
    from . import aw_sensor
    s = settings or Settings.load()
    now = time.time() if now is None else now
    b = load()
    if not b:
        _disarm()
        return {"idle": True}
    if now >= b["ends"]:
        return finish(s, now, "done", **seams)
    win = aw_sensor.current(get, now)
    verdict, project = judge(b, win, s, now, local)
    dt_s = max(0.0, min(now - (b.get("last_check") or b["started"]), 2 * CHECK_EVERY_S))
    b["last_check"], b["checks"] = now, b.get("checks", 0) + 1
    key = {"on": "on_s", "off": "off_s", "away": "away_s", "blind": "blind_s"}[verdict]
    b[key] = b.get(key, 0) + dt_s
    b["last_verdict"] = verdict
    nudged = False
    if verdict == "off":
        b["streak"] = b.get("streak", 0) + 1
        if b["streak"] == DRIFT_CHECKS:
            b["drifts"] = b.get("drifts", 0) + 1
        if b["streak"] >= DRIFT_CHECKS and now - b.get("last_nudge", 0) >= NUDGE_GAP_S:
            text = NUDGES[b.get("nudges", 0) % len(NUDGES)].format(topic=b.get("topic") or "your work")
            text = text[:1].upper() + text[1:]
            b["last_nudge"], b["nudges"] = now, b.get("nudges", 0) + 1
            nudged = deliver(text, b["id"], "nudge", s, now, **seams)
    elif verdict in ("on", "away"):
        b["streak"] = 0
    save(b)
    events.log("focus", action="check", verdict=verdict, project=project, streak=b.get("streak", 0),
               nudged=nudged or False)
    return {"verdict": verdict, "streak": b.get("streak", 0), "nudged": nudged or False}


def finish(settings=None, now=None, reason="done", **seams):
    """End the block (once, whoever gets there first) and say the summary line."""
    s = settings or Settings.load()
    now = time.time() if now is None else now
    b = claim()
    _disarm()
    if not b:
        return {"idle": True}
    last = {"on": "on_s", "off": "off_s", "away": "away_s", "blind": "blind_s"}.get(b.get("last_verdict"))
    if last:  # the stretch since the last check goes to what that check saw
        b[last] = b.get(last, 0) + max(0.0, min(min(now, b["ends"]) - (b.get("last_check") or b["started"]),
                                                  2 * CHECK_EVERY_S))
    line = summary(b, now, early=(reason != "done"))
    via = deliver(line, b["id"], "summary", s, now, **seams)
    events.log("focus", action="finish", reason=reason, drifts=b.get("drifts", 0), on_min=int(b.get("on_s", 0) / 60 + 0.5),
               via=via)
    _cache.clear()
    return {"finished": True, "line": line, "via": via}


# ---- starting and stopping ------------------------------------------------------------------------------------------

def _arm(block, now=None):
    from waggle.core import timers
    timers.add(CHECK_TIMER, "interval", f"{CHECK_EVERY_S}s", JOB, {}, catch_up="skip", origin="focus", now=now)
    timers.once(block["ends"], JOB, {"end": True}, timer_id=END_TIMER, catch_up="all", origin="focus")


def _disarm():
    try:
        from waggle.core import timers
        timers.remove(CHECK_TIMER)
        timers.remove(END_TIMER)
    except Exception:  # noqa: BLE001 - no runtime store: nothing to remove
        pass


def start(topic="", minutes=None, settings=None, now=None, get=None, arm=True):
    """Start a block. Returns {"ok", "message", "block"}; refuses (ok False) when ActivityWatch cannot see the screen."""
    from . import aw_sensor
    s = settings or Settings.load()
    now = time.time() if now is None else now
    if not aw_sensor.available(s, get):
        return {"ok": False, "message": "I cannot see your screen, sir: ActivityWatch is not answering. Nothing was "
                                        "started."}
    mins = max(1, min(MAX_MINUTES, int(round(float(minutes if minutes is not None else DEFAULT_MINUTES)))))
    topic = " ".join(str(topic or "").split())[:MAX_TOPIC]
    old = load()
    prof = topic_profile(topic)
    block = {"id": hashlib.sha1(f"{now}{topic}".encode()).hexdigest()[:8], "topic": topic, "minutes": mins,
             "started": now, "ends": now + mins * 60, "tokens": prof["tokens"], "project": prof["project"],
             "streak": 0, "drifts": 0, "nudges": 0, "last_nudge": 0, "last_check": None, "checks": 0,
             "on_s": 0, "off_s": 0, "away_s": 0, "blind_s": 0}
    save(block)
    _cache.clear()
    _backoff["until"] = 0.0
    if arm:
        _arm(block, now)
    events.log("focus", action="start", minutes=mins)
    what = f"{mins} minutes on {topic}" if topic else f"{mins} minutes"
    msg = (f"Focus block started: {what}. I will say something only after two checks in a row, 90 seconds apart, "
           f"and no more than once every 3 minutes.")
    if old:
        msg = f"Replaced the earlier block ({old.get('topic') or 'untitled'}). " + msg
    return {"ok": True, "message": msg, "block": {k: block[k] for k in ("id", "topic", "minutes", "ends")}}


def stop(settings=None, now=None, **seams):
    """End the running block early; the summary line is spoken or texted and returned."""
    if not load():
        return {"ok": False, "message": "No focus block is running, sir."}
    out = finish(settings, now, "stopped", **seams)
    return {"ok": bool(out.get("finished")), "message": out.get("line") or "Already finished.", "via": out.get("via")}


def status(now=None):
    now = time.time() if now is None else now
    b = load()
    if not b:
        return {"active": False, "message": "No focus block is running."}
    left = max(0, round((b["ends"] - now) / 60))
    return {"active": True, "topic": b.get("topic"), "minutes_left": left, "drifts": b.get("drifts", 0),
            "message": f"Focus block on {b.get('topic') or 'your work'}: {left} minutes left, "
                       f"{b.get('drifts', 0)} drift(s) so far."}


def command(args, settings=None):
    """The reply to `/waggle focus ...` (no model)."""
    a = (args or "").strip()
    word = a.split(None, 1)[0].lower() if a else ""
    if word in ("", "status"):
        return status()["message"]
    if word in ("stop", "end", "off", "cancel", "done"):
        return stop(settings)["message"]
    minutes, topic = parse_request(a)
    return start(topic, minutes, settings)["message"]


# ---- core wiring ---------------------------------------------------------------------------------------------

def check_job(payload=None, *, settings=None, now=None):
    from waggle.core import offswitch
    if offswitch.is_off():
        return {"skipped": "Waggle is switched off"}
    return tick(settings, now)


def reconcile():
    """After a core start: timers exist exactly while a block is active."""
    b = load()
    if b:
        _arm(b)
    else:
        _disarm()
    return bool(b)


def register_core(now=None):
    from waggle.core import jobs
    jobs.register(JOB, check_job, max_attempts=1, lease_s=60.0)
    return {"jobs": [JOB], "timers": [CHECK_TIMER, END_TIMER] if reconcile() else []}


# ---- MCP tools (chat and voice) ----------------------------------------------------------------------------------

def _t_start(args):
    topic = str(args.get("topic") or "")
    minutes = args.get("minutes")
    try:
        minutes = None if minutes in (None, "") else float(minutes)
    except (TypeError, ValueError):
        minutes = None
    return json.dumps(start(topic, minutes), ensure_ascii=False)


def _t_stop(args):
    return json.dumps(stop(), ensure_ascii=False)


def _t_status(args):
    return json.dumps(status(), ensure_ascii=False)


def _tools():
    try:
        from .toolkit import schema
        return [
            (schema("focus_start", "Start a focus block when the owner asks ('focus on calculus for 45 minutes', 'help me "
                    "concentrate'). Waggle then checks the foreground window every 90 seconds through ActivityWatch and, "
                    "after two checks in a row on something unrelated (YouTube, games, anime), says a short line on the "
                    "PC voice or texts it, at most every 3 minutes; at the end it gives one summary line. Pass the "
                    "topic as the owner said it and the minutes (default 30, at most 240).",
                    {"topic": {"type": "string", "description": "what to focus on, e.g. calculus"},
                     "minutes": {"type": "number", "description": "length of the block in minutes"}}, ["topic"]), _t_start),
            (schema("focus_stop", "End the running focus block early ('stop the focus block', 'I'm done'). Gives the "
                    "summary line (minutes on task, drifts).", {}, []), _t_stop),
            (schema("focus_status", "Is a focus block running, on what, how many minutes are left.", {}, []), _t_status),
        ]
    except Exception:  # noqa: BLE001
        return []


MCP_TOOLS = _tools()


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    print(command(" ".join(argv)))


if __name__ == "__main__":
    main()
