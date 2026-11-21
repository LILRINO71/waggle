"""Remembering conversations: a real daily note from traces.db, a short "Yesterday" block, and outcome capture.

For each day with turns:
- a deterministic skeleton (no model): conversations grouped by session and pauses, with what was asked and how
  each turn ended (answered, agent run, failed, local fallback). Written every run into Journal/<day>.md inside a
  marked block, so it never touches anything else in the note.
- once the day is over (and once in the evening for today), one capable-model call under the background budget
  distils the whole day: per conversation what was asked, found, decided and left open; facts the USER stated
  (each with the user's exact words, checked against the trace); outcomes the agent verifiably produced (each tied
  to a trace whose reply or tool results contain it). Verified facts go to the fact model (learn.py), outcomes and
  decisions to the right project note's Log/Decisions. Everything rejected is logged with the reason.

Pulled on a schedule by the ingest job; no plugin hooks."""
import datetime as dt
import difflib
import json
import re
import time

from . import events, kstore, projects, vault, vault_git
from .config import Settings

GAP_S = 30 * 60
BACKGROUND = ("objective-", "routine-")
LOCAL_MARK = re.compile(r"local|waggle-4b|ollama|qwen|gemma", re.I)

DISTILL_SYSTEM = """You write the daily memory of a personal AI assistant from one day of its conversation log.
You get the turns: id, time, the USER's message, the assistant's reply, and tool steps with their results.
Return JSON with:
- "summary": 2-3 plain sentences on what the day was about.
- "conversations": per conversation {"time": "HH:MM", "project": project name or "", "asked": what the user wanted,
  "found": what the assistant found or produced (concrete: names, numbers, files), "decided": what the USER decided,
  "open": what is still unresolved or promised}. Empty strings when nothing.
- "facts": lasting or dated facts about the USER that the USER stated in their own messages. Each {"kind":
  "about|like|dislike|preference|goal|person|project|event|deadline", "key": for about: name|home|school|grade|age|
  job|schedule|routine|place|family|health|other, "text": the fact in third person, "date": YYYY-MM-DD for
  events/deadlines resolved from the message's date (e.g. "Saturday" said on 2026-11-19 is 2026-11-21), "quote":
  the user's exact words that state it}. Never take facts from the assistant's replies, web results or test
  prompts that are obviously about the assistant itself. Most days have few or none. "supersedes": ids of the
  KNOWN FACTS (given with the turns) that this fact contradicts or replaces because the user changed their mind
  or their situation (empty when it only adds something).
- "outcomes": durable results the assistant verifiably produced or found: {"trace_id": the turn id, "project":
  project name or "", "type": "found|built|decided|sent", "text": one sentence with the concrete result}.
  Only what the reply or tool results actually show. Decisions count only when the user made them.
Use the project names given when they fit. No guesses, no secrets."""
DISTILL_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"},
    "conversations": {"type": "array", "items": {"type": "object", "properties": {
        "time": {"type": "string"}, "project": {"type": "string"}, "asked": {"type": "string"},
        "found": {"type": "string"}, "decided": {"type": "string"}, "open": {"type": "string"}}}},
    "facts": {"type": "array", "items": {"type": "object", "properties": {
        "kind": {"type": "string"}, "key": {"type": "string"}, "text": {"type": "string"},
        "date": {"type": "string"}, "quote": {"type": "string"},
        "supersedes": {"type": "array", "items": {"type": "integer"}}}, "required": ["kind", "text", "quote"]}},
    "outcomes": {"type": "array", "items": {"type": "object", "properties": {
        "trace_id": {"type": "integer"}, "project": {"type": "string"}, "type": {"type": "string"},
        "text": {"type": "string"}}, "required": ["trace_id", "text"]}}},
    "required": ["summary", "conversations", "facts", "outcomes"]}


def _day(ts):
    return dt.date.fromtimestamp(ts)


def day_bounds(day):
    t0 = time.mktime(dt.datetime.combine(day, dt.time()).timetuple())
    return t0, t0 + 86400


