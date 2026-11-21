"""Learning about the user: a fact model with durable facts, dated events and a record of every decision.

- Durable facts (like, dislike, preference, goal, fact, person, project, idea, about) last until replaced.
  Single-value details (home, school, grade, age, job, name) replace the old value instead of piling up.
- Dated events (event, deadline, reply) carry an absolute date and expire the day after; they live in
  Knowledge/Upcoming.md, never in the profile. "Meet Saturday" said on a Wednesday becomes the Saturday's date.
- Every fact filed or rejected is logged (events kind 'learn', with the reason) and kept in knowledge.db (facts).

Where facts come from: the daily conversation digest (digest.py: whole conversations, a capable model, background
budget, only the user's own words or verified outcomes), the `remember` tool, and the Calendar/Gmail sync. The old
per-message extraction with the small local model is retired: learn_async() is kept for the plugin and does
nothing. A weekly Sonnet pass (`waggle consolidate`) still tidies the profile."""
import datetime as dt
import json
import math
import os
import re
import time

from . import claude_cli, events, factdecide, kstore, ollama, quota, vault, vault_git
from .config import Settings, data_path

FILES = {"like": "Me/Likes.md", "dislike": "Me/Dislikes.md", "preference": "Me/Learned.md",
         "goal": "Me/Learned.md", "fact": "Me/Learned.md", "person": "People/People.md",
         "project": "Projects/Projects.md", "idea": "Ideas/Ideas.md"}
FILES["about"] = "Me/Profile.md"  # personal details live in the profile's "About me" section
FILES["event"] = "Knowledge/Upcoming.md"  # dated: kept in the Upcoming note until the day has passed
EVENT_KINDS = {"event", "deadline", "reply"}
UPCOMING_NOTE = "Knowledge/Upcoming"

ABOUT_KEYS = ["name", "home", "school", "grade", "age", "job", "schedule", "routine", "place", "family", "health",
              "other"]
SINGLE = {"name", "home", "school", "grade", "age", "job"}

EXTRACT_SYSTEM = """You read ONE message the user typed to their personal assistant and pull out lasting facts
about the USER themself, worth remembering for months. Ignore questions, requests and anything about the world.
Return JSON {"facts": [{"kind": "about|like|dislike|preference|goal|person|project|idea", "key": "...",
"text": "..."}]}. Most messages have nothing: return {"facts": []}."""

# The model sometimes "learns" search results or forecasts; those are never facts about the user.
WORLD_FACT = re.compile(r"\b(forecast|chance of (rain|snow)|°[FC]|mph|high of|low of|announced|championship will|"
                        r"news|according to|wind speeds?)\b", re.I)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
TIME_BOUND = re.compile(r"\b(today|tonight|tomorrow|this (week|weekend|morning|afternoon|evening)|next week|"
                        r"(on |this |next )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?|"
                        r"right now|at the moment|currently deciding)\b", re.I)
EVENT_NOUN = re.compile(r"\b(meet|meeting|test|exam|quiz|game|match|competition|tournament|qualifier|scrimmage|"
                        r"practice|appointment|class|deadline|due|trip|party|interview|call|visit|inspection|event|"
                        r"concert|recital|presentation|fair|show|flight|birthday|dinner|lesson|session|review)\b", re.I)
RECURRING = re.compile(r"\b(every|each|weekly|daily|mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|"
                       r"sundays|weekdays|weekends)\b", re.I)


# ---- dates ---------------------------------------------------------------------------------------

def resolve_date(text, ref=None):
    """An absolute date for 'Saturday', 'tomorrow', 'Nov 22', '11/22', '2026-11-22' relative to `ref` (a date).
    None when the text names no day."""
    ref = ref or dt.date.today()
    low = (text or "").lower()
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", low)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"\b(" + "|".join(MONTHS) + r")[a-z]*\.? (\d{1,2})\b", low)
    if m:
        try:
            d = dt.date(ref.year, MONTHS.index(m.group(1)) + 1, int(m.group(2)))
            return d if d >= ref - dt.timedelta(days=60) else d.replace(year=ref.year + 1)
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", low)
    if m:
        try:
            d = dt.date(ref.year, int(m.group(1)), int(m.group(2)))
            return d if d >= ref - dt.timedelta(days=60) else d.replace(year=ref.year + 1)
        except ValueError:
            return None
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", low):
        return ref
    if "tomorrow" in low:
        return ref + dt.timedelta(days=1)
    m = re.search(r"\b(next )?(" + "|".join(WEEKDAYS) + r")\b", low)
    if m:
        ahead = (WEEKDAYS.index(m.group(2)) - ref.weekday()) % 7
        if m.group(1) and ahead == 0:
            ahead = 7
        return ref + dt.timedelta(days=ahead)
    return None


