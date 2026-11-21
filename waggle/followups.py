"""Follow-ups and reminders: what the user said they would do becomes a dated check-in; "remind me" becomes a
one-shot reminder. Both are core timers (waggle.core.timers.once, catch_up="all"), so one that falls while the PC is
off still fires at the next start, late, and says so.

add(at, text, chat=, origin=)  a reminder, the function the chat side calls ("remind me at 7 to charge the
                               batteries"). At `at` the job outreach.reminder queues the text as is (no model).
capture()                      reads the user's own new turns from traces.db (origin.user_turns: tests, benches,
                               the CLI and agents never count) and pulls out
  - commitments with a date: "I'll test the shooter tomorrow", "going to finish the essay tonight", "I have to
    submit the form by friday" -> a check-in at the right time (a clock time + 90 min, else followup_hour that
    day; something already past is asked about the next morning). At that time the job outreach.checkin hands it
    to the decision step (initiative.py) as a signal, which words it or says nothing when it is already done;
  - "remind me ..." with a time -> add().
"Call me at 7" is handled where it is said (call.parse_request / call.schedule), so it is skipped here.
Items live in initiative.db (table followups); the turn cursor is runtime.db meta "outreach.followups.cursor".
"""
import datetime as dt
import json
import re
import time
import uuid

from . import when as when_mod
from .config import Settings

COMMIT = re.compile(r"\b(?:i'?ll|i will|i'?m going to|i am going to|i'?m gonna|gonna|i need to|i have to|i've got to|"
                    r"i plan to|i'?m planning to|i want to|i should|i must|i'?m about to)\s+(.{3,140})", re.I)
REMIND = re.compile(r"\bremind me\b(?:\s+to)?\s+(.{3,200})", re.I)
CALL = re.compile(r"\b(?:call|ring|phone) me\b", re.I)
HYPOTHETICAL = re.compile(r"\b(if|whether|maybe|might|would|could)\b", re.I)
TIME_WORDS = re.compile(
    r"\s*\b(?:(?:later\s+)?today|tonight|tomorrow(?:\s+(?:morning|afternoon|evening|night))?|this\s+(?:morning|"
    r"afternoon|evening|weekend)|next\s+week|(?:on|by|this|next)\s+(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|(?:at|by|around)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|"
    r"\d{1,2}(?::\d{2})\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm)|in\s+(?:an?|\d+)\s+(?:minutes?|hours?|days?)|"
    r"20\d\d-\d{2}-\d{2})\b", re.I)


CURSOR_META = "outreach.followups.cursor"
FIELDS = ("id", "kind", "created", "trace", "said", "said_at", "what", "due", "status", "closed", "outbox",
          "reason", "chat", "origin", "timer_id")


def _db():
    from . import initiative
    return initiative._db()


def _insert(item):
    con = _db()
    con.execute(f"INSERT INTO followups ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
                tuple(item.get(k) for k in FIELDS))
    con.commit()
    con.close()


def get(fid):
    con = _db()
    row = con.execute("SELECT * FROM followups WHERE id=?", (fid,)).fetchone()
    con.close()
    return dict(row) if row else None


def items(status=None, kind=None):
    con = _db()
    q, args = "SELECT * FROM followups WHERE 1=1", []
    if status:
        q += " AND status=?"
        args.append(status)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    out = [dict(r) for r in con.execute(q + " ORDER BY due", args)]
    con.close()
    return out


VOICE_WINDOW_S = 600


def asked_by_voice(now=None, chat="", turns=None, window_s=VOICE_WINDOW_S):
    """True when the user's most recent own turn (origin user) within the last `window_s` seconds was a voice turn.
    Non-user turns (tests, benches, the CLI, agents) never count, and a turn of another chat is skipped when the
    chat is known. turns: trace dicts, newest first (default: learn.user_turns from traces.db). Never raises."""
    try:
        from . import learn
        now = time.time() if now is None else float(now)
        rows = learn.user_turns(now - window_s) if turns is None else turns
        for r in rows:
            if not learn.is_user_turn(r) or not (now - window_s <= (r.get("time") or 0) <= now):
                continue
            if chat and r.get("chat") and r.get("chat") != chat:
                continue
            extra = r.get("extra") or {}
            if isinstance(extra, str):
                extra = json.loads(extra or "{}")
            vt = extra.get("voice_turn")
            return bool(extra.get("voice") or (isinstance(vt, dict) and vt.get("voice")))
    except Exception:  # noqa: BLE001  (the voice flag is a nicety; a reminder is created either way)
        pass
    return False


