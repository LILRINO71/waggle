"""Recipes: repeated work seen by ActivityWatch -> a drafted automation -> the owner's yes -> a routine Waggle runs.

The loop, each step a gate for the next:

  mine      PrefixSpan over the owner's work episodes (waggle/aw_sensor.py rows, derived facts only: program or site
            host, project, minutes). A pattern needs MIN_OCC episodes on MIN_DAYS different days. Items may be one
            step apart (noise); a pattern inside a longer one with the same support is dropped; support is counted
            in days, not events. No raw title or URL exists in this module: it never sees one.
  trigger   the pattern's start times must be regular (the middle half within 90 min), else it is blocked: a routine
            needs a time. Weekdays come from the days it was seen.
  draft     the local model (waggle.ollama.chat_json, the same call and options as the other local callers) writes a
            name, the steps and what it saves, from the tools that cover what the owner did (CATALOG). Code checks the
            answer: only catalog tools that are registered, only grounded in the pattern, only whitelisted arguments,
            no number that is not in the evidence. A rule-based draft stands in when the model is down or wrong.
            A pattern no tool covers is "blocked: no tool yet" (the owner's own work, e.g. Onshape or a calculator).
  shadow    "what it would have done": for each past day it would have fired (the owner at the PC around the time),
            the log keeps the rendered steps and whether the owner then really did the pattern. Nothing runs.
  offer     at most one suggestion a day, none while an older one is unanswered, only when the shadow log has
            SHADOW_NEEDED days and the owner did it on at least SHADOW_HIT of them, and only when outreach_ev's welcome
            odds for kind "recipe" allow it. It is sent as an initiative text, so holdq waits for a natural break.
  answer    "yes R7KD" turns it into a routine (waggle/routines.py); "no"/"no R7KD" drops it (30 days, and a "no"
            teaches the welcome odds); "never"/"never R7KD" drops it for good (a stronger lesson). A routine made
            from a recipe is removed again by "no CODE". Nothing runs before the yes; the routine's agent is told it
            may only read.

Adopt-before-build: PrefixSpan on PyPI is MIT but dormant since 2018 and has no gap limit or day support; SPMF
(GPLv3, Java) and pm4py (AGPL) are excluded by licence; so the 40-line mining below is our own, after Pei et al.
2004 and the research notes (Idea 8). Switch: config `recipes_enabled` (default true; with no activity data every call
is a no-op).

    python -m waggle.recipes list|show CODE|accept CODE|reject CODE [--never]|shadow [CODE]|mine|run [--offer]
"""
import datetime as dt
import hashlib
import json
import re
import statistics
import sys
import time

from . import autonomy_store as store_
from .config import Settings

MIN_OCC, MIN_DAYS = 3, 2          # an episode count and distinct days a pattern needs
MAX_LEN, MAX_SKIP = 4, 1          # pattern length; steps that may sit between two pattern items
EPISODE_GAP_S = 600               # a longer pause ends an episode
STEP_MIN_S = 15                   # shorter visits are glances, not steps
LOOKBACK_DAYS = 30
MAX_PATTERNS = 30
MAX_NODES = 4000                  # a safety cap on the search
IQR_MAX_MIN = 90                  # start times must be this regular
SHADOW_BACK_DAYS = 21
SHADOW_NEEDED = 3
SHADOW_HIT = 0.6
SHADOW_WINDOW_S = 7200            # the owner is "there" and the pattern "seen" within this of the trigger time
DECLINE_COOLDOWN_DAYS = 30
BARE_ANSWER_S = 3 * 3600          # a bare "no" answers an offer sent this recently
OPEN_OFFER_S = 3 * 86400          # an unanswered offer blocks the next one this long
KIND = "recipe"                   # the outreach_ev kind
JOB, TIMER, EVERY = "recipes.run", "recipes.run", "30 17 * * *"
DAYNAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Tools a recipe may use. All read-only. covers: the facets (site hosts, projects) of the owner's pattern that make the
# tool a fair stand-in for a step of it. days: (low, high, default) of its one argument.
CATALOG = {
    "google_classroom_due": {"hosts": {"classroom.google.com"}, "projects": set(), "days": (1, 14, 3),
                             "line": "check what is due in Classroom"},
    "google_calendar_list": {"hosts": {"calendar.google.com"}, "projects": set(), "days": (1, 7, 2),
                             "line": "read your calendar"},
    "google_tasks_list": {"hosts": set(), "projects": {"school"}, "days": None, "line": "read your open tasks"},
}
NICE = {"classroom.google.com": "Classroom", "docs.google.com": "Docs", "drive.google.com": "Drive",
        "calendar.google.com": "Calendar", "desmos.com": "Desmos", "onshape.com": "Onshape",
        "calculatorapp": "the calculator", "code": "VS Code", "windowsterminal": "the terminal",
        "vls": "the laser tool"}
CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_RX = r"r[2-9a-hj-km-np-z]{3}"
YES_RE = re.compile(r"^\s*(?:yes|yep|yeah|yup|ok|okay|sure|accept|approve|do it|go ahead)\b[\s,.:;-]*(" + CODE_RX +
                    r")\b[\s.!]*$", re.I)
NO_RE = re.compile(r"^\s*(no|nope|never|reject|drop|stop)\b(?:\s+(?:thanks|thank you|again))?[\s,.:;-]*(" + CODE_RX +
                   r")?\b[\s.!]*$", re.I)
BARE_NO = {"no", "nope", "never"}


def enabled(settings=None):
    s = settings or Settings.load()
    return bool(getattr(s, "recipes_enabled", True))


# ---- mining -----------------------------------------------------------------------------------------------------

def unit_of(row):
    """One step's name: a kept site host, else the program (a browser on an unknown site is 'chrome:web')."""
    from . import aw_sensor
    if row["app"] in aw_sensor.BROWSERS:
        return row.get("host") or f"{row['app']}:{row.get('project') or 'web'}"
    return row["app"]


def episodes(rows):
    """(episodes, meta). episodes: [{day, steps: [[unit, start, end]]}] of work only: system windows, glances and AFK
    rows are skipped, leisure and a pause over EPISODE_GAP_S end an episode. meta: {unit: {hosts, projects}}."""
    from . import aw_sensor
    steps, meta = [], {}
    for r in sorted((r for r in rows if not r.get("afk")), key=lambda r: r["start"]):
        if r["app"] in aw_sensor.NOISE_APPS or r["secs"] < STEP_MIN_S:
            continue
        if aw_sensor.is_leisure(r.get("project")):
            steps.append(None)
            continue
        u = unit_of(r)
        m = meta.setdefault(u, {"hosts": set(), "projects": set()})
        if r.get("host"):
            m["hosts"].add(r["host"])
        if r.get("project"):
            m["projects"].add(r["project"])
        a, b = r["start"], r["start"] + r["secs"]
        if steps and steps[-1] and steps[-1][0] == u and a - steps[-1][2] <= EPISODE_GAP_S:
            steps[-1][2] = max(steps[-1][2], b)
        else:
            steps.append([u, a, b])
    out, cur = [], []
    for st in steps + [None]:
        if st is None or (cur and st[1] - cur[-1][2] > EPISODE_GAP_S):
            if len(cur) >= 2:
                out.append({"day": dt.date.fromtimestamp(cur[0][1]).isoformat(), "steps": cur})
            cur = []
        if st is not None:
            cur.append(st)
    return out, meta


def prefixspan(eps, min_days=MIN_DAYS, max_len=MAX_LEN, max_skip=MAX_SKIP):
    """Frequent patterns of 2..max_len items: [(pattern tuple, {episode index: {end position: start position}})].
    PrefixSpan with a gap limit: the next item sits within max_skip steps of the last. Support is distinct days."""
    out, nodes = [], [0]

    def days(sub):
        return {eps[i]["day"] for i in sub}

    def grow(prefix, proj):
        cand = {}
        for ei, ends in proj.items():
            units = eps[ei]["steps"]
            for end, start in ends.items():
                for p in range(end + 1, min(end + 2 + max_skip, len(units))):
                    slot = cand.setdefault(units[p][0], {}).setdefault(ei, {})
                    slot[p] = min(start, slot.get(p, start))
        for item, sub in cand.items():
            if len(days(sub)) < min_days or nodes[0] >= MAX_NODES:
                continue
            nodes[0] += 1
            pat = prefix + (item,)
            out.append((pat, sub))
            if len(pat) < max_len:
                grow(pat, sub)

    first = {}
    for ei, e in enumerate(eps):
        for p, st in enumerate(e["steps"]):
            first.setdefault(st[0], {}).setdefault(ei, {})[p] = p
    for item, sub in first.items():
        if len(days(sub)) >= min_days:
            grow((item,), sub)
    return out


def _is_subseq(a, b):
    it = iter(b)
    return all(x in it for x in a)


def _quantile(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * (len(vals) - 1) + 0.5))]


def _minute(ts):
    d = dt.datetime.fromtimestamp(ts)
    return d.hour * 60 + d.minute


def _hhmm(m):
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


def _nice_time(m):
    h, mi = divmod(int(m), 60)
    return f"{(h + 11) % 12 + 1}:{mi:02d} {'AM' if h < 12 else 'PM'}"