def _iso(d):
    if isinstance(d, dt.date):
        return d.isoformat()
    try:
        return dt.date.fromisoformat(str(d)[:10]).isoformat()
    except ValueError:
        return None


# ---- whose words count ---------------------------------------------------------------------------

# A turn is the user's own when its origin says so. Rows recorded before origins existed have none: those from the
# CLI (every development test went through it) or with no platform (background agent prompts) are never the user's.
USER_SQL = "(origin='user' OR (origin IS NULL AND lower(coalesce(platform,'')) NOT IN ('cli','')))"
NON_USER_SQL = f"NOT {USER_SQL}"


def is_user_turn(row):
    """True only for a turn the user typed or said: origin 'user', or no origin on an old row that did not come
    from the CLI or a background agent."""
    if not isinstance(row, dict):
        return False
    origin = (row.get("origin") or "").strip().lower()
    if origin:
        return origin == "user"
    return (row.get("platform") or "").strip().lower() not in ("cli", "")


def user_turns(since=0.0, until=None, limit=5000):
    """The user's own turns since `since` (and before `until`), newest first, with their steps. Everything that
    learns from past conversations reads them through here."""
    from . import traces
    return [r for r in traces.all_traces(since, limit=limit, include_non_user=True)
            if is_user_turn(r) and (until is None or r["time"] < until)]


def env_origin():
    """The origin this process runs under (WAGGLE_ORIGIN, set by tests and benches), or ''."""
    return (os.environ.get("WAGGLE_ORIGIN") or "").strip().lower()


# ---- the store -----------------------------------------------------------------------------------

def _log(action, fact, reason="", source="", **extra):
    events.log("learn", action=action, fact_kind=fact.get("kind"), text=str(fact.get("text", ""))[:200],
               reason=reason, source=source, **extra)


def _words(text):
    return set(re.findall(r"[a-z0-9]{3,}", (text or "").lower())) - {"the", "and", "has", "have", "with", "for",
                                                                     "user", "wants", "likes"}


def _similar(a, b, th=0.75):
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= th


def facts(status="active", kind=None, since=None):
    con = kstore.db()
    q, params = "SELECT * FROM facts WHERE 1=1", []
    if status:
        q += " AND status=?"
        params.append(status)
    if kind:
        kinds = [kind] if isinstance(kind, str) else list(kind)
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    if since:
        q += " AND created>=?"
        params.append(since)
    rows = [dict(r) for r in con.execute(q + " ORDER BY id", params)]
    con.close()
    return rows


def _insert(row):
    con = kstore.db()
    cur = con.execute("INSERT INTO facts (created, kind, key, text, date, expires, source, evidence, status, note, "
                      "source_turn, origin, observed, valid_from, invalid_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (row.get("created", time.time()), row["kind"], row.get("key") or "", row["text"],
                       row.get("date"), row.get("expires"), row.get("source", ""), row.get("evidence", "")[:500],
                       row.get("status", "active"), row.get("note", ""), row.get("source_turn"),
                       row.get("origin") or "user", row.get("observed"),
                       row.get("valid_from") or dt.date.today().isoformat(), row.get("invalid_at")))
    con.commit()
    fid = cur.lastrowid
    con.close()
    return fid


def _set_status(fid, status, replaced_by=None, invalid_at=None):
    """End a fact: it stays in the database with its history, `invalid_at` says from when it no longer holds."""
    con = kstore.db()
    con.execute("UPDATE facts SET status=?, replaced_by=?, invalid_at=COALESCE(?, invalid_at, ?) WHERE id=?",
                (status, replaced_by, invalid_at, dt.date.today().isoformat(), fid))
    con.commit()
    con.close()


