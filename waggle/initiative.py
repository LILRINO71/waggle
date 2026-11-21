"""Initiative: Claude decides what is worth telling the user, and how; rules outside the model keep it in bounds.

Signals (watchers.py, followups.py, scout.py) are stored in initiative.db (this module's own database). Each look
around (core timer outreach.look) checks due(): every initiative_every_min while awake, and right away when a
signal is urgent by itself, decide() gives Claude (Sonnet, a
background call under budget.py) the new signals, the user's profile and current projects, the recent
conversation, what the user worked on today, the time, and everything Waggle already told them recently. For each
signal it picks: none (say nothing), text, voice (a short voice note), call, evening (keep it for the evening note)
or, for an automation candidate, automate; and writes the message in Waggle's voice.

Guard rails applied to whatever the model says (guard()):
  - the same topic is never raised twice within topic_repeat_hours (an urgent item may repeat after 2 h);
  - at most daily_text_cap unprompted texts/voice notes/calls a day; urgent items and things the user asked for
    bypass the cap; over the cap an item goes to the evening note;
  - quiet hours: nothing non-urgent goes out at night (the outbox holds it until morning); no calls at night;
  - a call only for urgent items or when asked; when calls are not provisioned an urgent push + voice note go out
    instead (and call.call_sender falls back to push + text when a queued call cannot ring);
  - at most one automation a day.
Every decision is logged (signal, action the model chose, final action, reason, guard note, outbox id); whether the
user answered is filled in later (track_responses). The context the model sees holds only the user's own turns
(origin.user_turns, decision 10); every message it causes is one core outbox row with origin initiative,
followup or scout. When Claude is unavailable, urgent facts still go out with
template text; the rest waits for the next decision or the evening note.

A cheap gate (waggle/gates.py) sits in front of the Claude call: when every pending signal is routine, a repeat of
a topic already raised, or the daily cap is used up, and the check is very sure (P >= 0.95), the call is skipped and
the signals go to the evening note. Urgent signals, check-ins and automation candidates never pass through it.
"""
import datetime as dt
import json
import re
import sqlite3
import time
import uuid

from . import notify
from .config import Settings, data_path

ACTIONS = ("none", "text", "voice", "call", "evening", "later", "automate")
MAX_DEFERRALS = 3      # a finding can be put off this many times; the next time it is told or dropped
URGENT_LATER_MAX_MIN = 30
SEND_ACTIONS = ("text", "voice", "call")

SYSTEM = """You are Waggle, a student's personal assistant, deciding what to tell them unprompted. They are busy
(school, FTC robotics, GitHub projects) and a phone buzz costs them focus, so every message has to earn it. Below
you get the current time, what they have been doing (commits today, their recent conversation with you, what you
already told them) and new findings: facts Waggle noticed. For EACH finding pick one action and say why in one
sentence:
  text     tell them now, a short message
  voice    say it out loud: only for something personal and timely
  call     urgent AND minutes away, where a text could be missed
  evening  not now, but worth a line in tonight's evening note
  later    look at it again at a time you choose (look_again_minutes): the right moment is not now, or it may
           settle by itself. It comes back to you with how often it was put off. When can_defer is false you must
           choose another action.
  none     drop it: noise, already known, already handled, or not for them
  automate (only kind=automation) set that automation up now
How urgent something is depends on what the user is doing right now, not on its kind:
  - Something broken (failing tests or CI, a failed build) on a project they worked on today or talked about
    recently is worth telling now: they are in it and can fix it while the context is fresh. The same failure on
    a project they have not touched for days can wait for the evening note.
  - A deadline, meeting, quiz or start time inside the next two hours is told now, with the time left. One later
    today or tomorrow is for the evening note or the morning brief unless they have not been told yet.
  - A person or a review waiting on them (a pull request, a question) is worth a text when they are active on that
    project today, otherwise the evening note. Not a reason to nag: do not repeat what they were already told.
  - A check-in on something they said they would do ("did you test the shooter?") is for asking how it went, when
    its time has come and nothing shows it is done. If they already did it (their own words or a commit), drop it
    and say so. If the moment is wrong (late, or they are busy), use later with a time. Never drop a check-in
    just because it feels early: choose later.
  - Routine news (release notes, digests, newsletters, status mail) waits for the evening note, or none when it
    does not concern their work. Marketing, promotions and sales never get through: none.
  - Respect quiet hours and the daily count shown: when many findings compete, tell the one that matters most
    now and let the rest wait.
Messages: 1-2 short sentences, plain and specific (the project, the time left, the one thing to do), friendly, no
greetings, no emoji, no markdown. For a check-in ask how it went. Return JSON only; reason is one sentence."""

