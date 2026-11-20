"""Initiative: Claude decides what is worth telling the user, and how; rules outside the model keep it in bounds.

Signals (watchers.py, followups.py, scout.py) are stored in ~/.waggle/initiative.db. On a schedule during waking
hours (initiative_every_min), and right away when a signal is urgent by itself, decide() gives Claude (Sonnet, a
background call under budget.py) the new signals, the user's profile and current projects, the recent
conversation, what the user worked on today, the time, and everything Waggle already told them recently. For each
signal it picks: none (say nothing), text, voice (a short voice note), call, evening (keep it for the evening note)
or, for an automation candidate, automate; and writes the message in Waggle's voice.

Guard rails applied to whatever the model says (guard()):
  - the same topic is never raised twice within topic_repeat_hours (an urgent item may repeat after 2 h);
  - at most daily_text_cap unprompted texts/voice notes/calls a day; urgent items and things the user asked for
    bypass the cap; over the cap an item goes to the evening note;
  - quiet hours: nothing non-urgent goes out at night (the outbox holds it until morning); no calls at night;
  - a call only for urgent items or when asked; when calls are impossible notify falls back to push + voice note;
  - at most one automation a day.
Every decision is logged (signal, action the model chose, final action, reason, guard note, outbox id); whether the
user answered is filled in later from the outbox. When Claude is unavailable, urgent facts still go out with
template text; the rest waits for the next decision or the evening note. The local model is not used here.
"""
import datetime as dt
import json
import re
import sqlite3
import time
import uuid

from . import notify
from .config import Settings, data_path

ACTIONS = ("none", "text", "voice", "call", "evening", "automate")
SEND_ACTIONS = ("text", "voice", "call")

SYSTEM = """You are Waggle, the user's personal assistant, deciding what to tell them unprompted. They are a busy
student (school, FTC robotics, GitHub projects). You get new signals (facts Waggle noticed) plus context.
For EACH signal decide one action:
  none     not worth their attention, already handled in the conversation, or they clearly know
  text     worth a short message now
  voice    worth saying out loud (only for something personal and timely, e.g. a check-in or an event soon)
  call     only if it is urgent AND time-critical (minutes to hours) and a text could be missed
  evening  useful but not now: it goes into tonight's evening note
  automate (only for kind=automation) Waggle should set this automation up now
Judge by what matters to THIS user today: deadlines, broken builds on projects they are actively working on,
things they said they would do, people waiting on them. Don't repeat what they were already told. Silence is
fine. Messages: 1-2 short sentences, plain and specific, friendly, no greetings, no emoji, no markdown. For a
check-in on something they said they'd do, ask how it went. Return JSON only."""