def check(fact, ref=None):
    """Normalise one candidate fact. Returns (fact, None) or (None, reason it is rejected)."""
    if not isinstance(fact, dict):
        return None, "not a fact object"
    kind = str(fact.get("kind", "")).lower()
    text = re.sub(r"\s+", " ", str(fact.get("text", ""))).strip().rstrip(".")
    if kind not in FILES and kind not in EVENT_KINDS:
        return None, f"unknown kind {kind!r}"
    if not 6 <= len(text) <= 300:
        return None, "too short or too long"
    if WORLD_FACT.search(text):
        return None, "a fact about the world, not about the user"
    out = {"kind": kind, "text": text}
    ref = ref or dt.date.today()
    if kind == "about":
        out["key"] = fact.get("key") if fact.get("key") in ABOUT_KEYS else "other"
    if kind in ("event", "deadline", "reply") or (kind != "about" and TIME_BOUND.search(text)
                                                  and not RECURRING.search(text)) or \
            (kind == "about" and out.get("key") in ("schedule", "other") and TIME_BOUND.search(text)
             and not RECURRING.search(text)):
        if kind not in EVENT_KINDS and not EVENT_NOUN.search(text):
            return None, "a passing state, not a lasting fact or a dated event"
        date = _iso(fact.get("date")) if fact.get("date") else None
        date = date or _iso(resolve_date(text, ref))
        if not date:
            return None, "time-bound with no resolvable date (not a lasting fact)"
        out.update(kind=kind if kind in EVENT_KINDS else "event", date=date,
                   time=str(fact.get("time") or "")[:5])
        if date < ref.isoformat() and out["kind"] != "reply":
            return None, f"already past ({date})"
        out["expires"] = (dt.date.fromisoformat(date) + dt.timedelta(days=7 if out["kind"] == "reply" else 1)
                          ).isoformat()
    return out, None


OPPOSITE = {"like": "dislike", "dislike": "like"}
_POLARITY = {"like", "likes", "liked", "love", "loves", "enjoy", "enjoys", "dislike", "dislikes", "hate", "hates",
             "not", "no", "longer", "anymore", "stopped", "never", "doesn", "don", "user"}


def _subject(text):
    return {w for w in _words(text) if w not in _POLARITY}


def _unfile_line(f, s):
    """Take the vault line a retired fact was filed as out of its note (the facts table keeps the history)."""
    rel = FILES.get(f["kind"])
    if not rel or f["kind"] in EVENT_KINDS:
        return
    lines = vault.read(rel, s).splitlines()
    keep = [l for l in lines if not (l.startswith("- ") and f["text"] in l)]
    if len(keep) != len(lines):
        vault.write(rel, "\n".join(keep) + "\n", s)


def _close(old, by, day, s, why):
    """A later fact contradicts `old`: it stops holding from `day`."""
    _set_status(old["id"], "replaced", by, invalid_at=day)
    _unfile_line(old, s)
    _log("replaced", old, why, old.get("source", ""), by=by)


def _when(row):
    """When a fact was STATED: its source turn's time (observed), else its valid_from day, else when it was filed."""
    t = row.get("observed")
    if t:
        return float(t)
    vf = row.get("valid_from")
    if vf:
        try:
            return time.mktime(dt.date.fromisoformat(str(vf)[:10]).timetuple())
        except ValueError:
            pass
    return float(row.get("created") or 0)


def _stale_against(f, supersedes):
    """The active fact that `f` would close but which was stated AFTER `f`, or None. A statement that arrives late
    (the digest of an older day, a correction processed after a later conversation) must not overwrite a newer one:
    the newer fact stays and the old one is dropped (graphiti's temporal invalidation: the candidate that starts
    later supersedes, never the other way round). Only facts that were stated by the owner count as the newer
    side (a conversation turn, or the remember tool): a calendar, mail or quiz fact carries the day it was
    synced, not the day it was said."""
    stamp = _when(f)
    ids = {int(i) for i in (supersedes or []) if str(i).lstrip("-").isdigit()}
    for a in facts("active"):
        if not (a.get("source_turn") is not None or (a.get("origin") or "user") == "user"):
            continue
        hit = a["id"] in ids
        if not hit and f["kind"] == "about" and f.get("key") in SINGLE:
            hit = a["kind"] == "about" and a["key"] == f["key"]
        if not hit and f["kind"] in OPPOSITE and a["kind"] == OPPOSITE[f["kind"]]:
            sa, sf = _subject(a["text"]), _subject(f["text"])
            hit = bool(sa and sf and len(sa & sf) / len(sa | sf) >= 0.5)
        if hit and _when(a) > stamp:
            return a
    return None


