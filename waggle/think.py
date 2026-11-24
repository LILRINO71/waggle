"""The thinking pass: a few times a day Claude gets the whole picture of the owner's life and one open question.

The older reasoning step (initiative.py) only judges findings that a coded detector produced, so Waggle could
only notice what somebody wrote a detector for. This pass has no detectors and no list of situations. It is given
  - the date and the days ahead, what the world's calendar says about them (waggle/worldcal.py), who the owner is, the people in their life, their projects and goals,
  - what is coming up, what they said lately, what they did today, the weather,
  - what Waggle's detectors noticed, what Waggle already told them and how they reacted,
and is asked what a thoughtful friend who knows all this would tell them, or do for them, right now. "Nothing"
is a normal answer.

One pass (core timers outreach.think.*, job outreach.think):
  1. picture()   builds the sections above; every section is guarded, a missing one is simply left out.
  2. reason()    one tool-less Claude call with a schema. The model may ask for up to MAX_LOOKUPS read-only
                 look-ups (the vault, the weather, one public web page through web_read); code runs them and asks
                 once more with the results. Nothing here can write, send or run anything.
  3. act()       each thought becomes an initiative signal of kind "thought" and goes through the same guard rails
                 as every other unprompted message (initiative._apply: topic repeat, daily cap, quiet hours, the
                 expected-value check), so it is logged, capped and labelled welcome / ignored / unwelcome like the
                 rest. At most one question a day goes out when the model lacks a fact about the owner's life.

Rule for changing this file: a new situation is never a reason to add code or a line to the prompt. New code is
only for a new thing Waggle can see (a section of the picture, a kind of look-up). Situations go into
bench/think_cases.json as test cases (tools/think_eval.py runs them with the real model).

  python -m waggle.think picture            print the picture the model would get
  python -m waggle.think run --dry-run      one real pass, printed, nothing sent
  python -m waggle.think run                one real pass, delivered through the guard rails
  python -m waggle.think status             the last passes
"""
import datetime as dt
import json
import re
import sqlite3
import time

from . import events
from .config import Settings, data_path

PURPOSE = "think"
MAX_THOUGHTS = 3          # per pass, after the model's own choice
MAX_LOOKUPS = 3
MAX_QUESTIONS_PER_DAY = 1
OPEN_QUESTIONS_STOP = 2   # no new question while this many asked in the last week got no answer
QUESTION_REPEAT_DAYS = 14
THOUGHT_ACTIONS = ("text", "voice", "evening", "none")
DEFAULT_TIMES = ("07:40", "16:15", "19:45")

SYSTEM = """You are Waggle, one person's personal assistant, thinking about their day without being asked.

Below is everything Waggle knows right now: the date, who they are, the people in their life, what they are
working on, what is coming up, what they said and did lately, and what Waggle already told them. Read it the way
a thoughtful friend who knows all of this would, and ask yourself: is there anything I would tell them, remind
them of, warn them about, or offer to do for them, now or for tomorrow?

Use everything you know about the world as well as what is written here: the calendar and what a date means,
how school terms, competitions, applications, friendships and families work, what usually goes wrong before a
deadline, what people forget. Connect things that nobody connected for you. The value is in the link between
something in the world and something in their life, and in things they would be glad about but did not think of.
Do not restate what they already know, and do not repeat what they were already told.

Before you decide, scan (the "scan" field, a few words each, "nothing" when nothing):
  dates       one entry for today and one for EACH of the days ahead, taken one at a time: everything the calendar
              says about that exact date in their country and in the wider world (what is observed, celebrated,
              remembered, or changes on it, the well-known and the minor), and where it falls in the school or
              work year
  people      for each person in the picture: is anything about them due, near, or worth a word
  open_loops  what they said they would do, needed, or were worried about, with no sign since that it happened
  risks       what is coming up that clashes with something else, depends on something not done, or could be
              spoiled by the weather, the time, or a missing thing
A date, a person or a loose end matters only when it links to THEIR life: it is about them, about something they
do or care about, or about a kind of person they have in their life. Say nothing about the rest. What they have
said they want from you (when the picture has that section) outranks your own sense of what is worth a message.

After the scan, most passes should still produce nothing or one thought. A message costs them attention, so it
has to be something they would thank you for. An empty list is a good answer.

For each thought:
  topic    a short stable slug for the subject (reuse the exact topic shown under earlier thoughts when it is the
           same subject)
  message  1-3 short sentences in plain words, specific, friendly, no greeting, no emoji, no markdown. Say the
           thing and the one action. When you offer to do something, offer it as a question they can answer yes to.
  draft    optional: the ready-to-use thing itself when that saves them work (the text to send, the list, the
           two-line plan). Empty when there is nothing to draft.
  action   text (worth a message now), voice (personal and timely, better said aloud), evening (worth one line in
           tonight's note, not a buzz now), none (you considered it and it is not worth their attention)
  why      one sentence: what you connected, and why now

look-ups: when one fact would change what you say and it is not in the picture, ask for it instead of guessing:
  vault    a search in their notes (query)
  weather  a forecast (query: the place, day: today or tomorrow)
  web      one public page you know the address of (url, query: what you want from it)
You get the results and answer once more. Ask for at most three, and only when they matter.

questions: when you notice that you cannot be useful about a whole part of their life because Waggle does not
know it (who matters to them, a date, a routine), put ONE short, easy question for them here. When a whole section
of the picture is missing or nearly empty (the people in their life above all), asking is the most useful thing
this pass can do: you cannot connect what you do not know. Never ask what the picture already answers, and never
ask something you asked before.

Never invent a fact about them, a date, or a person. If you are unsure whether something is true of their life,
say it as a question or leave it out. Return JSON only."""