def add(at, text, *, chat="", origin="user", now=None, voice=None):
    """A one-shot reminder at `at` (epoch seconds or a datetime). Returns its id. The text goes out as is when it
    fires (late with a note when the PC was off). origin: who asked (decision 10): anything but the user's own
    channels (test, bench, cli, agent) never reaches the phone."""
    from waggle.core import timers
    now = time.time() if now is None else float(now)
    ts = at.timestamp() if isinstance(at, dt.datetime) else float(at)
    text = " ".join(str(text or "").split())[:500]
    if not text:
        raise ValueError("a reminder needs text")
    fid = uuid.uuid4().hex[:10]
    asked = (origin or "user") == "user" and (asked_by_voice(now, chat) if voice is None else bool(voice))
    tid = timers.once(ts, "outreach.reminder", {"id": fid, **({"voice": True} if asked else {})}, timer_id=f"reminder:{fid}", catch_up="all",
                      origin=origin or "user")
    _insert({"id": fid, "kind": "reminder", "created": now, "what": text, "due": ts, "status": "open",
             "chat": chat or "", "origin": origin or "user", "timer_id": tid})
    return fid


def _add_checkin(what, due, said, said_at, trace, now, chat="", origin="user", voice=None):
    from waggle.core import timers
    fid = uuid.uuid4().hex[:8]
    asked = origin == "user" and (asked_by_voice(now, chat) if voice is None else bool(voice))
    tid = timers.once(due, "outreach.checkin", {"id": fid, **({"voice": True} if asked else {})}, timer_id=f"checkin:{fid}", catch_up="all",
                      origin=origin)
    item = {"id": fid, "kind": "checkin", "created": now, "trace": trace, "said": (said or "")[:300],
            "said_at": said_at, "what": what, "due": due, "status": "open", "chat": chat, "origin": origin,
            "timer_id": tid}
    _insert(item)
    return item


def cancel(fid):
    """Drop an open reminder or check-in (its timer too). True when there was one."""
    from waggle.core import timers
    item = get(fid)
    if not item or item["status"] != "open":
        return False
    if item.get("timer_id"):
        timers.remove(item["timer_id"])
    mark(fid, "cancelled")
    return True


def _clean(phrase):
    phrase = TIME_WORDS.sub("", phrase)
    phrase = re.split(r"[.!?;\n]| and then | but ", phrase)[0]
    return " ".join(phrase.split()).strip(" ,-")


