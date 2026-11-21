"""Quick answers to spoken requests, before the Claude session is asked (the PC voice's 5-9 s wait).

Every request spoken to Waggle used to go to the chat's Claude session (model_first 4.9-8.1 s measured on the owner's
PC). Three tiers now come first, cheapest first; a request that none of them is sure about goes to Claude exactly as
before, so a miss costs nothing but the tier's own time:

  code    no model, about 0 s: the time, the date and day, the open reminders (the core's followups store), sums
          ("15 percent of 80") and unit conversions ("feet in a mile") (waggle/fastcalc.py). A closed vocabulary:
          the words must be the question and nothing else, so "what time is the meet" or "what day is Thanksgiving"
          are not "the time" or "the date" and go on.
  local   the small local model (the PC voice judge's gemma4, already loaded; a different model, or a different
          num_ctx, makes Ollama reload for about 8 s) answers a self-contained general-knowledge question or a
          definition with JSON {needs_tools, answer, confident}. Used only when a code gate first finds the
          request harmless (no word about the owner, the PC, files, mail, school, code, an action, or anything that
          changes with time), the model is already loaded, it answers within voice_fast_cap_s (2.5 s) and says it is
          confident and needs no tools. Any doubt, timeout or failure is a miss.
  claude  everything else.

The caller (chat_engine.run_turn, voice turns only) gets a Route. The exchange is recorded as a normal turn
(answered_by "code" or "local") and the next message to Claude carries it (unseen_note), so a follow-up such as
"and tomorrow?" works. One INFO line per voice turn names the tier and its time, never the words.

Switches (config.json): voice_fast_path (true), voice_fast_local (true), voice_fast_model, voice_fast_cap_s (2.5).
"""
import datetime as dt
import json
import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass

from . import fastcalc, voice_intent

log = logging.getLogger("waggle.voice_fast")

CAP_S = 2.5
BACKOFF_S = 60.0


@dataclass
class Fast:
    """One answer given without Claude. spoken carries "sir"; written is what the chat shows."""
    tier: str  # code | local
    kind: str  # time | date | reminders | calc | convert | knowledge
    spoken: str
    written: str
    model: str = ""
    ms: int = 0


@dataclass
class Route:
    tier: str = "claude"  # code | local | claude
    fast: Fast = None
    why: str = ""  # a short reason code when it went to Claude (never the words)
    ms: int = 0
    local_ms: int = 0  # time spent asking the local model that ended in a miss


# ---- the words ---------------------------------------------------------------------------------------------

def words(text):
    """Lower-case words with apostrophes dropped ("what's" -> "whats"), the wake name and "sir" taken off the ends."""
    t = re.sub(r"[^a-z0-9%.+*/^()\- ']", " ", (text or "").lower().replace("’", "'")).replace("'", "")
    w = [x.strip(".-") for x in t.split()]
    w = [x for x in w if x]
    lead = {"hey", "hi", "hello", "ok", "okay", "waggle", "um", "uh", "so", "yo", "sir", "oh"}
    tail = {"waggle", "sir", "please", "thanks", "thank", "you"}
    while w and w[0] in lead:
        w.pop(0)
    while w and w[-1] in tail and not (w[-1] == "you" and (len(w) < 2 or w[-2] != "thank")):
        w.pop()
    return w


def _only(w, allowed):
    return bool(w) and all(x in allowed for x in w)


# ---- speaking numbers --------------------------------------------------------------------------------------

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty"]


def _below_60(n):
    return _ONES[n] if n < 20 else _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")


def time_words(t):
    """"eleven forty-six in the morning", "five o'clock in the afternoon", "noon" for a datetime."""
    h, m = t.hour, t.minute
    if h == 0 and m == 0:
        return "midnight"
    if h == 12 and m == 0:
        return "noon"
    h12 = h % 12 or 12
    part = "in the morning" if h < 12 else "in the afternoon" if h < 17 else "in the evening" if h < 21 else "at night"
    clock = f"{_ONES[h12]} o'clock" if m == 0 else (f"{_ONES[h12]} oh {_ONES[m]}" if m < 10 else f"{_ONES[h12]} {_below_60(m)}")
    return f"{clock} {part}"


def time_text(t):
    return t.strftime("%#I:%M %p")