SCHEMA = {"type": "object", "properties": {
    "scan": {"type": "object", "properties": {
        "dates": {"type": "array", "items": {"type": "object", "properties": {
            "date": {"type": "string"}, "on_the_calendar": {"type": "string"}}, "required": ["date", "on_the_calendar"]}},
        "people": {"type": "string"},
                                              "open_loops": {"type": "string"}, "risks": {"type": "string"}},
             "required": ["dates", "people", "open_loops", "risks"]},
    "thoughts": {"type": "array", "items": {"type": "object", "properties": {
        "topic": {"type": "string"}, "message": {"type": "string"}, "draft": {"type": "string"},
        "action": {"type": "string", "enum": list(THOUGHT_ACTIONS)}, "why": {"type": "string"}},
        "required": ["topic", "message", "action", "why"]}},
    "lookups": {"type": "array", "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["vault", "weather", "web"]}, "query": {"type": "string"},
        "url": {"type": "string"}, "day": {"type": "string"}}, "required": ["kind", "query"]}},
    "questions": {"type": "array", "items": {"type": "object", "properties": {
        "question": {"type": "string"}, "why": {"type": "string"}}, "required": ["question", "why"]}}},
    "required": ["scan", "thoughts"]}


# ---- storage -------------------------------------------------------------------------------------------------