def extract(text, now=None, followup_hour=19):
    """[(kind, what, due_ts)] from one user message. kind: checkin | reminder."""
    now = now or dt.datetime.now()
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        if not sentence.strip() or sentence.strip().endswith("?") or CALL.search(sentence):
            continue
        m = REMIND.search(sentence)
        if m:
            at, has_time = when_mod.parse(sentence, now)
            what = _clean(m.group(1))
            if at and what:
                if not has_time:
                    at = at.replace(hour=9, minute=0)
                if at > now:
                    out.append(("reminder", what, at.timestamp()))
            continue
        m = COMMIT.search(sentence)
        if not m or HYPOTHETICAL.search(sentence[:m.start()]):
            continue
        at, has_time = when_mod.parse(sentence, now)
        what = _clean(m.group(1))
        if not at or len(what) < 3:
            continue
        if has_time:
            due = at + dt.timedelta(minutes=90)
        else:
            due = at.replace(hour=followup_hour, minute=0)
        if due <= now:  # "I'll do it today" said at 22:00: ask tomorrow morning
            due = (now + dt.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        out.append(("checkin", what, due.timestamp()))
    return out


def capture(settings=None, now=None, rows=None):
    """Read the user's own new turns since the last run; store check-ins and create reminders. Returns what was
    added. rows: traces dicts (tests); default: traces.db. Only origin.user_turns are read."""
    from waggle.core import store

    from . import origin
    s = settings or Settings.load()
    now = now or time.time()
    if rows is None:
        try:
            from . import traces
            rows = traces.all_traces(since=now - 3 * 86400, limit=400)
        except Exception:  # noqa: BLE001
            rows = []
    cursor = int(float(store.get_meta(CURSOR_META, 0) or 0))
    new = sorted((r for r in rows if r.get("id", 0) > cursor), key=lambda r: r["id"])
    if not new:
        return []
    user = {id(r) for r in origin.user_turns(new, s)}
    open_items = items(status="open")
    added = []
    for r in new:
        if id(r) not in user:
            continue
        said_at = dt.datetime.fromtimestamp(r.get("time") or now)
        for kind, what, due in extract(r.get("request") or "", said_at, s.followup_hour):
            text = what[:1].upper() + what[1:]
            if any(abs(i["due"] - due) < 6 * 3600 and i["what"].lower() == (text if kind == "reminder" else what).lower()
                   for i in open_items):
                continue  # already set (by the chat, or an earlier capture)
            if kind == "reminder":
                fid = add(due, text, origin="user", now=now)
                item = get(fid)
            else:
                item = _add_checkin(what, due, r.get("request"), r.get("time"), r.get("id"), now)
            open_items.append(item)
            added.append(item)
    store.set_meta(CURSOR_META, max(r["id"] for r in new))
    if added:
        from . import events
        for a in added:
            events.log("followup", action="captured", item_kind=a["kind"], what=a["what"][:120], due=a["due"])
    return added


def signal_for(item, now=None):
    """The decision step's signal for a check-in whose time has come."""
    from .watchers import signal
    now = now or time.time()
    said = dt.datetime.fromtimestamp(item.get("said_at") or item["created"])
    ago = "today" if said.date() == dt.date.fromtimestamp(now) else said.strftime("on %A")
    return signal(f"followup:{item['id']}", "followup", f"You said {ago} you'd {item['what']}",
                  f'Their words: "{(item.get("said") or "")[:200]}"', f"followup:{item['id']}", when=item["due"])


def mark(fid, status, now=None, outbox_id=None, reason=""):
    con = _db()
    con.execute("UPDATE followups SET status=?, closed=?, outbox=?, reason=? WHERE id=?",
                (status, now or time.time(), None if outbox_id is None else str(outbox_id), (reason or "")[:200], fid))
    con.commit()
    con.close()


DONE_WORDS = re.compile(r"\b(done|did|finished|tested|completed|submitted|pushed|fixed|sent|built|wrote|ran|worked|"
                        r"works|handed in|turned in|it scored)\b", re.I)
STOP = {"the", "and", "for", "with", "that", "this", "about", "some", "from", "into", "have", "get", "out", "tonight",
        "today", "tomorrow", "then", "just", "all", "them", "our", "his", "her", "its", "one", "new"}


def done_evidence(item, now=None, settings=None, turns=None, activity=None):
    """What the records say about a check-in's subject having been done since it was said: a statement by the user
    (not a question) with a word of completion and a word of the subject, or a commit of today whose subject shares
    its words. "" when nothing shows it. turns: trace dicts (default: the user's own turns since it was said,
    from traces.db); activity: {repo: [commit subjects]} (default: none)."""
    now = now or time.time()
    keys = [w for w in re.findall(r"[a-z0-9]{3,}", (item.get("what") or "").lower()) if w not in STOP]
    if not keys:
        return ""
    said_at = float(item.get("said_at") or item.get("created") or 0)

    def hits(text):
        words = re.findall(r"[a-z0-9]{3,}", text.lower())
        return sum(1 for k in keys if any(w.startswith(k[:4]) or k.startswith(w[:4]) and len(w) >= 4 for w in words))

    if turns is None:
        try:
            from . import origin, traces
            s = settings or Settings.load()
            turns = origin.user_turns(traces.all_traces(since=said_at, limit=200), s)
        except Exception:  # noqa: BLE001
            turns = []
    for r in sorted(turns, key=lambda x: x.get("time") or 0):
        text = (r.get("request") or "").strip()
        if (r.get("time") or 0) <= said_at or r.get("id") == item.get("trace") or text.endswith("?"):
            continue
        if DONE_WORDS.search(text) and hits(text) >= min(2, len(keys)):
            return f'the user said "{text[:100]}"'
    for repo, subjects in (activity or {}).items():
        for subj in subjects:
            if hits(subj) >= min(2, len(keys)):
                return f'a commit in {repo}: "{subj[:80]}"'
    return ""


def template(sig):
    """Fallback wording when Claude is unavailable."""
    what = sig["title"].split("you'd", 1)[-1].strip()
    return f"🐝 You said you'd {what}. How did it go?"


def upcoming(start_ts, end_ts):
    """Open check-ins due in [start_ts, end_ts) (the evening note's "tomorrow")."""
    return [i for i in items(status="open", kind="checkin") if start_ts <= i["due"] < end_ts]