def trigger_of(occ):
    """{weekdays, minute, fire, schedule, text} from the start times of the occurrences, or (None, reason) when the
    start times are too scattered for a routine to be useful."""
    mins = [_minute(o[0]) for o in occ]
    med = statistics.median(mins)
    p25, p75 = _quantile(mins, 0.25), _quantile(mins, 0.75)
    near = sum(1 for m in mins if abs(m - med) <= 120) / len(mins)
    if p75 - p25 > IQR_MAX_MIN or near < 0.8:
        return None, "no regular time of day"
    wd = sorted({dt.datetime.fromtimestamp(o[0]).weekday() for o in occ})
    if len(wd) >= 4 and all(d < 5 for d in wd):
        wd = [0, 1, 2, 3, 4]
    fire = max(0, int((p25 - 5) // 5 * 5))
    hh, mm = divmod(fire, 60)
    if wd == [0, 1, 2, 3, 4]:
        sched, words = f"weekdays {hh:02d}:{mm:02d}", "weekdays"
    elif len(wd) == 1:
        sched, words = f"weekly {DAYNAMES[wd[0]].lower()} {hh:02d}:{mm:02d}", f"every {DAYNAMES[wd[0]]}"
    else:
        sched = f"{mm} {hh} * * " + ",".join(str((d + 1) % 7) for d in wd)
        words = "/".join(DAYNAMES[d] for d in wd)
    return {"weekdays": wd, "minute": fire, "median": med, "schedule": sched,
            "text": f"{words} around {_nice_time(med)}"}, ""


def mine(rows, now=None):
    """Strong repeated patterns, best first: [{key, label, pattern, evidence}]. Pure: rows in, patterns out."""
    now = store_.now_or(now)
    rows = [r for r in rows if r["start"] >= now - LOOKBACK_DAYS * 86400]
    eps, meta = episodes(rows)
    found = []
    for pat, sub in prefixspan(eps):
        if len(set(pat)) < 2 or len(sub) < MIN_OCC:
            continue
        occ = []
        for ei, ends in sub.items():
            steps = eps[ei]["steps"]
            end, start = min(ends.items(), key=lambda kv: (kv[1], kv[0]))
            occ.append([steps[start][1], steps[end][2], eps[ei]["day"]])
        if len({o[2] for o in occ}) < MIN_DAYS:
            continue
        found.append((pat, sorted(occ), set(sub)))
    keep = []
    for pat, occ, eset in found:
        # drop a pattern inside a longer one that occurs in every episode it does; and a longer one that is only
        # a shorter one plus an extra step the owner takes now and then (under 60 % of the shorter one's episodes)
        if any(len(q) > len(pat) and eset <= qe and _is_subseq(pat, q) for q, _, qe in found) or                 any(len(q) == len(pat) - 1 and _is_subseq(q, pat) and len(eset) < 0.6 * len(qe) for q, _, qe in found):
            continue
        keep.append((pat, occ))
    out = []
    for pat, occ in keep:
        spans = [(o[1] - o[0]) / 60 for o in occ]
        facets = {"hosts": sorted({h for u in pat for h in meta.get(u, {}).get("hosts", ())}),
                  "projects": sorted({p for u in pat for p in meta.get(u, {}).get("projects", ())})}
        ev = {"count": len(occ), "days": len({o[2] for o in occ}), "last": max(o[0] for o in occ),
              "median_min": round(statistics.median(spans), 1), "facets": facets, "occ": occ[-60:]}
        out.append({"key": hashlib.sha1(" > ".join(pat).encode()).hexdigest()[:10], "label": " > ".join(pat),
                    "pattern": list(pat), "evidence": ev, "value": ev["days"] * max(ev["median_min"], 1.0)})
    out.sort(key=lambda p: -p["value"])
    return out[:MAX_PATTERNS]


# ---- the store --------------------------------------------------------------------------------------------------

JSON_COLS = ("pattern", "evidence", "trigger", "draft", "shadow")


def _db():
    con = store_.db()
    con.executescript("""CREATE TABLE IF NOT EXISTS recipes (code TEXT PRIMARY KEY, key TEXT UNIQUE, label TEXT,
        status TEXT, note TEXT, pattern TEXT, evidence TEXT, trigger TEXT, draft TEXT, shadow TEXT, offered_at REAL,
        offer_outbox INTEGER, answered_at REAL, answer TEXT, routine TEXT, created REAL, updated REAL)""")
    return con


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS:
        d[k] = store_.loads(d.get(k), None)
    return d


def get(code):
    con = _db()
    try:
        return _row(con.execute("SELECT * FROM recipes WHERE code=?", (str(code or "").upper(),)).fetchone())
    finally:
        con.close()


def all_recipes(status=None):
    con = _db()
    try:
        q, a = "SELECT * FROM recipes", ()
        if status:
            q, a = q + " WHERE status IN (%s)" % ",".join("?" * len(status)), tuple(status)
        return [_row(r) for r in con.execute(q + " ORDER BY created, code", a)]
    finally:
        con.close()


def _set(code, **fields):
    fields["updated"] = time.time()
    con = _db()
    try:
        con.execute(f"UPDATE recipes SET {', '.join(f'{k}=?' for k in fields)} WHERE code=?",
                    (*[store_.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()], code))
        con.commit()
    finally:
        con.close()


def _new_code(con, key):
    n = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    for i in range(200):
        x = n + i * 7919
        code = "R" + "".join(CODE_CHARS[(x >> (5 * j)) % 32] for j in range(3))
        if not con.execute("SELECT 1 FROM recipes WHERE code=?", (code,)).fetchone():
            return code
    raise RuntimeError("no free recipe code")


def save(found, now=None):
    """Insert new patterns ('found') and refresh the evidence of known ones; their status and draft are kept."""
    now = store_.now_or(now)
    con = _db()
    try:
        for p in found:
            row = con.execute("SELECT code FROM recipes WHERE key=?", (p["key"],)).fetchone()
            if row:
                con.execute("UPDATE recipes SET evidence=?, updated=? WHERE key=?",
                            (store_.dumps(p["evidence"]), now, p["key"]))
            else:
                con.execute("INSERT INTO recipes(code, key, label, status, pattern, evidence, created, updated) "
                            "VALUES(?,?,?,?,?,?,?,?)", (_new_code(con, p["key"]), p["key"], p["label"], "found",
                                                        store_.dumps(p["pattern"]), store_.dumps(p["evidence"]),
                                                        now, now))
        con.commit()
    finally:
        con.close()


# ---- drafting ---------------------------------------------------------------------------------------------------

def available_tools():
    """Names of the tools Waggle's chat really has (the MCP registry); empty when it cannot be loaded."""
    try:
        from . import waggle_mcp
        return set(waggle_mcp.TOOLS)
    except Exception:  # noqa: BLE001
        return set()


def grounded(evidence, tools):
    """Catalog tools that are registered and cover what the owner did, in catalog order."""
    f = evidence.get("facets") or {}
    hosts, projects = set(f.get("hosts") or ()), set(f.get("projects") or ())
    return [n for n, c in CATALOG.items() if n in tools and (c["hosts"] & hosts or c["projects"] & projects)]


def _days_arg(name, val):
    spec = CATALOG[name]["days"]
    if not spec:
        return None
    try:
        v = int(val)
    except (TypeError, ValueError):
        v = spec[2]
    return max(spec[0], min(spec[1], v))


def _step(name, days=None):
    d = _days_arg(name, days)
    return {"tool": name, **({"days": d} if d is not None else {})}


def render_step(st):
    line = CATALOG[st["tool"]]["line"]
    return line + (f" (next {st['days']} day{'s' * (st['days'] != 1)})" if st.get("days") else "")


def chain(pattern):
    return ", then ".join(NICE.get(u, u.replace(":web", " (web)")) for u in pattern)


def _slug(text, fallback="recipe"):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:36].strip("-")
    return s or fallback


SCHEMA = {"type": "object", "properties": {
    "name": {"type": "string"}, "summary": {"type": "string"}, "saves": {"type": "string"},
    "steps": {"type": "array", "items": {"type": "object", "properties": {
        "tool": {"type": "string"}, "days": {"type": "integer"}}, "required": ["tool"]}}},
    "required": ["name", "steps", "saves", "summary"]}
SYSTEM = ("You design one small automation for a personal assistant from a pattern of the owner's repeated work. "
          "Choose steps ONLY from the tools listed, in a sensible order, and only those that replace part of what the "
          "owner does by hand. Reply JSON: name (2-4 words, lower case), summary (one plain sentence: what the "
          "assistant will do), saves (one plain sentence: what it spares the owner; use only numbers given), steps "
          "[{tool, days?}]. No other tools, no writing, deleting or sending.")


def _numbers(text):
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def _clean_text(text, limit):
    t = re.sub(r"https?://\S+", "", str(text or ""))
    return " ".join(t.split())[:limit]


def validate_draft(raw, ok_tools, evidence, trig):
    """The model's answer, checked: only grounded tools, clamped arguments, a clean name, no invented number.
    Returns the draft dict or None."""
    if not isinstance(raw, dict):
        return None
    steps, seen = [], set()
    for st in raw.get("steps") or []:
        name = st.get("tool") if isinstance(st, dict) else None
        if name in ok_tools and name not in seen and len(steps) < 4:
            seen.add(name)
            steps.append(_step(name, st.get("days")))
    if not steps:
        return None
    allowed = _numbers(json.dumps([evidence["count"], evidence["days"], evidence["median_min"]]) + trig["text"]) | {
        str(CATALOG[s["tool"]]["days"][2]) for s in steps if CATALOG[s["tool"]]["days"]} | {
        str(s["days"]) for s in steps if s.get("days")}
    saves = _clean_text(raw.get("saves"), 180)
    summary = _clean_text(raw.get("summary"), 180)
    if not saves or not summary or (_numbers(saves) | _numbers(summary)) - allowed:
        return None
    name = _slug(raw.get("name"), "")
    if not name:
        return None
    return {"name": name, "steps": steps, "saves": saves, "summary": summary, "by": "model"}


def rule_draft(ok_tools, evidence, trig):
    steps = [_step(n) for n in ok_tools][:4]
    what = " and ".join(render_step(s).split(" (")[0] for s in steps)
    return {"name": _slug(f"{trig['text'].split()[0]} {steps[0]['tool'].replace('google_', '').replace('_', ' ')}"),
            "steps": steps, "summary": f"I will {what} before you start, and text you the short list.",
            "saves": f"You spend about {evidence['median_min']:g} min on this stretch; I would hand you the "
                     f"lookup part first.", "by": "rules"}


def draft(rec, tools, settings=None, chat=None):
    """(draft, trigger, blocker): the recipe for a mined pattern, or why there is none."""
    ev = rec["evidence"]
    trig, why = trigger_of(ev["occ"])
    if trig is None:
        return None, None, why
    ok = grounded(ev, tools)
    if not ok:
        return None, trig, "no tool yet covers these steps (the owner's own work)"
    cat = {n: {"does": CATALOG[n]["line"], "days": CATALOG[n]["days"] and list(CATALOG[n]["days"][:2])} for n in ok}
    user = json.dumps({"pattern": chain(rec["pattern"]), "when": trig["text"], "times": ev["count"],
                       "on_days": ev["days"], "median_minutes": ev["median_min"], "tools": cat}, ensure_ascii=False)
    d = None
    try:
        from . import ollama
        ask = chat or ollama.chat_json
        raw = ask([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], schema=SCHEMA,
                  settings=settings, timeout=150)
        d = validate_draft(raw, set(ok), ev, trig)
    except Exception:  # noqa: BLE001 - no model: the rule draft below
        d = None
    return d or rule_draft(ok, ev, trig), trig, ""


# ---- shadow runs ------------------------------------------------------------------------------------------------

def shadow(rec, rows, now=None):
    """Log, for each past day the recipe would have fired, what it would have done and whether the owner did the
    pattern then. Pure bookkeeping: no tool is called, nothing is sent. Returns the new entries."""
    now = store_.now_or(now)
    trig, dr, ev = rec["trigger"], rec["draft"], rec["evidence"]
    log = list(rec.get("shadow") or [])
    done = {e["date"] for e in log}
    first = min(o[0] for o in ev["occ"])
    present = [r["start"] for r in rows if not r.get("afk")]
    new = []
    today = dt.date.fromtimestamp(now)
    for back in range(SHADOW_BACK_DAYS, -1, -1):
        d = today - dt.timedelta(days=back)
        if d.weekday() not in trig["weekdays"] or d.isoformat() in done:
            continue
        fire = dt.datetime.combine(d, dt.time(trig["minute"] // 60, trig["minute"] % 60)).timestamp()
        if fire > now or fire < first - 86400:
            continue
        if not any(abs(t - fire) <= SHADOW_WINDOW_S for t in present):
            continue  # the owner was not at the PC: no evidence either way
        new.append({"date": d.isoformat(), "fire": fire, "ran": False,
                    "would": [render_step(s) for s in dr["steps"]],
                    "owner_did_it": any(abs(o[0] - fire) <= SHADOW_WINDOW_S for o in ev["occ"])})
    return new


def shadow_stats(rec, now=None):
    now = store_.now_or(now)
    log = [e for e in rec.get("shadow") or [] if e["fire"] >= now - SHADOW_BACK_DAYS * 86400]
    hits = sum(1 for e in log if e["owner_did_it"])
    return {"days": len(log), "hits": hits, "rate": hits / len(log) if log else 0.0}


def ready(rec, now=None):
    st = shadow_stats(rec, now)
    return st["days"] >= SHADOW_NEEDED and st["rate"] >= SHADOW_HIT


# ---- the pass ---------------------------------------------------------------------------------------------------

def _rows(now, rows=None):
    if rows is not None:
        return rows
    from . import aw_sensor
    return aw_sensor.slices_since(now - LOOKBACK_DAYS * 86400)


def refresh(settings=None, now=None, rows=None, tools=None, chat=None):
    """Mine, draft what is new or blocked, run the shadow log. Sends nothing. Returns counts."""
    s = settings or Settings.load()
    now = store_.now_or(now)
    if not enabled(s):
        return {"skipped": "recipes_enabled is off"}
    rows = _rows(now, rows)
    out = {"patterns": 0, "drafted": 0, "blocked": 0, "shadow_entries": 0}
    if not rows:
        return out
    found = mine(rows, now)
    out["patterns"] = len(found)
    save(found, now)
    tools = available_tools() if tools is None else set(tools)
    for rec in all_recipes(("found", "blocked")):
        d, trig, why = draft(rec, tools, s, chat)
        if d:
            _set(rec["code"], status="drafted", note="", draft=d, trigger=trig)
            out["drafted"] += 1
        else:
            _set(rec["code"], status="blocked", note=why, trigger=trig)
            out["blocked"] += 1
    for rec in all_recipes(("drafted", "shadow")):
        new = shadow(rec, rows, now)
        if new or rec["status"] == "drafted":
            _set(rec["code"], status="shadow", shadow=((rec.get("shadow") or []) + new)[-40:])
        out["shadow_entries"] += len(new)
    for rec in all_recipes(("declined",)):  # a "no" lapses after the cooldown, once the habit has kept going
        if rec["answered_at"] and now - rec["answered_at"] > DECLINE_COOLDOWN_DAYS * 86400 and \
                rec["evidence"]["last"] > rec["answered_at"]:
            _set(rec["code"], status="shadow", answer="")
    return out


# ---- offering ---------------------------------------------------------------------------------------------------

def offer_text(rec, now=None):
    ev, dr, trig = rec["evidence"], rec["draft"], rec["trigger"]
    st = shadow_stats(rec, now)
    steps = " and ".join(render_step(x) for x in dr["steps"])
    return (f"🐝 You tend to do this {trig['text']}: {chain(rec['pattern'])} ({ev['count']} times on {ev['days']} "
            f"days, about {ev['median_min']:g} min each). I could {steps} a few minutes before and text you the short "
            f"list. {dr['saves']} On paper over the last {st['days']} days it would have fired {st['days']} times "
            f"and you did it on {st['hits']} of them. Reply \"yes {rec['code']}\" to make it a routine, \"no "
            f"{rec['code']}\" to drop it, or \"never {rec['code']}\" to stop these. Nothing runs until you say yes.")


def _local_midnight(now):
    return dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _decision_row(rec, oid, text, now):
    """An initiative decision for the offer, so outreach_ev labels it welcome/ignored like any other suggestion."""
    from . import initiative
    con = initiative._db()
    try:
        con.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"recipe-{rec['code']}-{int(now)}", now, f"recipe:{rec['code']}", KIND, f"recipe:{rec['code']}",
                     "text", "text", text[:400], "recipe suggestion", "", "recipes", str(oid), 0))
        con.commit()
    finally:
        con.close()


def offer(settings=None, now=None):
    """Send at most one suggestion a day. Returns {"offered": code} or {"skipped": why}."""
    from . import notify
    s = settings or Settings.load()
    now = store_.now_or(now)
    if not enabled(s):
        return {"skipped": "recipes_enabled is off"}
    recs = all_recipes()
    if any(r["offered_at"] and r["offered_at"] >= _local_midnight(now) for r in recs):
        return {"skipped": "one suggestion a day: already offered today"}
    if any(r["status"] == "offered" and now - (r["offered_at"] or 0) < OPEN_OFFER_S for r in recs):
        return {"skipped": "an earlier suggestion is still unanswered"}
    cands = [r for r in recs if r["status"] in ("drafted", "shadow") and ready(r, now)]
    if not cands:
        return {"skipped": "nothing ready (needs a shadow log of regular days)"}
    rec = max(cands, key=lambda r: r["evidence"]["days"] * r["evidence"]["median_min"])
    if notify.in_quiet_hours(s, now):
        return {"skipped": "quiet hours"}
    try:  # the welcome odds for this kind of message (waggle/outreach_ev.py)
        from . import initiative, outreach_ev
        ok, why = outreach_ev.allow({"kind": KIND}, s, now, {"cap_used": initiative.sent_today(now, s)})
    except Exception:  # noqa: BLE001 - a bug in the odds never stops the loop; the daily cap above still holds
        ok, why = True, ""
    if not ok:
        return {"skipped": why}
    text = offer_text(rec, now)
    row = notify.deliver("text", text, origin="initiative", ref=f"recipe:{rec['code']}:{int(now)}",
                         topic=f"recipe:{rec['code']}", settings=s, now=now)
    if row.get("id") is None:
        return {"skipped": "the message was suppressed (developer or test source)"}
    _set(rec["code"], status="offered", offered_at=now, offer_outbox=row["id"])
    try:
        _decision_row(rec, row["id"], text, now)
    except Exception:  # noqa: BLE001 - labelling is a nicety
        pass
    return {"offered": rec["code"], "outbox": row["id"]}


def run(settings=None, now=None, rows=None, tools=None, chat=None, send=True):
    s = settings or Settings.load()
    out = refresh(s, now, rows, tools, chat)
    if send and "skipped" not in out:
        out["offer"] = offer(s, now)
    return out


def run_job(payload=None, *, settings=None, now=None):
    from .core import offswitch
    if offswitch.is_off():
        return {"skipped": "Waggle is switched off"}
    return run(settings, now)


def register_core(now=None):
    """Called by the core at every start (idempotent): the job and one timer a day, 17:30."""
    from .core import jobs, timers
    jobs.register(JOB, run_job, detached=True, max_attempts=1, lease_s=120.0, timeout_s=600.0)
    timers.add(TIMER, "cron", EVERY, JOB, catch_up="skip", origin="recipes", now=now)
    return {"jobs": [JOB], "timers": [TIMER]}


# ---- answers ----------------------------------------------------------------------------------------------------

def task_text(rec):
    steps = "; ".join(f"{i}. {st['tool']}" + (f" with days={st['days']}" if st.get("days") else "")
                      for i, st in enumerate(rec["draft"]["steps"], 1))
    return (f"Recipe {rec['code']} ({rec['draft']['name']}), {rec['trigger']['text']}. Use these tools in this order: "
            f"{steps}. Tell the owner, in at most 4 short lines, only what is due soon or open. Read only: never "
            f"create, change, send or delete anything. If there is nothing worth saying, reply exactly NOTHING_NEW.")


def accept(code, settings=None, now=None, tools=None):
    """The owner's yes: make the routine. Returns (ok, message)."""
    from . import routines
    s = settings or Settings.load()
    now = store_.now_or(now)
    rec = get(code)
    if not rec:
        return False, f"I have no recipe {code}."
    if rec["status"] == "accepted":
        return True, f"{rec['code']} is already a routine ({rec['routine']})."
    if rec["status"] not in ("offered", "drafted", "shadow") or not rec.get("draft"):
        return False, f"{rec['code']} is {rec['status']}; it cannot be turned on."
    avail = available_tools() if tools is None else set(tools)
    missing = [st["tool"] for st in rec["draft"]["steps"] if st["tool"] not in avail]
    if missing:
        return False, f"I can't turn {rec['code']} on: the tool {missing[0]} is not available right now."
    name = f"recipe-{rec['draft']['name']}"
    try:
        routines.add(name, rec["trigger"]["schedule"], task_text(rec), agent="general", deliver="telegram",
                     source=f"recipe:{rec['code']}")
    except ValueError as e:
        return False, f"I could not schedule {rec['code']}: {e}"
    _set(rec["code"], status="accepted", answered_at=now, answer="yes", routine=name)
    _teach(rec, "welcome", now)
    return True, (f"Done. Routine {name} will run {rec['trigger']['text']}: " +
                  " and ".join(render_step(x) for x in rec["draft"]["steps"]) +
                  f". Say \"no {rec['code']}\" and I remove it.")


def _teach(rec, label, now):
    try:
        from . import outreach_ev
        outreach_ev.teach(rec.get("offer_outbox"), KIND, label, rec.get("offered_at"), now)
    except Exception:  # noqa: BLE001
        pass


def reject(code, never=False, now=None):
    """The owner's no (30 days) or never (for good). Removes the routine of an accepted recipe. (ok, message)."""
    from . import routines
    now = store_.now_or(now)
    rec = get(code)
    if not rec:
        return False, f"I have no recipe {code}."
    if rec["routine"]:
        routines.remove(rec["routine"])
    _set(rec["code"], status="rejected" if never else "declined", answered_at=now, answer="never" if never else "no",
         routine="")
    _teach(rec, "never" if never else "unwelcome", now)
    return True, (f"Understood: {rec['code']} is dropped for good, and I will suggest fewer of these." if never else
                  f"Dropped {rec['code']}. I won't raise it again for {DECLINE_COOLDOWN_DAYS} days.")


def _owner_chats(s):
    return {f"telegram:{str(c).strip()}" for c in (getattr(s, "engine_chats", None) or []) if str(c).strip()}


def _offer_sent_at(rec):
    from . import notify
    try:
        row = notify.get(rec["offer_outbox"])
    except Exception:  # noqa: BLE001
        return None
    return row.get("sent_at") if row and row.get("status") == "sent" else None


def preflight(chat, text, *, origin, kind="text", settings=None, now=None, tools=None):
    """The owner's "yes CODE" / "no [CODE]" / "never [CODE]". Returns the reply (no model is asked) or None when this is
    not that. Only typed words in the owner's own engine chat count. A bare no/never answers the single offer that
    was delivered within BARE_ANSWER_S; a yes always needs its code."""
    if kind != "text" or not text or len(text) > 60:
        return None
    s = settings or Settings.load()
    if not enabled(s) or origin not in ("user", "canary") or chat not in _owner_chats(s):
        return None
    now = store_.now_or(now)
    m = YES_RE.match(text)
    if m:
        if not get(m.group(1)):
            return None
        return accept(m.group(1), s, now, tools)[1]
    m = NO_RE.match(text)
    if not m:
        return None
    verb, code = m.group(1).lower(), (m.group(2) or "").upper()
    if code:
        return reject(code, never=verb == "never", now=now)[1] if get(code) else None
    if verb not in BARE_NO:
        return None
    open_ = [r for r in all_recipes(("offered",)) if (_offer_sent_at(r) or 0) >= now - BARE_ANSWER_S]
    if len(open_) != 1:
        return None
    return reject(open_[0]["code"], never=verb == "never", now=now)[1]


# ---- reporting --------------------------------------------------------------------------------------------------

def evening_line(now=None, settings=None):
    """One line for the evening note when a suggestion went out in the last day and is still unanswered."""
    s = settings or Settings.load()
    if not enabled(s):
        return ""
    now = store_.now_or(now)
    try:
        recs = [r for r in all_recipes(("offered",)) if (r["offered_at"] or 0) >= now - 86400]
    except Exception:  # noqa: BLE001
        return ""
    if not recs:
        return ""
    r = recs[0]
    return (f"New automation idea ({r['code']}): {r['draft']['name']}, {r['trigger']['text']}. Reply \"yes {r['code']}\" "
            f"to turn it on, \"no {r['code']}\" to drop it.")


def summary(settings=None):
    """{counts: {status: n}, open: [{code, name, when}]} for the dashboard."""
    s = settings or Settings.load()
    if not enabled(s):
        return {"enabled": False, "counts": {}, "open": []}
    recs = all_recipes()
    counts = {}
    for r in recs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"enabled": True, "counts": counts,
            "open": [{"code": r["code"], "name": r["draft"]["name"], "when": r["trigger"]["text"]}
                     for r in recs if r["status"] == "offered"]}