SCHEMA = {"type": "object", "properties": {"decisions": {"type": "array", "items": {"type": "object", "properties": {
    "key": {"type": "string"}, "action": {"type": "string", "enum": list(ACTIONS)},
    "message": {"type": "string"}, "topic": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["key", "action", "message", "reason"]}}}, "required": ["decisions"]}


# ---- storage ---------------------------------------------------------------------------------------------

def _db():
    con = sqlite3.connect(data_path("initiative.db"), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS signals (key TEXT PRIMARY KEY, time REAL, kind TEXT, title TEXT,
        detail TEXT, topic TEXT, urgent INTEGER, url TEXT, project TEXT, data TEXT, status TEXT, decided_at REAL,
        decision_id TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, time REAL, signal_key TEXT, kind TEXT,
        topic TEXT, model_action TEXT, action TEXT, message TEXT, reason TEXT, guard TEXT, decided_by TEXT,
        outbox_id TEXT, urgent INTEGER)""")
    con.execute("CREATE TABLE IF NOT EXISTS responses (outbox_id INTEGER PRIMARY KEY, responded_at REAL)")
    return con


def record(signals, now=None):
    """Store new signals (a key already seen is ignored). Told ones are kept only for the evening note."""
    now = now or time.time()
    con = _db()
    n = 0
    for sg in signals:
        cur = con.execute("INSERT OR IGNORE INTO signals (key, time, kind, title, detail, topic, urgent, url, project, "
                          "data, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (sg["key"], now, sg["kind"], sg["title"], sg.get("detail", ""), sg.get("topic") or sg["key"],
                           int(bool(sg.get("urgent"))), sg.get("url", ""), sg.get("project", ""),
                           json.dumps({k: v for k, v in sg.items() if k not in ("title", "detail")}, default=str),
                           "told" if sg.get("told") else "pending"))
        n += cur.rowcount
    con.commit()
    con.close()
    return n


def pending():
    con = _db()
    out = [dict(r) for r in con.execute("SELECT * FROM signals WHERE status='pending' ORDER BY urgent DESC, time")]
    con.close()
    return out


def signals_between(start, end, status=None):
    con = _db()
    q, args = "SELECT * FROM signals WHERE time>=? AND time<?", [start, end]
    if status:
        q += f" AND status IN ({','.join('?' * len(status))})"
        args += list(status)
    out = [dict(r) for r in con.execute(q + " ORDER BY time", args)]
    con.close()
    return out


def decisions(since=0.0):
    """Every decision with whether the user answered (from the outbox), for evaluation."""
    con = _db()
    rows = [dict(r) for r in con.execute("SELECT * FROM decisions WHERE time>=? ORDER BY time", (since,))]
    answered = {r[0] for r in con.execute("SELECT outbox_id FROM responses")}
    con.close()
    for r in rows:
        oid = int(r["outbox_id"]) if str(r.get("outbox_id") or "").isdigit() else None
        ob = notify.get(oid) if oid is not None else None
        r["delivered"] = bool(ob and ob["status"] == "sent")
        r["responded"] = oid in answered
    return rows


def track_responses(settings=None, now=None, window_s=6 * 3600, turns=None):
    """Mark sent outreach the user answered (one of the user's own turns within window_s after it) and record
    that as confirmed evidence for the channel. turns = [(time, platform)] for tests; default: the user's own
    turns from traces.db (origin.user_turns: tests, benches, the CLI and agents never count)."""
    s = settings or Settings.load()
    now = now or time.time()
    sent = [r for r in notify.rows(since=now - 2 * 86400, status="sent") if r.get("sent_at")]
    con = _db()
    answered = {r[0] for r in con.execute("SELECT outbox_id FROM responses")}
    con.close()
    open_ = [r for r in sent if r["id"] not in answered]
    if not open_:
        return 0
    if turns is None:
        try:
            from . import origin, traces
            turns = [(r["time"], r.get("platform") or "") for r in
                     origin.user_turns(traces.all_traces(since=now - 3 * 86400, limit=500), s)]
        except Exception:  # noqa: BLE001
            turns = []
    user_times = sorted(t for t, p in turns if p in s.followup_platforms)
    marked = 0
    con = _db()
    for r in open_:
        hit = next((t for t in user_times if r["sent_at"] < t <= r["sent_at"] + window_s), None)
        if hit:
            con.execute("INSERT OR IGNORE INTO responses VALUES (?, ?)", (r["id"], hit))
            marked += 1
            if r["kind"] in ("text", "voice"):
                notify.record_evidence(notify.CHANNEL[r["kind"]], True, "the user replied after it",
                                       ref=f"outbox:{r['id']}", level="confirmed", now=hit)
    con.commit()
    con.close()
    return marked


# ---- guard rails -------------------------------------------------------------------------------------------

def _norm_topic(t):
    return re.sub(r"[^a-z0-9:#/._-]+", " ", (t or "").lower()).strip()


def recent_topics(now, hours):
    """{topic: last time it went out} for things that actually reached (or are queued for) the phone."""
    con = _db()
    rows = con.execute("SELECT topic, time FROM decisions WHERE action IN ('text','voice','call') AND time>=?",
                       (now - hours * 3600,)).fetchall()
    con.close()
    out = {}
    for t, ts in rows:
        out[_norm_topic(t)] = max(out.get(_norm_topic(t), 0), ts)
    for r in notify.rows(since=now - hours * 3600):
        if r["topic"] and r["status"] in ("sent", "queued", "sending"):
            out[_norm_topic(r["topic"])] = max(out.get(_norm_topic(r["topic"]), 0), r["created"])
    return out


def sent_today(now, settings):
    """Unprompted messages counted against the cap today (texts, voice notes, calls from initiative origins)."""
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return sum(1 for r in notify.rows(since=start, origin=notify.INITIATIVE_ORIGINS)
               if r["kind"] in ("text", "voice", "call") and not r["urgent"] and not r["asked"])


def guard(sig, action, settings, now, state):
    """(final action, note). state carries counters within one decision run: cap_used, topics, automations."""
    s = settings
    urgent = bool(sig.get("urgent"))
    asked = bool(sig.get("asked"))
    if action not in ACTIONS:
        return "none", f"unknown action {action!r}"
    if action == "automate" and sig["kind"] != "automation":
        action = "text"
    if action == "automate":
        if not s.scout_enabled or state["automations"] >= 1:
            return "evening", "automation limit: at most one a day"
        return "automate", ""
    if action not in SEND_ACTIONS:
        return action, ""
    topic = _norm_topic(sig.get("topic") or sig["key"])
    last = state["topics"].get(topic)
    if last is not None:
        window = 2 if urgent else s.topic_repeat_hours
        if now - last < window * 3600:
            return "none", f"topic already raised {int((now - last) / 3600)} h ago"
    if action == "call" and not (urgent or asked):
        action, note = "text", "call downgraded: not urgent"
    else:
        note = ""
    quiet = notify.in_quiet_hours(s, now)
    if quiet and not (urgent or asked):
        return "evening" if action != "call" else "evening", "quiet hours: kept for later"
    if action == "call" and quiet and not asked:
        action, note = "text", "no calls in quiet hours"
    if not (urgent or asked) and state["cap_used"] >= s.daily_text_cap:
        return "evening", f"daily cap of {s.daily_text_cap} reached"
    return action, note


# ---- context for the model -----------------------------------------------------------------------------------

def context(settings, now, activity=None):
    from . import projects, vault
    parts = [f"Now: {dt.datetime.fromtimestamp(now):%A %Y-%m-%d %H:%M}. Quiet hours {settings.quiet_hours[0]}:00-"
             f"{settings.quiet_hours[1]}:00."]
    try:
        prof = vault.read("Me/Profile", settings)
        if prof.strip():
            parts.append("Profile:\n" + prof[:1200])
    except Exception:  # noqa: BLE001
        pass
    try:
        lines = []
        for p in projects.all_projects(settings):
            if p.get("status") == "active":
                nxt = next((t["text"] for t in projects.tasks(p["name"], settings) if not t["done"]), "")
                lines.append(f"- {p['name']}" + (f" (next: {nxt[:80]})" if nxt else ""))
        if lines:
            parts.append("Active projects:\n" + "\n".join(lines[:10]))
    except Exception:  # noqa: BLE001
        pass
    if activity:
        parts.append("Commits today: " + "; ".join(f"{k}: {len(v)} ({v[0][:60]})" for k, v in activity.items()))
    try:
        from . import traces
        turns = [r for r in traces.all_traces(since=now - 2 * 86400, limit=60)
                 if (r.get("platform") or "") in settings.followup_platforms][:10]
        if turns:
            parts.append("Recent conversation (newest first):\n" + "\n".join(
                f"[{dt.datetime.fromtimestamp(r['time']):%a %H:%M}] user: {(r.get('request') or '')[:160]} | "
                f"waggle: {(r.get('reply') or '')[:120]}" for r in turns))
    except Exception:  # noqa: BLE001
        pass
    told = [r for r in notify.rows(since=now - 2 * 86400) if r["status"] in ("sent", "queued", "sending")]
    if told:
        parts.append("Already told them (last 2 days):\n" + "\n".join(
            f"[{dt.datetime.fromtimestamp(r['created']):%a %H:%M}] {r['text'][:140]}" for r in told[-15:]))
    return "\n\n".join(parts)


# ---- deciding ------------------------------------------------------------------------------------------------

def template(sig):
    if sig["kind"] == "followup":
        from . import followups
        return followups.template(sig)
    return f"🐝 {sig['title']}" + (f". {sig['detail']}" if sig.get("detail") else "")


def _ask_claude(sigs, settings, now, asker=None, activity=None):
    from . import budget, claude_cli
    payload = [{"key": x["key"], "kind": x["kind"], "title": x["title"], "detail": x.get("detail", ""),
                "urgent": bool(x["urgent"]), "topic": x["topic"]} for x in sigs]
    prompt = (context(settings, now, activity) + "\n\nNew signals:\n" + json.dumps(payload, indent=1) +
              f"\n\nUnprompted messages already sent today: {sent_today(now, settings)} of {settings.daily_text_cap}.")
    with budget.background("initiative"):
        res = (asker or claude_cli.ask)(prompt, model=settings.initiative_model, system=SYSTEM, schema=SCHEMA,
                                        effort="low", purpose="initiative", timeout=180)
    data = res.get("json") or {}
    if not data and res.get("text"):
        try:
            data = json.loads(res["text"])
        except ValueError:
            data = {}
    return {d.get("key"): d for d in data.get("decisions", []) if isinstance(d, dict)}


def due(settings, now, state):
    """Is a decision run due? Pending signals and (an urgent one, or the interval passed while awake)."""
    p = pending()
    if not p:
        return False
    if any(x["urgent"] for x in p):
        return True
    if notify.in_quiet_hours(settings, now):
        return False
    return now - state.get("last_decision", 0) >= settings.initiative_every_min * 60


def decide(settings=None, now=None, asker=None, activity=None, only_urgent=False):
    """One decision run over the pending signals. Returns the decision rows."""
    s = settings or Settings.load()
    now = now or time.time()
    sigs = pending()
    if only_urgent:
        sigs = [x for x in sigs if x["urgent"]]
    if not sigs:
        return []
    sigs = sigs[:20]
    by_model, decided_by = {}, s.initiative_model
    try:
        by_model = _ask_claude(sigs, s, now, asker, activity)
    except Exception as e:  # noqa: BLE001  (budget, quota, CLI missing, timeout)
        decided_by = f"rules (Claude unavailable: {str(e)[:80]})"
        by_model = None
    state = {"topics": recent_topics(now, max(s.topic_repeat_hours, 2)), "cap_used": sent_today(now, s),
             "automations": _automations_today(now)}
    out = []
    for sig in sigs:
        if by_model is None:
            if sig["urgent"]:
                d = {"action": "text", "message": template(sig), "reason": "urgent fact; Claude unavailable"}
            elif now - sig["time"] > 20 * 3600:
                d = {"action": "evening", "message": "", "reason": "waited a day for a decision"}
            else:
                continue  # stays pending for the next run
        else:
            d = by_model.get(sig["key"]) or {"action": "evening", "message": "",
                                             "reason": "the model returned no decision for it"}
        out.append(_apply(sig, d, s, now, state, decided_by))
    return out


def _automations_today(now):
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0).timestamp()
    con = _db()
    n = con.execute("SELECT COUNT(*) FROM decisions WHERE action='automate' AND time>=?", (start,)).fetchone()[0]
    con.close()
    return n


def _apply(sig, d, s, now, state, decided_by):
    model_action = str(d.get("action", "none"))
    action, note = guard(sig, model_action, s, now, state)
    msg = " ".join(str(d.get("message") or "").split())[:900]
    if action in SEND_ACTIONS and not msg:
        msg = template(sig).lstrip("🐝 ")
    did = uuid.uuid4().hex[:12]
    topic = sig.get("topic") or sig["key"]
    origin = "followup" if sig["kind"] == "followup" else "scout" if sig["kind"] == "automation" else "initiative"
    outbox_id = None
    kw = dict(urgent=bool(sig["urgent"]), asked=bool(sig.get("asked")), origin=origin, topic=topic,
              decision_id=did, settings=s, now=now)
    if action == "text":
        outbox_id = notify.deliver("text", f"🐝 {msg}", **kw)["id"]
    elif action == "voice":
        outbox_id = notify.voice(msg, **kw)["id"]
    elif action == "call":
        from . import call
        if call.configured():
            outbox_id = notify.deliver("call", msg, **kw)["id"]
        else:
            note = (note + "; " if note else "") + "calls not possible: urgent push + voice note instead"
            notify.deliver("push", msg, title="Waggle", **kw)
            outbox_id = notify.voice(msg, caption=f"📞 {msg}", **kw)["id"]
    elif action == "automate":
        from . import scout
        res = scout.build(sig, msg, s)
        note = (note + "; " if note else "") + res.get("note", "")
        if res.get("message"):
            outbox_id = notify.deliver("text", f"🐝 {res['message']}", **kw)["id"]
    if action in SEND_ACTIONS:
        state["topics"][_norm_topic(topic)] = now
        if not (sig["urgent"] or sig.get("asked")):
            state["cap_used"] += 1
    if action == "automate":
        state["automations"] += 1
    status = {"none": "dropped", "evening": "evening"}.get(action, "decided")
    con = _db()
    con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, now, sig["key"], sig["kind"], topic, model_action, action, msg,
                 str(d.get("reason", ""))[:400], note[:300], decided_by, outbox_id, int(bool(sig["urgent"]))))
    con.execute("UPDATE signals SET status=?, decided_at=?, decision_id=? WHERE key=?", (status, now, did, sig["key"]))
    con.commit()
    con.close()
    if sig["kind"] == "followup":
        from . import followups
        followups.mark(sig["key"].split(":", 1)[1], "asked" if action in SEND_ACTIONS else
                       "evening" if action == "evening" else "dropped", now, outbox_id, d.get("reason", ""))
    from . import events
    events.log("decision", id=did, signal=sig["key"], signal_kind=sig["kind"], model_action=model_action, action=action,
               reason=str(d.get("reason", ""))[:200], guard=note[:160], by=decided_by[:60])
    return {"id": did, "signal": sig["key"], "kind": sig["kind"], "model_action": model_action, "action": action,
            "message": msg, "reason": d.get("reason", ""), "guard": note, "outbox_id": outbox_id}


def evening_items(now, settings=None):
    """Signals kept for tonight's note (decided 'evening' today, or pending since this morning)."""
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0).timestamp()
    con = _db()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM signals WHERE (status='evening' AND decided_at>=?) OR (status='pending' AND time>=?)",
        (start - 6 * 3600, start))]
    con.close()
    return rows


def close_evening(keys, now):
    con = _db()
    for k in keys:
        con.execute("UPDATE signals SET status='in_evening_note', decided_at=? WHERE key=?", (now, k))
    con.commit()
    con.close()