def _db():
    con = sqlite3.connect(data_path("think.db"), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS passes (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, slot TEXT,
        ok INTEGER, note TEXT, picture_chars INTEGER, calls INTEGER, lookups TEXT, thoughts TEXT, questions TEXT,
        sent INTEGER, dry INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS thoughts (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, pass INTEGER,
        topic TEXT, message TEXT, draft TEXT, model_action TEXT, action TEXT, why TEXT, guard TEXT, outbox_id TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, slug TEXT,
        question TEXT, why TEXT, outbox_id TEXT)""")
    return con


def slug(text, n=60):
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:n] or "thought"


def recent_thoughts(now, days=7):
    con = _db()
    rows = [dict(r) for r in con.execute("SELECT * FROM thoughts WHERE time>=? ORDER BY time", (now - days * 86400,))]
    con.close()
    return rows


def recent_questions(now, days=QUESTION_REPEAT_DAYS):
    con = _db()
    rows = [dict(r) for r in con.execute("SELECT * FROM questions WHERE time>=? ORDER BY time", (now - days * 86400,))]
    con.close()
    return rows


def passes(limit=10):
    con = _db()
    rows = [dict(r) for r in con.execute("SELECT * FROM passes ORDER BY id DESC LIMIT ?", (limit,))]
    con.close()
    return rows


# ---- the picture ---------------------------------------------------------------------------------------------

def _clock(d):
    try:
        return d.strftime("%#I:%M %p")
    except ValueError:  # not Windows
        return d.strftime("%-I:%M %p")


def _now_section(settings, now):
    d = dt.datetime.fromtimestamp(now)
    days = [d.date() + dt.timedelta(days=i) for i in range(8)]
    lines = [f"It is {d:%A} {d:%Y-%m-%d}, {_clock(d)}.",
             "The days ahead: " + ", ".join(f"{x:%a} {x:%Y-%m-%d}" for x in days[1:]) + ".",
             f"Quiet hours (no messages): {settings.quiet_hours[0]}:00 to {settings.quiet_hours[1]}:00."]
    if getattr(settings, "home_location", ""):
        lines.append(f"They live in {settings.home_location}.")
    return "\n".join(lines)


def _people_section(settings, cap=1800):
    """Waggle's own notes on people: one line per note (its name and its first lines)."""
    from . import vault
    own = vault.own(settings).resolve()
    out = []
    for p in sorted(vault.own_notes(settings)):
        try:
            rel = p.resolve().relative_to(own).as_posix()
        except ValueError:
            continue
        if not rel.lower().startswith("people/"):
            continue
        try:
            title, bits = vault._compact(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        line = f"- {title or p.stem}" + (": " + "; ".join(bits)[:260] if bits else "")
        out.append(line)
    return "\n".join(out)[:cap]


def _conversation_section(settings, now, days=4, limit=24):
    from . import origin, traces
    turns = origin.user_turns(traces.all_traces(since=now - days * 86400, limit=200), settings)[:limit]
    return "\n".join(f"[{dt.datetime.fromtimestamp(r['time']):%a %H:%M}] they said: {(r.get('request') or '')[:220]}"
                     + (f" | Waggle: {(r.get('reply') or '')[:140]}" if r.get("reply") else "") for r in turns)


def _open_items_section(now):
    from . import followups
    out = []
    for it in followups.items(status="open"):
        due = f"{dt.datetime.fromtimestamp(it['due']):%a %Y-%m-%d %H:%M}" if it.get("due") else "no time"
        out.append(f"- {it.get('kind') or 'item'} due {due}: {str(it.get('what') or '')[:140]}")
    return "\n".join(out[:15])


def _told_section(now, days=3):
    """What already went out, with the owner's reaction where one was recorded."""
    from . import notify
    labels = {}
    try:
        from . import outreach_ev
        con = outreach_ev._db()
        labels = {r[0]: r[1] for r in con.execute("SELECT outbox_id, label FROM outcomes WHERE sent_at>=?",
                                                  (now - days * 86400,))}
        con.close()
    except Exception:  # noqa: BLE001
        pass
    rows = [r for r in notify.rows(since=now - days * 86400) if r["status"] in ("sent", "queued", "sending", "held")]
    return "\n".join(f"[{dt.datetime.fromtimestamp(r['created']):%a %H:%M}] {' '.join((r['text'] or '').split())[:160]}"
                     + (f"  -> {labels[r['id']]}" if r["id"] in labels else "") for r in rows[-20:])


def _earlier_thoughts_section(now):
    labels = {}
    try:
        from . import outreach_ev
        con = outreach_ev._db()
        labels = {str(r[0]): r[1] for r in con.execute("SELECT outbox_id, label FROM outcomes")}
        con.close()
    except Exception:  # noqa: BLE001
        pass
    out = []
    for t in recent_thoughts(now):
        got = labels.get(str(t.get("outbox_id") or ""), "")
        out.append(f"[{dt.datetime.fromtimestamp(t['time']):%a %m-%d}] topic {t['topic']} ({t['action']}"
                   + (f", they found it {got}" if got else "") + f"): {t['message'][:140]}")
    return "\n".join(out[-20:])


def _findings_section(now):
    from . import initiative
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = [r for r in initiative.signals_between(start - 86400, now + 1) if r["kind"] != "thought"]
    return "\n".join(f"- [{r['kind']}] {r['title'][:140]} ({r['status']})" for r in rows[-15:])


TASTE_TITLE = "What they have said they want from you (their own words)"
TASTE_NOTE = "Me/Proactive.md"   # in Waggle's own vault folder; the owner (or the chat, on their word) edits it


def taste(settings=None):
    """The owner's own words about what they want to be told unprompted, and what they do not: the note TASTE_NOTE.
    Their examples go here and into bench/think_cases.json, never into this module."""
    from . import memory, vault
    return memory._strip_frontmatter(vault.read(TASTE_NOTE, settings) or "").strip()[:2000]


def add_taste(line, settings=None, now=None):
    """Append one dated line in the owner's words to TASTE_NOTE (created with a heading when missing)."""
    from . import vault
    line = " ".join(str(line or "").split())[:400]
    if not line:
        return False
    if not (vault.read(TASTE_NOTE, settings) or "").strip():
        vault.write(TASTE_NOTE, "# What I want Waggle to tell me without being asked\n\n", settings)
    vault.append(TASTE_NOTE, f"- {line} ({dt.date.fromtimestamp(now or time.time()):%Y-%m-%d})\n", settings)
    return True


CALENDAR_TITLE = ("On the world's calendar (what is observed on each date, from an encyclopedia; most of it has "
                  "nothing to do with them)")


def picture(settings=None, now=None, activity=None, weather=None, calendar=None):
    """{section title: text} in reading order. A section that fails or is empty is left out. activity, weather and
    calendar are for tests (None = look them up; False = skip)."""
    s = settings or Settings.load()
    now = now or time.time()
    out = {}

    def part(title, fn):
        try:
            text = fn()
        except Exception as e:  # noqa: BLE001 - one missing section never stops the pass
            events.log("think_section_error", section=title, error=f"{type(e).__name__}: {e}"[:200])
            return
        text = (text or "").strip()
        if text:
            out[title] = text

    from . import memory
    part("Now", lambda: _now_section(s, now))

    def world():
        if calendar is not None:
            return calendar
        from . import worldcal
        return "\n".join(worldcal.lines(now, days=int(getattr(s, "think_calendar_days", 3) or 3)))
    if calendar is not False:
        part(CALENDAR_TITLE, world)
    part(TASTE_TITLE, lambda: taste(s))
    part("Who they are (from their own notes)", lambda: memory._profile_core(s, cap=4200))
    part("People in their life (Waggle's notes)", lambda: _people_section(s))
    part("Active projects", lambda: memory._focus_lines(s))

    def coming():
        from . import learn
        return "\n".join(learn.upcoming_lines(s, days=14, now=now))
    part("Coming up (dated)", coming)
    part("Open reminders and check-ins", lambda: _open_items_section(now))
    part("What they said lately (their own words, newest first)", lambda: _conversation_section(s, now))

    def commits():
        act = activity
        if act is None:
            from . import watchers
            act = watchers.repo_activity(s, now=now)
        return "; ".join(f"{k}: {len(v)} commit(s), latest: {v[0][:70]}" for k, v in (act or {}).items())
    if activity is not False:
        part("Work today (commits)", commits)

    def sky():
        if weather is not None:
            return weather
        from . import live
        return "\n".join(str(live.weather(day=d, settings=s) or "").strip() for d in ("today", "tomorrow"))
    if weather is not False and getattr(s, "home_location", ""):
        part("Weather", sky)
    part("What Waggle's detectors noticed (handled separately; context only)", lambda: _findings_section(now))
    part("Already told them in the last 3 days (with their reaction where known)", lambda: _told_section(now))
    part("Your earlier thoughts, last 7 days (do not repeat a topic unless something changed)",
         lambda: _earlier_thoughts_section(now))
    part("Questions you already asked them (never ask again)",
         lambda: "\n".join(f"[{dt.datetime.fromtimestamp(q['time']):%a %m-%d}] {q['question']}"
                           for q in recent_questions(now)))
    return out


def render(sections):
    return "\n\n".join(f"## {title}\n{text}" for title, text in sections.items())


# ---- look-ups (read-only by construction) --------------------------------------------------------------------

def _lookup_vault(q, settings):
    from . import memory
    hits = memory.search(q["query"], k=4, settings=settings, log=False)
    return "\n".join(f"({h['path']}) {' '.join(h['text'].split())[:500]}" for h in hits) or "nothing found in the notes"


def _lookup_weather(q, settings):
    from . import live
    day = q.get("day") if q.get("day") in ("today", "tomorrow") else "tomorrow"
    return str(live.weather(q.get("query") or None, day=day, settings=settings) or "no forecast")


def _lookup_web(q, settings):
    from . import toolkit
    url = str(q.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return "no address given"
    return str(toolkit.web_read({"url": url, "question": q.get("query") or ""}))[:1500]


LOOKUPS = {"vault": _lookup_vault, "weather": _lookup_weather, "web": _lookup_web}


def run_lookups(wanted, settings, runners=None):
    """[{kind, query, result}] for at most MAX_LOOKUPS requests; a failed look-up says so instead of raising."""
    runners = runners or LOOKUPS
    out = []
    for q in [x for x in (wanted or []) if isinstance(x, dict) and x.get("kind") in runners][:MAX_LOOKUPS]:
        try:
            res = runners[q["kind"]](q, settings)
        except Exception as e:  # noqa: BLE001
            res = f"look-up failed: {type(e).__name__}: {str(e)[:120]}"
        out.append({"kind": q["kind"], "query": str(q.get("query") or "")[:200], "result": str(res)[:1600]})
    return out


# ---- reasoning -----------------------------------------------------------------------------------------------

def _parse(res):
    data = res.get("json") or {}
    if not data and res.get("text"):
        try:
            data = json.loads(res["text"])
        except ValueError:
            data = {}
    return data if isinstance(data, dict) else {}


def _clean_thoughts(items):
    out, seen = [], set()
    for t in items or []:
        if not isinstance(t, dict):
            continue
        msg = " ".join(str(t.get("message") or "").split())
        action = t.get("action") if t.get("action") in THOUGHT_ACTIONS else "none"
        topic = slug(t.get("topic") or msg[:40])
        if not msg or topic in seen:
            continue
        seen.add(topic)
        out.append({"topic": topic, "message": msg[:600], "draft": str(t.get("draft") or "").strip()[:900],
                    "action": action, "why": " ".join(str(t.get("why") or "").split())[:300]})
    return out


def reason(text, settings=None, asker=None, lookup_runners=None, allow_lookups=True):
    """The model's answer to one picture: {"thoughts", "questions", "lookups" (those that were run), "calls"}.
    asker(prompt, model=..., system=..., schema=..., effort=..., purpose=..., timeout=...) is for tests."""
    from . import budget, claude_cli
    s = settings or Settings.load()
    ask = asker or claude_cli.ask
    kw = dict(model=getattr(s, "think_model", "sonnet") or "sonnet", system=SYSTEM, schema=SCHEMA,
              effort=getattr(s, "think_effort", "medium") or "medium", purpose=PURPOSE, timeout=240)
    with budget.background(PURPOSE):
        data = _parse(ask(text, **kw))
        calls, ran = 1, []
        if allow_lookups and data.get("lookups"):
            ran = run_lookups(data["lookups"], s, lookup_runners)
            if ran:
                more = (text + "\n\n## Look-up results (you asked for these; no more look-ups are possible)\n"
                        + "\n\n".join(f"[{r['kind']}] {r['query']}\n{r['result']}" for r in ran))
                data = _parse(ask(more, **kw))
                calls = 2
    return {"thoughts": _clean_thoughts(data.get("thoughts")),
            "scan": data.get("scan") if isinstance(data.get("scan"), dict) else {},
            "questions": [q for q in (data.get("questions") or []) if isinstance(q, dict) and q.get("question")],
            "lookups": ran, "calls": calls}


# ---- acting --------------------------------------------------------------------------------------------------

def _message(t):
    return t["message"] + (f" Draft: {t['draft']}" if t.get("draft") else "")


def _same_question(a, b):
    wa, wb = set(re.findall(r"[a-z]{4,}", a.lower())), set(re.findall(r"[a-z]{4,}", b.lower()))
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def _question_allowed(q, settings, now):
    """(ok, why not): at most one question a day, none while earlier ones wait, never the same one twice."""
    from . import notify
    asked = recent_questions(now)
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if sum(1 for a in asked if a["time"] >= start) >= MAX_QUESTIONS_PER_DAY:
        return False, "one question a day"
    if any(_same_question(a["question"], q) for a in asked):
        return False, "asked before"
    if notify.in_quiet_hours(settings, now):
        return False, "quiet hours"
    week = [a for a in asked if a["time"] >= now - 7 * 86400]
    if len(week) >= OPEN_QUESTIONS_STOP:
        try:
            from . import origin, traces
            last = max(a["time"] for a in week)
            answered = any(r["time"] > last for r in
                           origin.user_turns(traces.all_traces(since=last, limit=50), settings))
        except Exception:  # noqa: BLE001
            answered = False
        if not answered:
            return False, "earlier questions are still unanswered"
    return True, ""


def act(result, settings=None, now=None, pass_id=None):
    """Send what a pass decided, through the guard rails. Returns {"thoughts": [...], "question": {...} or None}."""
    from . import initiative, notify
    s = settings or Settings.load()
    now = now or time.time()
    day = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    state = {"topics": initiative.recent_topics(now, max(s.topic_repeat_hours, 2)),
             "cap_used": initiative.sent_today(now, s), "automations": 0, "activity": None, "turns": None}
    earlier = {t["topic"]: t for t in recent_thoughts(now) if t["action"] in initiative.SEND_ACTIONS}
    rows = []
    con = _db()
    for t in [x for x in result.get("thoughts", []) if x["action"] != "none"][:MAX_THOUGHTS]:
        topic = f"thought:{t['topic']}"
        sig = {"key": f"thought:{day}:{t['topic']}", "kind": "thought", "title": t["message"][:300],
               "detail": t.get("why", ""), "topic": topic, "urgent": 0, "time": now, "data": "{}"}
        d = {"action": t["action"], "message": _message(t), "reason": t.get("why", "")}
        if t["topic"] in earlier and now - earlier[t["topic"]]["time"] < 7 * 86400:
            d = {"action": "none", "message": _message(t), "reason": "this topic was already raised this week"}
        if not initiative.record([sig], now):   # the same thought on the same day: nothing new to send
            row = {"action": "none", "guard": "already thought today", "outbox_id": None, "model_action": t["action"]}
        else:
            row = initiative._apply(sig, d, s, now, state, "think")
        con.execute("INSERT INTO thoughts (time, pass, topic, message, draft, model_action, action, why, guard, "
                    "outbox_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (now, pass_id, t["topic"], t["message"], t.get("draft", ""), t["action"], row["action"],
                     t.get("why", ""), (row.get("guard") or d["reason"]) if row["action"] == "none"
                     else (row.get("guard") or ""), row.get("outbox_id")))
        rows.append({"topic": t["topic"], "model_action": t["action"], "action": row["action"],
                     "guard": row.get("guard", ""), "outbox_id": row.get("outbox_id")})
    question = None
    for q in result.get("questions", [])[:1]:
        text = " ".join(str(q["question"]).split())[:300]
        ok, why = _question_allowed(text, s, now)
        if ok and state["cap_used"] < s.daily_text_cap:
            ob = notify.deliver("text", f"🐝 A question, so I can be more useful: {text}", origin="initiative",
                                topic=f"ask:{slug(text, 40)}", settings=s, now=now)
            con.execute("INSERT INTO questions (time, slug, question, why, outbox_id) VALUES (?,?,?,?,?)",
                        (now, slug(text, 40), text, str(q.get("why") or "")[:300], ob.get("id")))
            question = {"question": text, "outbox_id": ob.get("id")}
        else:
            question = {"question": text, "held": why or "daily cap reached"}
    con.commit()
    con.close()
    return {"thoughts": rows, "question": question}


def run(settings=None, now=None, asker=None, dry=False, slot="", sections=None, lookup_runners=None):
    """One whole pass. dry=True reasons and records, sends nothing. Returns a report dict; never raises for a
    model that is unavailable (the report says why)."""
    s = settings or Settings.load()
    now = now or time.time()
    report = {"slot": slot, "dry": dry, "ok": False}
    if not getattr(s, "think_enabled", True):
        report["note"] = "switched off (think_enabled false)"
        return report
    sections = sections if sections is not None else picture(s, now)
    text = render(sections)
    report["picture_chars"] = len(text)
    try:
        result = reason(text, s, asker=asker, lookup_runners=lookup_runners)
    except Exception as e:  # noqa: BLE001  (budget, limit, CLI missing, timeout)
        report["note"] = f"model unavailable: {str(e)[:160]}"
        _record_pass(now, report, None)
        events.log("think", ok=False, slot=slot, note=report["note"])
        return report
    report.update({"ok": True, "calls": result["calls"], "lookups": result["lookups"],
                   "thoughts": result["thoughts"], "questions": result["questions"]})
    pid = _record_pass(now, report, result)
    if not dry:
        report["acted"] = act(result, s, now, pass_id=pid)
        sent = sum(1 for r in report["acted"]["thoughts"] if r["outbox_id"])
        con = _db()
        con.execute("UPDATE passes SET sent=? WHERE id=?", (sent, pid))
        con.commit()
        con.close()
    events.log("think", ok=True, slot=slot, dry=dry, thoughts=[(t["topic"], t["action"]) for t in result["thoughts"]],
               lookups=len(result["lookups"]), calls=result["calls"])
    return report


def _record_pass(now, report, result):
    con = _db()
    cur = con.execute("INSERT INTO passes (time, slot, ok, note, picture_chars, calls, lookups, thoughts, questions, "
                      "sent, dry) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (now, report.get("slot", ""), int(bool(report.get("ok"))), report.get("note", ""),
                       report.get("picture_chars", 0), (result or {}).get("calls", 0),
                       json.dumps((result or {}).get("lookups", []))[:6000],
                       json.dumps((result or {}).get("thoughts", []))[:8000],
                       json.dumps((result or {}).get("questions", []))[:2000], 0, int(bool(report.get("dry")))))
    con.commit()
    pid = cur.lastrowid
    con.close()
    return pid


# ---- the core job and its timers -----------------------------------------------------------------------------

def due(payload=None, *, settings=None, now=None, asker=None):
    """Job outreach.think: one pass. A pass that fires late (the PC was off) still runs: the picture is built
    from now, not from the scheduled time."""
    payload = payload or {}
    rep = run(settings=settings, now=now, asker=asker, slot=str(payload.get("slot") or ""))
    return {k: rep.get(k) for k in ("ok", "note", "slot", "calls", "picture_chars")} | {
        "thoughts": [(t["topic"], t["action"]) for t in rep.get("thoughts", [])],
        "acted": rep.get("acted")}


def times(settings=None):
    """The daily pass times as (index, "HH:MM"); bad entries are dropped, none configured = DEFAULT_TIMES."""
    s = settings or Settings.load()
    out = []
    for t in (getattr(s, "think_times", None) or DEFAULT_TIMES):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(t))
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            out.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    return list(enumerate(out[:4]))


def timer_specs(settings=None):
    """(timer id, kind, spec, job kind, catch_up, max_late_s, payload) for tick.register_core."""
    s = settings or Settings.load()
    if not getattr(s, "think_enabled", True):
        return []
    out = []
    for i, t in times(s):
        h, m = t.split(":")
        out.append((f"outreach.think.{i}", "cron", f"{int(m)} {int(h)} * * *", "outreach.think", "latest", 3 * 3600,
                    {"slot": t}))
    return out


# ---- command line --------------------------------------------------------------------------------------------

def main(argv=None):
    import sys
    args = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if args[:1] == ["picture"]:
        print(render(picture()))
        return 0
    if args[:1] == ["run"]:
        rep = run(dry="--dry-run" in args, slot="manual")
        print(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
        return 0 if rep.get("ok") else 1
    if args[:1] == ["status"]:
        for p in reversed(passes(10)):
            th = json.loads(p["thoughts"] or "[]")
            print(f"{dt.datetime.fromtimestamp(p['time']):%a %m-%d %H:%M} slot {p['slot'] or '-':<6} "
                  f"{'ok' if p['ok'] else 'FAILED: ' + (p['note'] or '')[:60]}  {len(th)} thought(s), {p['sent']} sent"
                  + ("  (dry run)" if p["dry"] else ""))
            for t in th:
                print(f"    [{t['action']}] {t['topic']}: {t['message'][:110]}")
        return 0
    print("usage: python -m waggle.think picture | run [--dry-run] | status")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