def day_traces(day):
    from . import learn
    t0, t1 = day_bounds(day)
    return sorted(learn.user_turns(t0, t1), key=lambda r: r["id"])  # the user's own turns only (origin)


def is_background(r):
    return (r.get("session") or "").startswith(BACKGROUND)


def outcome_of(r):
    """How a turn ended, in a word or two."""
    if not r.get("ok"):
        return "failed"
    if LOCAL_MARK.search(f"{r.get('route', '')} {r.get('model', '')}") and r.get("route") != "sonnet":
        return "local model"
    if r.get("steps"):
        return f"agent, {len(r['steps'])} steps" + (", approval needed" if any(s.get("denied") for s in
                                                                               r["steps"]) else "")
    return "answered"


def conversations(rows, gap=GAP_S):
    """Interactive turns grouped into conversations: same session, no pause longer than `gap`."""
    convs = []
    for r in [r for r in rows if not is_background(r)]:
        last = convs[-1] if convs else None
        if last and last[-1]["session"] == r["session"] and r["time"] - last[-1]["time"] <= gap:
            last.append(r)
        else:
            convs.append([r])
    return convs


def skeleton(day, rows):
    """Deterministic digest (no model): conversations with what was asked and how each turn ended."""
    lines = []
    for conv in conversations(rows):
        t = dt.datetime.fromtimestamp(conv[0]["time"])
        where = conv[0].get("platform") or "chat"
        lines.append(f"- {t:%H:%M} ({where}, {len(conv)} turn{'s' if len(conv) > 1 else ''})")
        for r in conv[:6]:
            req = re.sub(r"\s+", " ", r.get("request") or "").strip()
            lines.append(f"  - asked: {req[:140]}{'...' if len(req) > 140 else ''} -> {outcome_of(r)}")
        if len(conv) > 6:
            lines.append(f"  - ... {len(conv) - 6} more")
    bg = [r for r in rows if is_background(r)]
    if bg:
        ok = sum(1 for r in bg if r.get("ok"))
        lines.append(f"- Background work: {len(bg)} step(s), {ok} ok")
    return lines


def render_day(day, rows, distilled=None):
    out = [f"_Waggle's notes on {day:%A %Y-%m-%d}: {len([r for r in rows if not is_background(r)])} turn(s). "
           "Rewritten by Waggle; write your own notes outside this block._", ""]
    if distilled:
        if distilled.get("summary"):
            out += [f"**Summary:** {distilled['summary'].strip()}", ""]
        for c in distilled.get("conversations") or []:
            head = f"- **{c.get('time', '')}**" + (f" [[{c['project']}]]" if c.get("project") and
                                                     projects.find(c["project"]) else
                                                     (f" {c['project']}" if c.get("project") else ""))
            out.append(head + f" asked: {c.get('asked', '').strip()}")
            for k in ("found", "decided", "open"):
                if (c.get(k) or "").strip():
                    out.append(f"  - {k}: {c[k].strip()}")
        out += ["", "**Turns:**"]
    out += skeleton(day, rows) or ["- No conversations."]
    return "\n".join(out)


def write_day(day, rows, settings=None, distilled=None):
    from .ingest import set_block
    s = settings or Settings.load()
    rel = f"Journal/{day:%Y-%m-%d}"
    text = vault.read(rel, s) or f"# {day:%Y-%m-%d}\n\n"
    new = set_block(text, "digest", render_day(day, rows, distilled), heading="Digest", before=None)
    if new.replace("\r\n", "\n") != text.replace("\r\n", "\n"):
        vault.write(rel, new, s)
        return True
    return False


# ---- distillation --------------------------------------------------------------------------------