@vault_git.recorded("learn", lambda r, a, k: f"filed {a[0].get('kind', 'fact')} fact: {str(a[0].get('text', ''))[:70]}")
def file_fact(fact, settings=None, source="", evidence="", ref=None, embedder=None, turn=None, supersedes=(),
              origin=None):
    """File one fact (after check()). Returns (True, fact_id) or (False, reason). Always logged.

    turn: the trace row the fact was stated in. A fact from a conversation is only filed when that turn is the
    user's own (is_user_turn); it then carries the turn's id, origin and time. `supersedes`: ids of active facts
    this one contradicts; they are closed (status replaced, invalid_at = the day this was observed), as are a
    single-value detail's old value and the opposite like/dislike of the same subject. Facts that did not come
    from a conversation (calendar, mail, the remember tool) say so in `origin`."""
    s = settings or Settings.load()
    if turn is None and str(source).startswith("digest"):
        _log("rejected", fact if isinstance(fact, dict) else {}, "no source turn", source)
        return False, "no source turn"
    if turn is not None and not is_user_turn(turn):
        _log("rejected", fact if isinstance(fact, dict) else {}, "not the user's own words "
             f"(origin {turn.get('origin') or turn.get('platform') or 'unknown'})", source, turn=turn.get("id"))
        return False, "not the user's own words"
    if turn is None and not origin and env_origin() and env_origin() != "user":
        _log("rejected", fact if isinstance(fact, dict) else {}, f"filed from a {env_origin()} run", source)
        return False, "not the user's own words"
    f, why = check(fact, ref)
    if not f:
        _log("rejected", fact if isinstance(fact, dict) else {}, why, source)
        return False, why
    if turn is not None:
        f.update(source_turn=turn.get("id"), origin=(turn.get("origin") or "user"), observed=turn.get("time"),
                 valid_from=dt.date.fromtimestamp(turn["time"]).isoformat())
    else:
        f.update(origin=origin or "user", observed=time.time())
    day = f.get("valid_from") or dt.date.today().isoformat()
    active = facts("active", f["kind"])
    if f["kind"] in EVENT_KINDS:
        for a in active:
            if a["date"] == f["date"] and _similar(a["text"], f["text"], 0.5):
                _log("rejected", f, "duplicate of a known event", source, existing=a["id"])
                return False, "duplicate of a known event"
    elif f["kind"] == "about" and f["key"] in SINGLE:
        pass  # replaced below
    else:
        # "Has 2 cats" and "Has 3 cats" share every word of 3+ letters: with the decision step on, a changed number
        # is no duplicate (it is an UPDATE, decided below).
        if getattr(s, "memory_fact_decide", True):
            def same(text):
                return _similar(text, f["text"]) and factdecide.numbers(text) == factdecide.numbers(f["text"])
        else:
            def same(text):
                return _similar(text, f["text"])
        for a in active:
            if (a["kind"] != "about" or a["key"] == f.get("key")) and same(a["text"]):
                _log("rejected", f, "duplicate of a known fact", source, existing=a["id"])
                return False, "duplicate of a known fact"
        rel = FILES.get(f["kind"])
        if rel and f["kind"] != "about":
            lines = [l[2:].split(" (")[0] for l in vault.read(rel, s).splitlines() if l.startswith("- ")]
            if any(same(l) for l in lines) or _embed_dup(lines, f["text"], embedder):
                _log("rejected", f, f"already in {rel}", source)
                return False, f"already in {rel}"
    decision = None
    if not supersedes and getattr(s, "memory_fact_decide", True) and not (f["kind"] == "about"
                                                                         and f["key"] in SINGLE):
        decision = _decide(f, s, embedder, source)  # compare with the 10 closest known facts: ADD/UPDATE/INVALIDATE/SKIP
        target = decision["target"]
        if decision["action"] == factdecide.SKIP and target:
            _log("rejected", f, f"duplicate: {decision['reason']}", source, existing=target["id"])
            return False, "duplicate of a known fact"
        if decision["action"] in (factdecide.UPDATE, factdecide.INVALIDATE) and target:
            supersedes = [target["id"]]  # the older-statement rule below applies to it like to any other
    newer = _stale_against(f, supersedes)
    if newer:  # an older statement that arrived late never replaces a newer one
        _log("rejected", f, f"older than fact #{newer['id']}, which was stated later", source, existing=newer["id"])
        return False, f"older than fact #{newer['id']}, which was stated later"
    if decision and decision["action"] == factdecide.INVALIDATE and decision["target"]:
        old = decision["target"]  # the statement only says the old fact stopped being true: nothing new to file
        _set_status(old["id"], "invalidated", None, invalid_at=day)
        _unfile_line(old, s)
        _log("invalidated", old, decision["reason"], source, by_text=f["text"][:200])
        if old["kind"] in EVENT_KINDS:
            render_upcoming(s)
        return False, f"invalidated fact #{old['id']}: {decision['reason']}"
    fid = _insert({**f, "source": source, "evidence": evidence})
    replaced = set()
    if f["kind"] == "about" and f["key"] in SINGLE:
        for a in active:
            if a["key"] == f["key"] and a["id"] != fid:
                _set_status(a["id"], "replaced", fid, invalid_at=day)
                _log("replaced", a, f"new {f['key']}", source, by=fid)
                replaced.add(a["id"])
    closing = {int(i) for i in (supersedes or []) if str(i).lstrip("-").isdigit()} - {fid} - replaced
    if f["kind"] in OPPOSITE:
        for a in facts("active", OPPOSITE[f["kind"]]):
            sa, sf = _subject(a["text"]), _subject(f["text"])
            if sa and sf and len(sa & sf) / len(sa | sf) >= 0.5:
                closing.add(a["id"])
    for a in facts("active"):
        if a["id"] in closing:
            _close(a, fid, day, s, "contradicted by a later statement")
    if f["kind"] in EVENT_KINDS:
        render_upcoming(s)
    elif f["kind"] == "about":
        set_about(f["key"], f["text"], s)
    else:
        vault.append(FILES[f["kind"]], f"- {f['text']} ({dt.date.today():%Y-%m-%d})", s)
    _log("filed", f, "", source, id=fid, date=f.get("date"))
    return True, fid