def _show(rec, now=None):
    ev, st = rec["evidence"], shadow_stats(rec, now)
    lines = [f"{rec['code']}  [{rec['status']}]  {chain(rec['pattern'])}",
             f"  seen {ev['count']} times on {ev['days']} days, about {ev['median_min']:g} min each; sites {ev['facets']['hosts']}"
             f" projects {ev['facets']['projects']}"]
    if rec.get("note"):
        lines.append(f"  blocked: {rec['note']}")
    if rec.get("trigger"):
        lines.append(f"  trigger: {rec['trigger']['text']}  (routine schedule: {rec['trigger']['schedule']})")
    if rec.get("draft"):
        dr = rec["draft"]
        lines += [f"  draft ({dr['by']}): {dr['name']}", f"  steps: " + "; ".join(render_step(x) for x in dr["steps"]),
                  f"  saves: {dr['saves']}"]
    if rec.get("shadow"):
        lines.append(f"  shadow: {st['hits']} of {st['days']} days the owner did it (needs {SHADOW_NEEDED} days at "
                     f"{SHADOW_HIT:.0%}); nothing was run")
        lines += [f"    {e['date']} {'did it' if e['owner_did_it'] else 'did not'}: would " + "; ".join(e["would"])
                  for e in rec["shadow"][-5:]]
    if rec.get("routine"):
        lines.append(f"  routine: {rec['routine']}")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    cmd, args = (argv[0] if argv else "list"), argv[1:]
    code = args[0].upper() if args and not args[0].startswith("-") else None
    if cmd == "list":
        recs = all_recipes()
        for r in recs:
            ev = r["evidence"]
            print(f"{r['code']}  [{r['status']:<8}] {chain(r['pattern'])}  x{ev['count']} on {ev['days']} days"
                  + (f"  ({r['note']})" if r.get("note") else ""))
        if not recs:
            print("no recipes yet (python -m waggle.recipes run)")
    elif cmd == "show" and code:
        rec = get(code)
        print(_show(rec) if rec else f"no recipe {code}")
        return 0 if rec else 1
    elif cmd == "accept" and code:
        ok, msg = accept(code)
        print(msg)
        return 0 if ok else 1
    elif cmd == "reject" and code:
        ok, msg = reject(code, never="--never" in argv)
        print(msg)
        return 0 if ok else 1
    elif cmd == "shadow":
        now, rows = time.time(), _rows(time.time())
        for rec in (all_recipes(("drafted", "shadow")) if not code else [get(code)]):
            if not rec or not rec.get("draft"):
                continue
            new = shadow(rec, rows, now)
            if new:
                _set(rec["code"], status="shadow", shadow=((rec.get("shadow") or []) + new)[-40:])
            print(_show(get(rec["code"])))
    elif cmd == "mine":
        for p in mine(_rows(time.time())):
            print(f"x{p['evidence']['count']} on {p['evidence']['days']} days: {chain(p['pattern'])}")
        print("(not saved; `run` saves)")
    elif cmd == "run":
        print(json.dumps(run(send="--offer" in argv), indent=2, default=str))
    else:
        print(__doc__.strip().splitlines()[-1])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