def _payload(day, rows, s, limit=40000):
    from . import learn
    known = [p["name"] for p in projects.all_projects(s)]
    held = [{"id": f["id"], "kind": f["kind"], "key": f["key"], "text": f["text"][:160]}
            for f in learn.facts("active") if f["kind"] not in learn.EVENT_KINDS][-60:]
    turns = []
    for r in rows:
        steps = [{"tool": (st.get("tool") or "").split("__")[-1], "ok": st.get("ok"),
                  "result": (st.get("result") or "")[:240]} for st in r.get("steps", [])[:12]]
        turns.append({"id": r["id"], "time": dt.datetime.fromtimestamp(r["time"]).strftime("%H:%M"),
                      "background": is_background(r), "user": (r.get("request") or "")[:700],
                      "assistant": (r.get("reply") or "")[:900], "steps": steps, "ok": bool(r.get("ok"))})
    body = json.dumps({"date": f"{day:%A %Y-%m-%d}", "projects": known, "known_facts": held, "turns": turns},
                      ensure_ascii=False)
    return body[:limit]


def distill(day, rows, settings=None, asker=None, model=None):
    """One background call. Returns the model's JSON (raises budget.Deferred / ClaudeError)."""
    from . import budget, claude_cli
    s = settings or Settings.load()
    model = model or s.deep_model
    asker = asker or claude_cli.ask
    with budget.spend(model, purpose="digest"):
        res = asker(_payload(day, rows, s), model=model, system=DISTILL_SYSTEM, schema=DISTILL_SCHEMA,
                    purpose="digest", timeout=300)
    return res.get("json") or {}


def _norm(text):
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def quote_in(quote, texts, ratio=0.85):
    """Is the quote (fuzzily) part of one of the texts?"""
    q = _norm(quote)
    if len(q) < 6:
        return False
    for t in texts:
        n = _norm(t)
        if q in n:
            return True
        m = difflib.SequenceMatcher(None, n, q, autojunk=False).find_longest_match(0, len(n), 0, len(q))
        if m.size >= ratio * len(q):
            return True
    return False


def _tokens(text):
    """Distinctive tokens of an outcome: identifiers, numbers, long or capitalised words."""
    raw = re.findall(r"[A-Za-z0-9][\w.-]*[\w]", text or "")
    return {t.lower() for t in raw if re.search(r"\d|[-_./]", t) or len(t) >= 7 or t[:1].isupper() and len(t) >= 4}


def outcome_verified(outcome, rows):
    """The outcome's trace exists, worked, and its reply/tool results contain most of the outcome's specifics."""
    r = next((x for x in rows if x["id"] == outcome.get("trace_id")), None)
    if not r:
        return False, "no such turn that day"
    if not r.get("ok"):
        return False, "that turn failed"
    hay = " ".join([r.get("reply") or ""] + [st.get("result") or "" for st in r.get("steps", [])]).lower()
    toks = _tokens(outcome.get("text", ""))
    if not toks:
        return False, "nothing concrete to check"
    found = sum(1 for t in toks if t in hay)
    if found / len(toks) < 0.5:
        return False, f"only {found}/{len(toks)} specifics appear in that turn's output"
    return True, ""


def file_results(day, rows, data, settings=None):
    """Facts -> learn (user's words only); outcomes -> project notes (verified only). Returns counts."""
    from . import learn
    s = settings or Settings.load()
    out = {"facts_filed": 0, "facts_rejected": 0, "outcomes_filed": 0, "outcomes_rejected": 0}
    mine = [r for r in rows if not is_background(r) and learn.is_user_turn(r)]
    for f in data.get("facts") or []:
        turn = next((r for r in mine if quote_in(f.get("quote", ""), [r.get("request") or ""])), None)
        if not turn:
            events.log("learn", action="rejected", fact_kind=f.get("kind"), text=str(f.get("text", ""))[:200],
                       reason="not in the user's own words", source=f"digest {day}")
            out["facts_rejected"] += 1
            continue
        ok, _ = learn.file_fact(f, s, source=f"digest {day}", evidence=f.get("quote", ""), ref=day, turn=turn,
                                supersedes=f.get("supersedes") or [])
        out["facts_filed" if ok else "facts_rejected"] += 1
    for o in data.get("outcomes") or []:
        ok, why = outcome_verified(o, rows)
        name = (projects.find(o.get("project", "")) or [None])[0] if o.get("project") else None
        if ok and not name:
            ok, why = False, f"no project note for {o.get('project')!r}"
        if not ok:
            events.log("learn", action="rejected", fact_kind="outcome", text=str(o.get("text", ""))[:200], reason=why,
                       source=f"digest {day}", trace=o.get("trace_id"))
            out["outcomes_rejected"] += 1
            continue
        text = o["text"].strip()
        if o.get("type") == "decided":
            from . import graph
            graph.record_decision(name, text, s)
        cur = projects.sections(name, s).get("Log", "")
        if _norm(text) not in _norm(cur):
            projects.add_log(name, f"From chat ({o.get('type', 'found')}, turn {o['trace_id']}): {text}", s)
        events.log("learn", action="filed", fact_kind="outcome", text=text[:200], project=name,
                   source=f"digest {day}", trace=o.get("trace_id"))
        out["outcomes_filed"] += 1
    return out