def _decide(f, s, embedder, source):
    """factdecide's verdict on candidate `f` against the active facts, logged. Never raises: a failure is ADD."""
    try:
        pool = [a for a in facts("active") if a.get("text")][-300:]
        embed = embedder or (lambda texts: ollama.embed(texts, "document", s))
        d = factdecide.decide(f, pool, s, embed)
    except Exception as e:
        d = {"action": factdecide.ADD, "target": None, "reason": f"decision failed: {type(e).__name__}",
             "via": "fallback", "neighbours": []}
    _log("decided", f, f"{d['action']} via {d['via']}: {d['reason']}", source,
         target=(d["target"] or {}).get("id"), neighbours=d.get("neighbours"))
    return d


def _embed_dup(lines, text, embedder, threshold=0.9):
    if not lines or embedder is None:
        return False
    try:
        vecs = embedder(lines + [text])
    except Exception:
        return False
    return any(_cos(vecs[-1], v) >= threshold for v in vecs[:-1])


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    n = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / n if n else 0


def file_facts(facts_, settings=None, embedder=None, threshold=0.9, source="tool"):
    """Compatibility wrapper (the `remember` tool): file each fact, return the ones that were filed."""
    filed = []
    for f in facts_:
        ok, _ = file_fact(f, settings, source=source, embedder=embedder)
        if ok:
            filed.append(f)
    return filed


def expire(now=None):
    """Mark dated events whose day has passed as expired. Returns how many."""
    today = dt.date.fromtimestamp(now or time.time()).isoformat()
    n = 0
    for f in facts("active", EVENT_KINDS):
        if f["expires"] and f["expires"] <= today:
            _set_status(f["id"], "expired", invalid_at=f["expires"])
            _log("expired", f, f"date {f['date']} has passed", f["source"])
            n += 1
    return n