def _ordinal(n):
    special = {1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth", 9: "ninth", 12: "twelfth"}
    if n in special:
        return special[n]
    if n < 20:
        return _ONES[n] + "th"
    tens, ones = n // 10, n % 10
    if ones == 0:
        return {2: "twentieth", 3: "thirtieth"}[tens]
    return _TENS[tens] + "-" + (special.get(ones) or _ONES[ones] + "th")


def day_words(d):
    return f"{d.strftime('%A')}, the {_ordinal(d.day)} of {d.strftime('%B')}"


def _count(n):
    return _ONES[n] if n < 20 else str(n)


# ---- tier 1: code ------------------------------------------------------------------------------------------

_TIME_OK = {"what", "whats", "the", "time", "is", "it", "its", "right", "now", "current", "currently", "tell", "me", "can",
            "you", "could", "do", "have", "got", "quick", "check", "exact", "give", "know", "would"}
_DATE_OK = {"what", "whats", "the", "date", "day", "is", "it", "its", "today", "todays", "tell", "me", "can", "you",
            "could", "do", "know", "current", "right", "now", "of", "week", "month", "year", "which", "tomorrow",
            "tomorrows", "yesterday", "yesterdays", "was", "will", "be", "after", "before", "number", "a", "and"}
_REMIND_OK = {"what", "whats", "are", "is", "my", "the", "any", "do", "i", "have", "got", "list", "show", "me", "read",
              "out", "tell", "next", "upcoming", "open", "all", "current", "pending", "reminders", "reminder", "for",
              "today", "tomorrow", "how", "many", "there", "coming", "up", "still", "outstanding", "can", "you",
              "could", "give", "a", "an", "of", "on", "todays", "tomorrows"}


def time_answer(w, now):
    if "time" in w and _only(w, _TIME_OK) and not ({"date", "day"} & set(w)):
        return Fast("code", "time", f"It is {time_words(now)}, sir.", f"It's {time_text(now)}.")
    return None


def date_answer(w, now):
    if not _only(w, _DATE_OK) or not ({"date", "day", "month", "year", "today", "todays"} & set(w)) or "time" in w:
        return None
    ws = set(w)
    off = 0
    if "yesterday" in ws or "yesterdays" in ws:
        off = -2 if "before" in ws else -1
    if "tomorrow" in ws or "tomorrows" in ws:
        if off:
            return None
        off = 2 if "after" in ws else 1
    day = now.date() + dt.timedelta(days=off)
    which = {0: "Today is", -1: "Yesterday was", 1: "Tomorrow is", -2: "The day before yesterday was",
             2: "The day after tomorrow is"}[off]
    if ws & {"month"} and not ws & {"date", "day", "year"}:
        return Fast("code", "date", f"It is {day.strftime('%B')}, sir.", f"It's {day.strftime('%B')}.")
    if ws & {"year"} and not ws & {"date", "day", "month"}:
        return Fast("code", "date", f"It is {day.year}, sir.", f"It's {day.year}.")
    return Fast("code", "date", f"{which} {day_words(day)}, sir.",
                f"{which} {day.strftime('%A, %B')} {day.day}, {day.year}.")


def _due_words(ts, now):
    d = dt.datetime.fromtimestamp(ts)
    days = (d.date() - now.date()).days
    when = "today" if days == 0 else "tomorrow" if days == 1 else d.strftime("on %A") if 1 < days < 7 else d.strftime("on %B ") + str(d.day)
    return d, when


def reminder_rows(now, chat=""):
    from . import followups
    rows = followups.items(status="open", kind="reminder")
    return [r for r in rows if not chat or r.get("chat") in ("", None, chat)]


def reminders_answer(w, now, chat="", rows=None):
    if not ({"reminder", "reminders"} & set(w)) or not _only(w, _REMIND_OK):
        return None
    ws = set(w)
    if not (ws & {"what", "whats", "do", "list", "show", "read", "tell", "any", "how", "are", "have", "give", "can",
                  "could", "next", "is"}):
        return None
    rows = reminder_rows(now, chat) if rows is None else rows
    scope = ""
    if ws & {"today", "todays"}:
        scope = "today"
    elif ws & {"tomorrow", "tomorrows"}:
        scope = "tomorrow"
    keep = []
    for r in sorted(rows, key=lambda r: r["due"]):
        d, when = _due_words(r["due"], now)
        if scope and (when != scope and not (scope == "today" and r["due"] < now.timestamp())):
            continue
        keep.append((r, d, when))
    only_next = "next" in ws
    if only_next:
        keep = keep[:1]
    tail = f" for {scope}" if scope else ""
    if not keep:
        none = f"You have no open reminders{tail}, sir." if not only_next else "You have no open reminders, sir."
        return Fast("code", "reminders", none, none.replace(", sir", ""))
    spoken_items, written_items = [], []
    for r, d, when in keep:
        what = " ".join(str(r["what"]).split()).rstrip(".")
        spoken_items.append(f"{when.capitalize()} at {time_words(d)}: {what}.")
        written_items.append(f"- {when}, {time_text(d)}: {what}")
    n = len(keep)
    head = "Your next reminder, sir." if only_next else f"You have {_count(n)} reminder{'s' if n != 1 else ''}{tail}, sir."
    spoken = head + " " + " ".join(spoken_items[:2 if n > 1 else 1])
    if n > 2:
        spoken += " The rest are in the written list."
    written = head.replace(", sir", "") + "\n" + "\n".join(written_items[:10])
    return Fast("code", "reminders", spoken, written)


def calc_answer(text):
    got = fastcalc.calc(text)
    if got:
        say = got["say"][0].upper() + got["say"][1:]
        return Fast("code", "calc", f"{say}, sir.", f"{say}.")
    got = fastcalc.convert(text)
    if got:
        say = got["say"][0].upper() + got["say"][1:]
        return Fast("code", "convert", f"{say}, sir.", f"{say}.")
    return None


def code_answer(text, now=None, chat="", rows=None):
    """A Fast answer from plain code, or None. now: a datetime (default: the local time); rows: the open reminders
    (default: the core's followups store)."""
    now = now or dt.datetime.now()
    w = words(text)
    if not w or len(w) > 14:
        return None
    for fn in (time_answer, date_answer):
        got = fn(w, now)
        if got:
            return got
    try:
        got = reminders_answer(w, now, chat, rows)
    except Exception as e:  # noqa: BLE001  (the store being unreadable is a miss, never a failed turn)
        log.debug("reminders tier skipped: %s", e)
        got = None
    return got or calc_answer(text)


# ---- tier 2: the local model -------------------------------------------------------------------------------

_STARTS = {"what", "whats", "who", "when", "where", "why", "how", "which", "define", "spell", "translate", "is", "are",
           "does", "did"}
# anything about the owner, the PC, files, mail, school, code, an action, a previous message, or what changes with time
_PRIVATE = set("""i im ive id ill my me mine myself we our ours us you your yours yourself waggle sir
 file files folder folders document documents doc docs google gmail email emails mail inbox calendar chrome tab tabs
 screen window windows pc computer laptop desktop app apps download downloads drive onedrive github repo repos code
 coding program script robot robots ftc biobuzz titan minecraft roblox bot school class classes homework teacher
 assignment assignments grade grades college essay project projects meet meeting meetings schedule reminder reminders
 remind alarm timer note notes vault memory remember
 open close create make send set add delete remove run start stop play pause turn find search look show tell read write
 build save install update fix call text message post push pull commit email draft print
 it that this these those them they he she him her his hers then also again earlier before yesterday last next
 current currently latest now today todays tonight tomorrow recent recently newest price prices cost costs weather
 forecast score scores news stock stocks trending president prime minister ceo champion champions winner won win
 election population age old season release released live ago since mayor governor senator leader chairman chief
 coach richest kokoro ollama hermes gemma qwen claude anthropic whisper obsidian onshape gobilda andymark rev frc
 opmode teamcode java python git gitlab fabric pickleball integral derivative integrate differentiate solve
 equation theorem prove probability limit largest tallest biggest fastest longest oldest youngest smallest highest deepest heaviest most
 moons satellites countries elements species record records
 best better worst should recommend suggest favorite favourite think opinion advice idea ideas tips help""".split())
_MATHY = re.compile(r"\b(plus|minus|times|divided|multiplied|percent|squared|cubed|root|factorial|half of|double|"
                    r"twice|triple|equals?|sum of|total of|average of)\b|[+*/^%=]|\d\s*x\s*\d")
_NUMERIC_Q = re.compile(r"\bhow (?:many|much|far|tall|long|big|heavy|fast|deep|high|wide|old)\b|\bwhat year\b|\bwhen was\b")
# counts of things that never change; any other number in a local answer goes to Claude
_COUNT_OK = re.compile(r"^(?:(?:hey|ok|okay|waggle) )*how many (?:legs|sides|days|hours|minutes|seconds|weeks|months|"
                       r"continents|oceans|planets|bones|letters|teeth|wheels|eyes|arms|wings|vowels|colou?rs)\b")
_HOW_TO_SAY = re.compile(r"^how do you (spell|say|write|pronounce|translate)\b")


def gate(text):
    """(ok, reason): may this spoken request be offered to the local model at all? Only a self-contained question
    with none of the private, action, time-sensitive or follow-up words. A reason is a short code, never the words."""
    t = " ".join((text or "").lower().replace("’", "'").replace("'", "").split())
    t = re.sub(r"^(?:(?:hey|hi|ok|okay|waggle|um|uh|so)[ ,]+)+", "", t)
    howto = bool(_HOW_TO_SAY.match(t))
    if howto:
        t = "how " + t[len("how do you "):]
    w = [x for x in re.sub(r"[^a-z0-9%+*/^=\- ]", " ", t).split() if x]
    if len(w) > 22 or len(w) < (2 if w[:1] and w[0] in ("define", "spell", "translate") else 3):
        return False, "length"
    if w[0] not in _STARTS:
        return False, "not a question"
    if re.match(r"^who (?:is|are) the\b", t):
        return False, "who holds a post now"
    if re.match(r"^(?:when (?:is|are)|what (?:day|date|year) (?:is|are|was))\b", t):
        return False, "a date that depends on the year"
    if _MATHY.search(t) or fastcalc.mismatched_units(t):
        return False, "arithmetic"
    hit = [x for x in w if x in _PRIVATE and not (howto and x in ("say", "write", "translate"))]
    if hit:
        return False, "private or time-sensitive word"
    if w[0] in ("is", "are", "does", "did") and len(w) < 4:
        return False, "length"
    return True, ""


SYSTEM = (
    "You answer one spoken question for a voice assistant, from your own knowledge, in one short complete sentence "
    "(at most 25 words) that restates what was asked, the answer first, no markdown, no lists, no apologies. needs_tools is true if the question "
    "needs the user's own files, mail, calendar, school, code, computer, memory, the internet, today's news, a live "
    "or changing fact (a price, a score, who currently holds a post), or doing anything at all. confident is true "
    "only if you are certain the answer is correct and would be the same next year; it is false if you are not "
    "sure, the question is ambiguous or may have been misheard, or the answer depends on who is asking. When "
    'confident is false, answer with an empty string. Reply with JSON only: {"needs_tools": false, "answer": "...", '
    '"confident": true}.')

SCHEMA = {"type": "object", "required": ["needs_tools", "answer", "confident"],
          "properties": {"needs_tools": {"type": "boolean"}, "answer": {"type": "string"},
                         "confident": {"type": "boolean"}}}
VERIFY_SYSTEM = (
    "You check a spoken question and a proposed answer for a voice assistant. well_known is true only if the topic is "
    "ordinary general knowledge that any encyclopedia or dictionary states the same way (a country, a famous person, a "
    "common word, a basic science fact); it is false for a brand, a product, a niche or technical term, a fictional "
    "thing, a rare name, or anything you could be inventing. answers_question is true only if the answer directly and "
    "completely answers exactly what was asked (the same quantity, the same units, nothing swapped for a nearby "
    "question). correct is true only if you are certain every fact in the answer is right, and it is not a number that "
    "has changed lately (a count of moons, records, a population). Reply with JSON only: "
    '{"well_known": true, "answers_question": true, "correct": true}.')
VERIFY_SCHEMA = {"type": "object", "required": ["well_known", "answers_question", "correct"],
                 "properties": {"well_known": {"type": "boolean"}, "answers_question": {"type": "boolean"},
                                "correct": {"type": "boolean"}}}
# keep the options the judge uses: a different num_ctx would make Ollama load another copy of the model
OPTIONS = {"temperature": 0, "num_predict": 110}
_HEDGE = re.compile(r"\b(not sure|unsure|i think|i believe|probably|possibly|perhaps|might be|i don'?t know|i do not know|"
                    r"cannot|can'?t|unable|as an ai|language model|no information|do not have|don'?t have|not aware|fictional|"
                    r"depends)\b", re.I)


def ollama_ask(messages, model, timeout, schema=None, temperature=0.0, *, url="http://127.0.0.1:11434"):
    body = {"model": model, "messages": messages, "stream": False, "think": False, "format": schema or SCHEMA,
            "keep_alive": voice_intent.KEEP_ALIVE, "options": {**OPTIONS, "temperature": temperature}}
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=max(0.3, timeout)) as r:  # the timeout also closes a slow request
        out = json.loads(r.read().decode("utf-8"))
    return json.loads(out["message"]["content"])