# ---- the job -------------------------------------------------------------------------------------

@vault_git.recorded("digest", lambda r, a, k: f"journal digest: {r['written']} note(s) written, {len(r['distilled'])} day(s) distilled")
def run(settings=None, now=None, asker=None, max_calls=2):
    """Write today's and yesterday's notes; distil finished days (and today after 20:00) once each."""
    s = settings or Settings.load()
    now = now or time.time()
    today = _day(now)
    state = kstore.get("digest.days", {})
    out = {"written": 0, "distilled": [], "skipped": []}
    calls = 0
    for day in (today - dt.timedelta(days=2), today - dt.timedelta(days=1), today):
        rows = day_traces(day)
        key = day.isoformat()
        st = state.get(key, {})
        if not rows:
            continue
        inter = [r for r in rows if not is_background(r)]
        last_id = rows[-1]["id"]
        due = inter and ((day < today and st.get("distilled_last") != last_id) or
                         (day == today and dt.datetime.fromtimestamp(now).hour >= 20 and not st.get("distilled_last")))
        distilled = st.get("distilled")
        if due and calls < max_calls:
            calls += 1
            try:
                distilled = distill(day, rows, s, asker)
                st["distilled"] = distilled
                st["distilled_last"] = last_id
                st["filed"] = file_results(day, rows, distilled, s)
                out["distilled"].append({"day": key, **st["filed"]})
            except Exception as e:  # budget deferred, Claude down: the skeleton still gets written
                out["skipped"].append(f"{key}: {type(e).__name__}: {str(e)[:120]}")
                events.log("learn", action="digest_skipped", day=key, reason=str(e)[:200])
        if st.get("written_last") != last_id or due:
            if write_day(day, rows, s, distilled):
                out["written"] += 1
            st["written_last"] = last_id
        st["turns"] = len(inter)
        state[key] = st
    for k in sorted(state)[:-10]:
        state.pop(k)
    kstore.put("digest.days", state)
    return out


def yesterday_block(settings=None, now=None, max_chars=700):
    """A short "Yesterday" block for the start of a new day ('' when there is nothing)."""
    day = _day(now or time.time()) - dt.timedelta(days=1)
    st = kstore.get("digest.days", {}).get(day.isoformat(), {})
    d = st.get("distilled") or {}
    lines = []
    if d.get("summary"):
        lines.append(d["summary"].strip())
        opens = [c["open"].strip() for c in d.get("conversations") or [] if (c.get("open") or "").strip()]
        if opens:
            lines.append("Left open: " + "; ".join(opens[:3]))
    elif st.get("turns"):
        rows = [r for r in day_traces(day) if not is_background(r)]
        asks = [re.sub(r"\s+", " ", r.get("request") or "")[:70] for r in rows[-4:]]
        if asks:
            lines.append(f"{len(rows)} turn(s); last asked: " + " | ".join(asks))
    if not lines:
        return ""
    text = f"Yesterday ({day:%a %Y-%m-%d}): " + " ".join(lines)
    return text[:max_chars]