def record_upcoming(events_, replies, settings=None, now=None, sources=()):
    """Calendar/mail results replace the previous calendar/mail items; conversation events are kept."""
    s = settings or Settings.load()
    ref = dt.date.fromtimestamp(now or time.time())
    old = [f for f in facts("active", EVENT_KINDS) if f["source"] in ("calendar", "mail")]
    keep, filed, rejected = set(), 0, 0
    items = [({"kind": "deadline" if str(e.get("kind")) == "deadline" else "event",
               "text": (e.get("title") or "").strip() + (f" ({e['where']})" if e.get("where") else ""),
               "date": e.get("date"), "time": e.get("time", "")}, "calendar") for e in events_]
    items += [({"kind": "reply", "date": r.get("date") or ref.isoformat(),
                "text": f"Reply to {r.get('from') or 'sender'}: {r.get('subject', '')}"
                        + (f" ({r['why']})" if r.get("why") else "")}, "mail") for r in replies]
    for item, src in items:
        f, why = check(item, ref)
        if not f:
            _log("rejected", item, why, src)
            rejected += 1
            continue
        same = next((o for o in old if o["date"] == f["date"] and _similar(o["text"], f["text"], 0.6)), None)
        if same:
            keep.add(same["id"])
            continue
        fid = _insert({**f, "source": src, "origin": src, "observed": time.time() if now is None else now,
                       "valid_from": ref.isoformat()})
        keep.add(fid)
        _log("filed", f, "", src, id=fid, date=f["date"])
        filed += 1
    for o in old:
        if o["id"] not in keep:
            _set_status(o["id"], "gone")
            _log("gone", o, "no longer in calendar/mail", o["source"])
    expire(now)
    render_upcoming(s, now=now)
    return {"events": len(events_), "replies": len(replies), "filed": filed, "rejected": rejected}


def render_upcoming(settings=None, now=None):
    """Write Knowledge/Upcoming.md from the active dated facts (expired ones drop out). Returns True if changed."""
    s = settings or Settings.load()
    expire(now)
    rows = sorted(facts("active", EVENT_KINDS), key=lambda f: (f["date"] or "", f["text"]))
    ev = [f for f in rows if f["kind"] != "reply"]
    rp = [f for f in rows if f["kind"] == "reply"]
    lines = ["---", "type: upcoming", "---", "# Upcoming", "",
             "_Dated events, deadlines and mail waiting for a reply. Kept by Waggle from your calendar, mail and "
             "what you told it; each item disappears after its day. Fix anything wrong by telling Waggle._", "",
             "## Dated"]
    for f in ev:
        day = dt.date.fromisoformat(f["date"])
        lines.append(f"- {f['date']} ({day:%a}) {f['text']}" + (" (deadline)" if f["kind"] == "deadline" else "")
                     + (f" _[{f['source']}]_" if f["source"] else ""))
    if not ev:
        lines.append("- Nothing dated yet.")
    lines += ["", "## Waiting for your reply"]
    lines += [f"- {f['date']} {f['text']}" for f in rp] or ["- Nothing waiting."]
    text = "\n".join(lines) + "\n"
    if vault.read(UPCOMING_NOTE, s).replace("\r\n", "\n") == text:
        return False
    vault.write(UPCOMING_NOTE, text, s)
    return True


def upcoming_lines(settings=None, days=4, now=None):
    """'- 2026-11-22 (Sat) Robotics meet' for dated items in the next `days` days (for the core block)."""
    today = dt.date.fromtimestamp(now or time.time())
    end = (today + dt.timedelta(days=days)).isoformat()
    out = []
    for f in sorted(facts("active", EVENT_KINDS), key=lambda f: f["date"] or ""):
        if f["expires"] and f["expires"] <= today.isoformat():
            continue
        if f["kind"] == "reply":
            out.append(f"- reply waiting: {f['text'][:100]}")
        elif today.isoformat() <= (f["date"] or "") <= end:
            day = dt.date.fromisoformat(f["date"])
            when = "today" if day == today else "tomorrow" if day == today + dt.timedelta(days=1) else f"{day:%a}"
            out.append(f"- {f['date']} ({when}) {f['text'][:100]}")
    return out