def loaded_models(url="http://127.0.0.1:11434", timeout=0.4):
    """Names of the models Ollama has in memory now (a quick GET), so a cold model is never waited for."""
    with urllib.request.urlopen(url.rstrip("/") + "/api/ps", timeout=timeout) as r:
        return [m.get("name", "") for m in json.loads(r.read().decode("utf-8")).get("models", [])]


def _same_model(a, b):
    norm = lambda n: (n or "").strip().lower().removesuffix(":latest")  # noqa: E731
    return norm(a) == norm(b)


def model_for(settings):
    name = (getattr(settings, "voice_fast_model", "") or "").strip()
    if name:
        return name
    try:
        from waggle.core import settings as core_settings
        name = str(core_settings._config_value("pc_voice_judge_model", "") or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    return name or voice_intent.DEFAULT_JUDGE_MODEL


_backoff = voice_intent.Backoff(BACKOFF_S)


def clean_answer(raw):
    """The sentence to say from the model's JSON, or "" when it is not a plain, short, firm answer."""
    if not isinstance(raw, dict):
        return ""
    if raw.get("needs_tools") is not False or raw.get("confident") is not True:
        return ""
    ans = " ".join(str(raw.get("answer") or "").split())
    if not ans or len(ans.split()) > 32 or re.search(r"[`*#\[\]{}<>]|https?:|www\.", ans) or _HEDGE.search(ans):
        return ""
    return ans if ans[-1] in ".!?" else ans + "."


def with_sir(sentence):
    """Waggle's manner: "sir" once, at the end of the first sentence's clause."""
    if re.search(r"\bsir\b", sentence, re.I):
        return sentence
    return sentence[:-1] + ", sir" + sentence[-1] if sentence[-1:] in ".!?" else sentence + ", sir."


def local_answer(text, settings, *, ask=None, loaded=None, now=None, cap_s=None):
    """(Fast or None, why, ms). ask(messages, model, timeout) -> dict; loaded() -> names of loaded models."""
    cap = float(cap_s if cap_s is not None else (getattr(settings, "voice_fast_cap_s", CAP_S) or CAP_S))
    model = model_for(settings)
    url = getattr(settings, "ollama_url", "") or "http://127.0.0.1:11434"
    if _backoff.active():
        return None, "backoff", 0
    try:
        names = (loaded or (lambda: loaded_models(url)))()
    except Exception:  # noqa: BLE001
        return None, "ollama not reachable", 0
    if not any(_same_model(n, model) for n in names):
        return None, "model not loaded", 0
    now = now or dt.datetime.now()
    msgs = [{"role": "system", "content": SYSTEM + f" Today is {now.strftime('%A %B %d %Y')}."},
            {"role": "user", "content": " ".join(text.split())[:300]}]
    ask = ask or (lambda m, mod, t, schema=None, temperature=0.0: ollama_ask(m, mod, t, schema, temperature, url=url))
    box = {}
    t0 = time.monotonic()

    def go():
        try:
            box["raw"] = ask(msgs, model, cap)
            ans = clean_answer(box["raw"])
            if ans and fastcalc.numbers_in(ans) and not _COUNT_OK.search(text.lower()):
                box["numeric"] = True  # a small model's figures (an area, a year, a distance) are not trusted
            elif ans:  # a second look by the same model: does it answer what was asked, and is it surely right?
                check = [{"role": "system", "content": VERIFY_SYSTEM},
                         {"role": "user", "content": f"Question: {' '.join(text.split())[:300]}\nAnswer: {ans}"}]
                box["verify"] = ask(check, model, max(0.3, cap - (time.monotonic() - t0)), VERIFY_SCHEMA)
                if _NUMERIC_Q.search(text) or fastcalc.numbers_in(ans):  # a number must come out the same twice
                    box["again"] = ask(msgs, model, max(0.3, cap - (time.monotonic() - t0)), None, 0.7)
        except Exception as e:  # noqa: BLE001
            box["err"] = e
    th = threading.Thread(target=go, daemon=True, name="waggle-voice-fast-local")
    th.start()
    th.join(cap)
    ms = int((time.monotonic() - t0) * 1000)
    if th.is_alive() or "err" in box:
        _backoff.failed()
        return None, "timeout" if th.is_alive() else "model error", ms
    _backoff.worked()
    raw = box.get("raw")
    if isinstance(raw, dict) and raw.get("needs_tools") is True:
        return None, "needs tools", ms
    ans = clean_answer(raw)
    if not ans:
        return None, "not confident", ms
    if box.get("numeric"):
        return None, "numeric answer", ms
    check = box.get("verify")
    if not (isinstance(check, dict) and check.get("answers_question") is True and check.get("correct") is True
            and check.get("well_known") is True):
        return None, "check failed", ms
    if "again" in box:
        second = clean_answer(box["again"])
        if not second or not fastcalc.same_numbers(ans, second):
            return None, "numbers disagree", ms
    return Fast("local", "knowledge", with_sir(ans), ans, model=model, ms=ms), "", ms


# ---- the route ---------------------------------------------------------------------------------------------

def _flag(settings, name):
    return bool(getattr(settings, name, True))


def route(text, settings, *, chat="", voice_info=None, now=None, rows=None, ask=None, loaded=None):
    """Which tier answers this spoken request: a Route whose tier is "code", "local" or "claude" (with why)."""
    t0 = time.monotonic()
    out = Route()
    if not _flag(settings, "voice_fast_path"):
        out.why = "off"
        return out
    try:
        got = code_answer(text, now=now, chat=chat, rows=rows)
        if got:
            got.ms = int((time.monotonic() - t0) * 1000)
            return Route("code", got, ms=got.ms)
        if not _flag(settings, "voice_fast_local"):
            out.why = "local off"
            return out
        info = voice_info or {}
        if info.get("stt_confident") is False and info.get("stt_avg_logprob") is not None:
            out.why = "low speech confidence"  # a misheard question would get a firm wrong answer
            return out
        ok, why = gate(text)
        if not ok:
            out.why = f"gate: {why}"
            return out
        got, why, ms = local_answer(text, settings, ask=ask, loaded=loaded, now=now)
        out.local_ms = ms
        if got:
            return Route("local", got, ms=int((time.monotonic() - t0) * 1000), local_ms=ms)
        out.why = f"local: {why}"
    except Exception as e:  # noqa: BLE001  (a bug here must only ever mean "ask Claude")
        out.why = f"error: {type(e).__name__}"
        log.warning("voice fast path failed: %s: %s", type(e).__name__, e)
    out.ms = int((time.monotonic() - t0) * 1000)
    return out


def reply_text(fast):
    """The reply in the voice format run_turn splits again, exactly as if the model had written it."""
    return f"SPOKEN: {fast.spoken}\nANSWER:\n{fast.written}"


def log_turn(tier, seconds, why="", local_ms=0):
    """The one INFO line per voice turn: which tier answered and how long it took (never the words)."""
    log.info("voice turn tier=%s time=%.2fs%s%s", tier, seconds, f" why={why}" if why else "",
             f" local_miss={local_ms}ms" if local_ms else "")


# ---- Claude must know what it did not answer ---------------------------------------------------------------

UNSEEN_MAX = 4


def unseen_note(chat, *, now=None, within_s=6 * 3600, limit=UNSEEN_MAX):
    """A note for Claude's next message: the exchanges of this chat answered without it (code or local) since the
    last one it answered, so "and tomorrow?" after a quick answer has its context. "" when there are none."""
    from . import traces
    rows = traces.recent_chat(chat, n=12, within_s=within_s, now=now)  # newest first
    unseen = []
    for r in rows:
        by = str(r.get("answered_by") or "")
        if by.startswith("claude") or (by and by not in ("code", "local", "none")):
            break  # Claude answered this one: it saw everything before it
        if by in ("code", "local") and r.get("reply"):
            unseen.append(r)
    if not unseen:
        return ""
    lines = [f"User: {r['request']}\nWaggle: {r['reply']}" for r in reversed(unseen[:limit])]
    return ("Waggle answered these without you, so they are not in your session yet; they are part of the "
            "conversation (a follow-up may refer to them):\n" + "\n\n".join(lines))
