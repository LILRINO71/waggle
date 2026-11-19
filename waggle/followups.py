"""Follow-ups: what the user said they would do becomes a dated check-in.

capture() reads new turns from traces.db (Telegram only by default: developer one-shots and benchmark runs are
ignored) and pulls out
  - commitments with a date: "I'll test the shooter tomorrow", "going to finish the essay tonight", "I have to
    submit the form by friday" -> a check-in at the right time (a clock time + 90 min, else followup_hour that
    day; something already past is asked about the next morning);
  - one-shot reminders: "remind me at 7 to charge the batteries" -> a routines one-shot (sent as is, no model).
"Call me at 7" is handled where it is said (call.parse_request), so it is skipped here.
due() returns the check-ins whose time has come as signals for the decision step, which words them (or says
nothing when the conversation shows it is already done). Everything is stored in ~/.waggle/followups.json with
its status (open / asked / dropped) and, after it is asked, whether the user answered.
"""
import datetime as dt
import json
import re
import time
import uuid

from . import when as when_mod
from .config import Settings, data_path

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


def _path():
    return data_path("followups.json")


def load():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"cursor": 0, "items": []}


def _save(data):
    _path().write_text(json.dumps(data, indent=1), encoding="utf-8")


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
    """Read new user turns since the last run; store check-ins and create reminders. Returns what was added."""
    s = settings or Settings.load()
    now = now or time.time()
    data = load()
    if rows is None:
        try:
            from . import traces
            rows = traces.all_traces(since=now - 3 * 86400, limit=400)
        except Exception:  # noqa: BLE001
            rows = []
    new = sorted((r for r in rows if r.get("id", 0) > data.get("cursor", 0)), key=lambda r: r["id"])
    added = []
    for r in new:
        data["cursor"] = max(data.get("cursor", 0), r["id"])
        if (r.get("platform") or "") not in s.followup_platforms:
            continue
        said_at = dt.datetime.fromtimestamp(r.get("time") or now)
        for kind, what, due in extract(r.get("request") or "", said_at, s.followup_hour):
            if kind == "reminder":
                from . import routines
                if any(abs((v.get("next_run") or 0) - due) < 600 and v.get("kind") == "message"
                       for v in routines._load().values()):
                    continue  # the chat already set this one
                try:
                    rid = routines.add_once(what[:1].upper() + what[1:], due)
                except ValueError:
                    continue
                added.append({"kind": "reminder", "routine": rid, "what": what, "due": due})
                continue
            if any(i["what"].lower() == what.lower() and abs(i["due"] - due) < 6 * 3600 for i in data["items"]):
                continue
            item = {"id": uuid.uuid4().hex[:8], "created": now, "trace": r["id"], "said": (r.get("request") or "")[:300],
                    "said_at": r.get("time"), "what": what, "due": due, "status": "open"}
            data["items"].append(item)
            added.append({"kind": "checkin", **item})
    data["items"] = [i for i in data["items"] if i["status"] == "open" or now - i.get("due", now) < 14 * 86400]
    _save(data)
    if added:
        from . import events
        for a in added:
            events.log("followup", action="captured", item_kind=a["kind"], what=a["what"][:120], due=a["due"])
    return added


def due(now=None):
    """Signals for check-ins whose time has come (still open)."""
    from .watchers import signal
    now = now or time.time()
    out = []
    for i in load()["items"]:
        if i["status"] == "open" and i["due"] <= now:
            said = dt.datetime.fromtimestamp(i.get("said_at") or i["created"])
            ago = "today" if said.date() == dt.date.fromtimestamp(now) else said.strftime("on %A")
            out.append(signal(f"followup:{i['id']}", "followup", f"You said {ago} you'd {i['what']}",
                              f'Their words: "{i["said"][:200]}"', f"followup:{i['id']}", when=i["due"]))
    return out


def mark(fid, status, now=None, outbox_id=None, reason=""):
    data = load()
    for i in data["items"]:
        if i["id"] == fid:
            i.update(status=status, closed=now or time.time(), outbox=outbox_id, reason=reason[:200])
    _save(data)


def template(sig):
    """Fallback wording when Claude is unavailable."""
    what = sig["title"].split("you'd", 1)[-1].strip()
    return f"🐝 You said you'd {what}. How did it go?"


def upcoming(start_ts, end_ts):
    return [i for i in load()["items"] if i["status"] == "open" and start_ts <= i["due"] < end_ts]