# ---- profile -------------------------------------------------------------------------------------

def set_about(key, text, settings=None):
    """Write one personal detail into Profile -> About me. Single-value keys replace their old line."""
    s = settings or Settings.load()
    text = text.strip().rstrip(".")
    prof = vault.read("Me/Profile.md", s) or "# Profile\n"
    line = f"- **{key.capitalize()}:** {text} ({dt.date.today():%Y-%m-%d})"
    m = re.search(r"## About me\n(.*?)(?=\n## |\Z)", prof, re.S)
    lines = [l for l in (m.group(1).strip().splitlines() if m else []) if l.strip()]
    if key in SINGLE:
        lines = [l for l in lines if not l.startswith(f"- **{key.capitalize()}:**")]
    elif any(text.lower() in l.lower() for l in lines):
        return False
    lines.append(line)
    block = "## About me\n" + "\n".join(lines) + "\n"
    if m:
        prof = prof[:m.start()] + block + "\n" + prof[m.end():].lstrip("\n")
    else:
        prof = prof.replace("## Basics", block + "\n## Basics", 1) if "## Basics" in prof else prof + "\n" + block
    vault.write("Me/Profile.md", prof, s)
    return True


# ---- retired per-message path (kept so callers keep working) --------------------------------------

def extract(user_msg, reply=None, settings=None, chat=None):
    """Facts from ONE message (the assistant reply is ignored). Kept for tools and tests; the daily digest is the
    real learner now."""
    chat = chat or ollama.chat_json
    msg = (user_msg or "").strip()
    if len(msg) < 4:
        return []
    try:
        out = chat([{"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": msg[:3000]}], settings=settings)
    except Exception as e:
        events.log("learn", action="extract_failed", error=f"{type(e).__name__}: {e}"[:200])
        return []
    clean = []
    for f in (out.get("facts") if isinstance(out, dict) else None) or []:
        if not (isinstance(f, dict) and f.get("kind") in FILES):
            continue
        text = str(f.get("text", "")).strip()
        if not 6 <= len(text) <= 300 or WORLD_FACT.search(text):
            continue
        item = {"kind": f["kind"], "text": text}
        if f["kind"] == "about":
            item["key"] = f.get("key") if f.get("key") in ABOUT_KEYS else "other"
        clean.append(item)
    return clean[:5]


def learn_async(user_msg, reply):
    """Retired, no-op. Per-message extraction filed passing remarks, and development test prompts, as
    permanent profile facts with no source. Learning returns with whole conversations and a recorded origin."""
    return None


# ---- weekly profile tidy-up ----------------------------------------------------------------------

CONSOLIDATE_SYSTEM = """You maintain a personal profile note for the user of an AI assistant. You get the current
profile and raw learned facts. Rewrite the profile in clean Markdown: merge duplicates, drop stale or contradicted
items (keep the newest), keep sections Basics / How I like answers / Goals / Likes / Dislikes / Current projects.
Be concise. Output only the new Markdown."""


@vault_git.recorded("learn", "weekly profile tidy-up (old profile kept in Me/History)")
def consolidate(settings=None):
    s = settings or Settings.load()
    ok, why = quota.can_escalate(s.router_model, s)
    if not ok:
        return f"skipped: {why}"
    parts = {rel: vault.read(rel, s) for rel in ["Me/Profile.md", "Me/Likes.md", "Me/Dislikes.md", "Me/Learned.md"]}
    prompt = "\n\n".join(f"=== {k} ===\n{v}" for k, v in parts.items())
    res = claude_cli.ask(prompt, model=s.router_model, system=CONSOLIDATE_SYSTEM, purpose="consolidate")
    stamp = dt.datetime.now()
    vault.write(f"Me/History/Profile-{stamp:%Y%m%d-%H%M}", parts["Me/Profile.md"], s)  # keep the old one
    vault.write("Me/Profile.md", res["text"].strip() + "\n", s)
    data_path("last_consolidate.json").write_text(json.dumps({"time": stamp.isoformat()}), encoding="utf-8")
    return "profile updated"