SCHEMA = {"type": "object", "properties": {"decisions": {"type": "array", "items": {"type": "object", "properties": {
    "key": {"type": "string"}, "action": {"type": "string", "enum": list(ACTIONS)},
    "message": {"type": "string"}, "topic": {"type": "string"}, "reason": {"type": "string"},
    "look_again_minutes": {"type": "integer"}},
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
    con.execute("""CREATE TABLE IF NOT EXISTS deferrals (key TEXT PRIMARY KEY, count INTEGER, next_at REAL,
        ready INTEGER, reason TEXT, timer_id TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS followups (id TEXT PRIMARY KEY, kind TEXT, created REAL, trace INTEGER,
        said TEXT, said_at REAL, what TEXT, due REAL, status TEXT, closed REAL, outbox TEXT, reason TEXT, chat TEXT,
        origin TEXT, timer_id TEXT)""")
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


# ---- "look again at": deferrals -------------------------------------------------------------------------------

def deferral(key):
    """{count, next_at, ready, reason} of a finding that was put off (count 0 when it never was)."""
    con = _db()
    row = con.execute("SELECT * FROM deferrals WHERE key=?", (key,)).fetchone()
    con.close()
    return dict(row) if row else {"key": key, "count": 0, "next_at": None, "ready": 0, "reason": "", "timer_id": None}


def defer(sig, minutes, reason, now):
    """Put a finding off: a one-shot core timer (catch-up "all", so a look-again that fell while the PC was off still
    happens) turns it back into a pending signal. Returns the timer id."""
    from waggle.core import timers
    n = deferral(sig["key"])["count"] + 1
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 60
    minutes = max(10, min(minutes, 24 * 60))
    if sig.get("urgent"):
        minutes = min(minutes, URGENT_LATER_MAX_MIN)
    tid = timers.once(now + minutes * 60, "outreach.recheck", {"key": sig["key"]}, timer_id=f"recheck:{sig['key']}:{n}",
                      catch_up="all", origin="outreach")
    con = _db()
    con.execute("INSERT OR REPLACE INTO deferrals VALUES (?,?,?,?,?,?)",
                (sig["key"], n, now + minutes * 60, 0, str(reason or "")[:300], tid))
    con.commit()
    con.close()
    return tid


def recheck(key, now=None):
    """The look-again time has come: a deferred signal is pending again (and due() will decide it at once)."""
    con = _db()
    cur = con.execute("UPDATE signals SET status='pending' WHERE key=? AND status='deferred'", (key,))
    if cur.rowcount:
        con.execute("UPDATE deferrals SET ready=1 WHERE key=?", (key,))
    con.commit()
    con.close()
    return bool(cur.rowcount)


def _settle(keys):
    con = _db()
    for k in keys:
        con.execute("UPDATE deferrals SET ready=0 WHERE key=?", (k,))
    con.commit()
    con.close()


def _ready_keys():
    con = _db()
    out = {r[0] for r in con.execute("SELECT key FROM deferrals WHERE ready=1")}
    con.close()
    return out


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
        from . import origin, traces
        turns = origin.user_turns(traces.all_traces(since=now - 2 * 86400, limit=60), settings)[:10]
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
    payload = []
    for x in sigs:
        item = {"key": x["key"], "kind": x["kind"], "title": x["title"], "detail": x.get("detail", ""),
                "urgent": bool(x["urgent"]), "topic": x["topic"]}
        d = deferral(x["key"])
        if d["count"]:
            item.update({"deferred_before": d["count"], "why_deferred": d["reason"]})
        item["can_defer"] = d["count"] < MAX_DEFERRALS
        payload.append(item)
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


LAST_DECISION_META = "outreach.last_decision"
BLOCKED_META = "outreach.decision_blocked"   # {"until": ts, "why": text}: the model could not be asked
RETRY_AFTER_S = 30 * 60                       # a failed try (budget, quota, CLI) is retried after this, not after 2 h


def gate(settings, now):
    """Is a decision run due, and which gate says so: {"due": bool, "gate": text, "pending", "urgent"}. The text is
    what the look pass records in its job result, so a pass that decided nothing says why."""
    from waggle.core import store
    p = pending()
    n, urgent = len(p), sum(1 for x in p if x["urgent"])
    out = {"pending": n, "urgent": urgent}
    if not p:
        return {**out, "due": False, "gate": "no pending signals"}
    blocked = store.loads(store.get_meta(BLOCKED_META)) or {}
    if urgent:
        return {**out, "due": True, "gate": f"{urgent} urgent signal(s): decided at once"}
    if notify.in_quiet_hours(settings, now):
        return {**out, "due": False, "gate": f"quiet hours: {n} non-urgent signal(s) held until morning"}
    if blocked.get("until", 0) > now:
        return {**out, "due": False, "gate": f"model unavailable ({blocked.get('why', '')}); next try in "
                                              f"{int((blocked['until'] - now) / 60) + 1} min"}
    if any(x["kind"] == "followup" for x in p):
        return {**out, "due": True, "gate": "a check-in's time has come: decided at once"}
    if _ready_keys() & {x["key"] for x in p}:
        return {**out, "due": True, "gate": "a look-again time came: decided at once"}
    every = settings.initiative_every_min * 60
    since = now - float(store.get_meta(LAST_DECISION_META, 0) or 0)
    if since >= every:
        return {**out, "due": True, "gate": "cadence: initiative_every_min passed"}
    return {**out, "due": False, "gate": f"cadence: last decision {int(since / 60)} min ago, next in "
                                         f"{int((every - since) / 60) + 1} min"}


def due(settings, now):
    """Is a decision run due? Pending signals and (an urgent one, a check-in or a look-again whose time came, or
    initiative_every_min passed while awake and the model was not just found unavailable)."""
    return gate(settings, now)["due"]


def rules_say_now(sig, activity):
    """With the model unavailable: a failure (CI, build, tests) on a project the user committed to today is still
    worth telling now. Nothing else is guessed: the rest waits for the model, or goes to the evening note."""
    if sig["kind"] not in ("github_ci", "build") or not activity:
        return False
    title = (sig.get("title") or "").lower()
    return "fail" in title and any(title.startswith(repo.lower() + ":") for repo in activity)


def _protected(sig):
    """A signal that always gets the model: urgent ones, the owner's check-ins, automation candidates, and anything
    the owner asked for."""
    if sig.get("urgent") or sig.get("kind") in ("followup", "automation"):
        return True
    try:
        return bool(json.loads(sig.get("data") or "{}").get("asked"))
    except ValueError:
        return False


GATE_QUESTION = ("Is every one of these new signals routine news, a repeat of something the owner was already told, "
                 "or marketing, so that none of them needs a decision from a stronger model today? Answer wake=false "
                 "only when you are very sure.")


def gate_check(sigs, settings, now, ask=None):
    """The cheap check in front of the decision call, as a zero-argument function for gates.evaluate: first two
    certain facts (code), then, only when Ollama already has a model loaded, one local-model question."""
    from . import gates

    def check():
        topics = recent_topics(now, max(settings.topic_repeat_hours, 2))
        if all(_norm_topic(x.get("topic") or x["key"]) in topics for x in sigs):
            return {"wake": False, "p_needed": 0.02, "data": {"why": "every signal repeats a topic already raised"}}
        if sent_today(now, settings) >= settings.daily_text_cap:
            return {"wake": False, "p_needed": 0.03, "data": {"why": f"the daily cap of {settings.daily_text_cap} "
                                                                       "is used up: nothing can go out"}}
        if getattr(settings, "gates_local", True) and (ask is not None or gates.ollama_ready(settings)):
            told = [r["text"][:100] for r in notify.rows(since=now - 2 * 86400)
                    if r["status"] in ("sent", "queued", "sending")][-8:]
            facts = ("New signals:\n" + "\n".join(f"- [{x['kind']}] {x['title'][:120]}" for x in sigs[:12])
                     + ("\n\nAlready told in the last 2 days:\n" + "\n".join(f"- {t}" for t in told) if told else ""))
            return gates.local_check(GATE_QUESTION, facts, settings=settings, ask=ask)()
        return {"wake": True, "p_needed": 1.0}
    return check


def decide(settings=None, now=None, asker=None, activity=None, only_urgent=False, turns=None, info=None, gate=None):
    """One decision run over the pending signals. Returns the decision rows. info (a dict) is filled with what
    happened: asked, model ("ok" or why it was unavailable), held (signals left pending), notes (guard notes), and
    gate (what the pre-check said). gate: a zero-argument check replacing the default (gates.evaluate), or False."""
    from waggle.core import store
    s = settings or Settings.load()
    now = now or time.time()
    info = info if info is not None else {}
    sigs = pending()
    if only_urgent:
        sigs = [x for x in sigs if x["urgent"]]
    info.update({"asked": 0, "model": "not asked", "held": 0, "notes": []})
    if not sigs:
        return []
    sigs = sigs[:20]
    info["asked"] = len(sigs)
    if gate is not False and not any(_protected(x) for x in sigs):
        from . import gates
        gv = gates.evaluate("initiative.decide", gate or gate_check(sigs, s, now), settings=s, now=now,
                            quiet_hold=True, subject=f"{len(sigs)} signal(s)")
        info["gate"] = gv.as_dict()
        if gv.skip:  # nothing is lost: each signal goes to the evening note, with the gate's reason
            info["model"] = "not asked: " + gv.reason
            state = {"topics": recent_topics(now, max(s.topic_repeat_hours, 2)), "cap_used": sent_today(now, s),
                     "automations": _automations_today(now), "activity": activity, "turns": turns}
            rows = [_apply(x, {"action": "evening", "message": "", "reason": "gate: " + gv.reason}, s, now, state,
                           "gate") for x in sigs]
            return rows
        if gv.hold:
            info["model"] = "not asked: " + gv.reason
            info["held"] = len(sigs)
            return []
    by_model, decided_by = {}, s.initiative_model
    try:
        by_model = _ask_claude(sigs, s, now, asker, activity)
        info["model"] = "ok"
        store.set_meta(BLOCKED_META, store.dumps({}))
    except Exception as e:  # noqa: BLE001  (budget, quota, CLI missing, timeout)
        decided_by = f"rules (Claude unavailable: {str(e)[:80]})"
        by_model = None
        info["model"] = f"unavailable: {str(e)[:120]}"
        store.set_meta(BLOCKED_META, store.dumps({"until": now + RETRY_AFTER_S, "why": str(e)[:80]}))
    state = {"topics": recent_topics(now, max(s.topic_repeat_hours, 2)), "cap_used": sent_today(now, s),
             "automations": _automations_today(now), "activity": activity, "turns": turns}
    out = []
    for sig in sigs:
        if by_model is None:
            if sig["urgent"]:
                d = {"action": "text", "message": template(sig), "reason": "urgent fact; Claude unavailable"}
            elif rules_say_now(sig, activity):
                d = {"action": "text", "message": template(sig), "reason": "failure on a project worked on today; "
                                                                         "Claude unavailable"}
            elif now - sig["time"] > 20 * 3600:
                d = {"action": "evening", "message": "", "reason": "waited a day for a decision"}
            else:
                info["held"] += 1
                continue  # stays pending for the next run
        else:
            d = by_model.get(sig["key"]) or {"action": "evening", "message": "",
                                             "reason": "the model returned no decision for it"}
        row = _apply(sig, d, s, now, state, decided_by)
        out.append(row)
        if row["guard"]:
            info["notes"].append(f"{sig['key'][:40]}: {row['guard'][:100]}")
    return out


def _automations_today(now):
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0).timestamp()
    con = _db()
    n = con.execute("SELECT COUNT(*) FROM decisions WHERE action='automate' AND time>=?", (start,)).fetchone()[0]
    con.close()
    return n


def _apply(sig, d, s, now, state, decided_by):
    model_action = str(d.get("action", "none"))
    wanted, note0 = _settle_later(sig, model_action, s, now, state)
    action, note = guard(sig, wanted, s, now, state)
    if note0:
        note = (note0 + "; " + note) if note else note0
    if sig["kind"] == "followup" and action == "none" and not note0.startswith("closed:"):
        action, note = "evening", (note + "; " if note else "") + "a check-in is never dropped silently"
    msg = " ".join(str(d.get("message") or "").split())[:900].lstrip("🐝 ")  # the bee is added once, below
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
    elif action == "later":
        defer(sig, d.get("look_again_minutes"), d.get("reason", ""), now)
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
    status = {"none": "dropped", "evening": "evening", "later": "deferred"}.get(action, "decided")
    con = _db()
    con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, now, sig["key"], sig["kind"], topic, model_action, action, msg,
                 str(d.get("reason", ""))[:400], note[:300], decided_by, outbox_id, int(bool(sig["urgent"]))))
    con.execute("UPDATE signals SET status=?, decided_at=?, decision_id=? WHERE key=?", (status, now, did, sig["key"]))
    con.commit()
    con.close()
    if sig["kind"] == "followup":
        from . import followups
        closed = note0.startswith("closed:")
        followups.mark(sig["key"].split(":", 1)[1], "asked" if action in SEND_ACTIONS else
                       "evening" if action == "evening" else "deferred" if action == "later" else
                       "closed" if closed else "dropped", now, outbox_id,
                       note0[len("closed: "):] if closed else d.get("reason", ""))
    if action != "later":
        _settle([sig["key"]])
    from . import events
    events.log("decision", id=did, signal=sig["key"], signal_kind=sig["kind"], model_action=model_action, action=action,
               reason=str(d.get("reason", ""))[:200], guard=note[:160], by=decided_by[:60])
    return {"id": did, "signal": sig["key"], "kind": sig["kind"], "model_action": model_action, "action": action,
            "message": msg, "reason": d.get("reason", ""), "guard": note, "outbox_id": outbox_id}


def _settle_later(sig, model_action, s, now, state):
    """(the action to run through guard(), a note). Two rules sit between the model and the guard rails:
    a check-in on something the user said they would do is never dropped: with no record that it was done it
    becomes a look-again (closed, with the evidence, when the records show it was done); and a finding that was
    put off MAX_DEFERRALS times cannot be put off again: it is told (check-ins and urgent ones) or dropped."""
    is_followup = sig["kind"] == "followup"
    count = deferral(sig["key"])["count"]
    note = ""
    if is_followup and model_action in ("none", "later"):
        from . import followups
        item = followups.get(sig["key"].split(":", 1)[1])
        ev = followups.done_evidence(item, now, s, turns=state.get("turns"), activity=state.get("activity")) if item else ""
        if ev:
            return "none", f"closed: {ev}"
        if model_action == "none":
            model_action, note = "later", "check-in not dropped: nothing in the records shows it was done"
    if model_action == "later" and count >= MAX_DEFERRALS:
        if is_followup or sig.get("urgent"):
            return "text", f"deferred {count} times: told now" + (f" ({note})" if note else "")
        return "none", f"deferred {count} times: dropped, nothing new came up"
    return model_action, note


def evening_items(now, settings=None):
    """Signals kept for tonight's note (decided 'evening' today, or pending since this morning)."""
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0).timestamp()
    con = _db()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM signals WHERE (status IN ('evening','deferred') AND decided_at>=?) OR (status='pending' AND time>=?)",
        (start - 6 * 3600, start))]
    con.close()
    return rows


def keep_for_evening(key, kind, title, detail="", now=None, topic=None):
    """Put one finished piece of background work into tonight's evening note (instead of a message now). The title is
    the line the note shows (140 characters), the detail stays here. Returns the signal key."""
    now = now or time.time()
    record([{"key": key, "kind": kind, "title": " ".join(str(title).split())[:300], "detail": str(detail)[:4000],
             "topic": topic or key}], now)
    con = _db()
    con.execute("UPDATE signals SET status='evening', decided_at=? WHERE key=? AND status='pending'", (now, key))
    con.commit()
    con.close()
    return key


def close_evening(keys, now):
    con = _db()
    for k in keys:
        con.execute("UPDATE signals SET status='in_evening_note', decided_at=? WHERE key=?", (now, k))
    con.commit()
    con.close()
