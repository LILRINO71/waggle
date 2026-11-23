"""Expected-value outreach: send an unprompted message only when it is likely to be welcome.

After Horvitz (CHI 1999: act when the probability of being wanted, times the benefit, beats the cost of
interrupting) and BusyBody (CSCW 2004: learn that cost from the user's own behaviour). Models that decide what to
say are right about half the time (ProactiveBench), so the filter learns from what the owner actually did.

Outcome of a sent message (label_outcomes, run by initiative.track_responses every look pass):
  welcome    the owner wrote to Waggle within window_s (6 h) after it                 -> Beta a += 1
  unwelcome  ...and a short message says stop / not now / too many messages          -> Beta b += 3
  ignored    window_s passed with no message from the owner                          -> Beta b += 0.5
Only unprompted, non-urgent messages are labelled (what the owner asked for, and urgent items, say nothing about
suggestions). Labels older than 30 days count half as much every 30 days.

Per signal kind (github_ci, mail, routine, followup ...) there is a Beta(1, 1) prior over "welcome". allow() sends a
decided text or voice message only when

        p_welcome * value(kind) - cost  >  outreach_ev_threshold          cost = 0.25 * (1 + sent today / daily cap)

otherwise guard() turns it into "evening" (kept for tonight's note, with the reason "low welcome odds") and the
evening note says "Held back N suggestions (kinds you usually ignore)". A kind with fewer than MIN_LABELS labelled
outcomes is always sent: it has to be tried before it can be judged. Urgent items and things the owner asked for
never come here. config.json "outreach_ev_enabled": false switches the hold-back off (labelling goes on, so the
odds are ready when it is switched on again).

    python -m waggle.initiative stats        the per-kind odds
"""
import re
import time

PRIOR_A, PRIOR_B = 1.0, 1.0
IGNORED_WEIGHT = 0.5
UNWELCOME_WEIGHT = 3.0
HALF_LIFE_DAYS = 30.0
WINDOW_S = 6 * 3600
# Unprompted messages whose reception is labelled besides the initiative decisions (outbox origins).
FEEDBACK_ORIGINS = ("mailwatch", "brief", "evening", "background", "core", "proposal", "tidy", "autonomy",
                    "objective", "routine", "recipe", "scout")
MIN_LABELS = 3
BASE_COST = 0.25
DEFAULT_VALUE = 0.5
# What a message of this kind is worth when it is welcome (1 = the best an unprompted message can be).
KIND_VALUE = {"followup": 1.0, "checkin": 1.0, "github_ci": 0.8, "build": 0.8, "github_pr": 0.7, "mail": 0.7,
              "upcoming": 0.7, "objective": 0.6, "automation": 0.6, "github_issue": 0.5, "unpushed": 0.5,
              "cron": 0.5, "routine": 0.4, "recipe": 0.6}
NOTE = "low welcome odds"   # the guard note, counted by held_today()
STOP = re.compile(r"(?:\bnot now\b|\bleave me alone\b|\bshut up\b|\btoo many (?:messages|texts|notifications|"
                  r"suggestions)\b|\bno more (?:messages|texts|suggestions)\b|\bdon'?t (?:text|message|ping|bother) "
                  r"me\b|\bquit (?:texting|messaging|pinging)\b)", re.I)
MAX_STOP_CHARS = 120
# A bare "stop" is a command only when the message is little else ("stop", "ok stop", "please stop it", "stop for
# today", "stop texting me"). "stop by the shop", "bus stop", "non-stop", "can't stop thinking about it" are not.
_STOP_LEAD = {"ok", "okay", "k", "please", "pls", "plz", "just", "hey", "waggle", "now", "alright", "can", "could",
              "would", "will", "you", "i", "said", "ugh", "no", "yes"}
_STOP_TAIL = {"it", "that", "this", "now", "please", "pls", "plz", "already", "everything", "all", "thanks", "thx",
              "waggle", "for", "today", "tonight", "the", "day", "night", "a", "while", "bit", "me", "these", "those",
              "them", "texting", "messaging", "pinging", "sending", "bothering", "notifying", "spamming", "messages",
              "texts", "notifications", "suggestions", "reminders", "alerts", "stop"}


def _db():
    from . import initiative
    con = initiative._db()
    con.execute("""CREATE TABLE IF NOT EXISTS outcomes (outbox_id INTEGER PRIMARY KEY, kind TEXT, label TEXT,
        sent_at REAL, labelled_at REAL)""")
    return con


def is_stop(text):
    """A short message that tells Waggle to hold off. A long message that happens to contain "stop" does not."""
    t = " ".join(str(text or "").split())
    if not t or len(t) > MAX_STOP_CHARS:
        return False
    return bool(STOP.search(t)) or _bare_stop(t)


def _bare_stop(t):
    """"stop" as the command of a short message: only lead words before it, only tail words after it."""
    words = re.findall(r"[a-z']+", t.lower().replace("-", " x "))  # "non-stop" -> "non x stop": the x is no lead word
    if "stop" not in words:
        return False
    i = words.index("stop")
    return all(w in _STOP_LEAD for w in words[:i]) and all(w in _STOP_TAIL for w in words[i + 1:])


def _turns(settings, now, turns):
    """[(time, text)] of the owner's own messages. turns (tests): (time, platform) or (time, platform, text)."""
    if turns is None:
        try:
            from . import origin, traces
            turns = [(r["time"], r.get("platform") or "", r.get("request") or "") for r in
                     origin.user_turns(traces.all_traces(since=now - 3 * 86400, limit=500), settings)]
        except Exception:  # noqa: BLE001 - no traces yet
            turns = []
    out = []
    for t in turns:
        platform, text = (t[1], t[2] if len(t) > 2 else "")
        if platform in settings.followup_platforms:
            out.append((t[0], text))
    return sorted(out, key=lambda x: x[0])


def label_outcomes(settings=None, now=None, turns=None, window_s=WINDOW_S):
    """Label the sent, unprompted messages whose answer window is over or already answered. Returns how many."""
    from . import notify
    from .config import Settings
    s = settings or Settings.load()
    now = now or time.time()
    con = _db()
    done = {r[0] for r in con.execute("SELECT outbox_id FROM outcomes")}
    cand = [dict(r) for r in con.execute(
        "SELECT outbox_id, kind, urgent FROM decisions WHERE action IN ('text','voice') AND urgent=0 AND time>=? "
        "AND outbox_id IS NOT NULL", (now - 4 * 86400,))]
    cand = [c for c in cand if str(c["outbox_id"]).isdigit() and int(c["outbox_id"]) not in done]
    # Every other unprompted message too (mail alerts, brief, evening note, background results ...), kind = its
    # origin, so feedback is collected on all of them from day one (architecture decision 2026-11-21, step 0).
    seen = {int(c["outbox_id"]) for c in cand}
    for r in notify.rows(since=now - 4 * 86400, status="sent", origin=FEEDBACK_ORIGINS):
        if r["id"] not in done and r["id"] not in seen:
            cand.append({"outbox_id": r["id"], "kind": r["origin"], "urgent": 0})
    if not cand:
        con.close()
        return 0
    user = _turns(s, now, turns)
    n = 0
    for c in cand:
        oid = int(c["outbox_id"])
        ob = notify.get(oid)
        if not ob or ob["status"] != "sent" or not ob.get("sent_at") or ob.get("asked") or ob.get("urgent"):
            continue
        sent_at = ob["sent_at"]
        after = [(t, x) for t, x in user if sent_at < t <= sent_at + window_s]
        if after:
            label = "unwelcome" if any(is_stop(x) for _, x in after) else "welcome"
        elif now >= sent_at + window_s:
            label = "ignored"
        else:
            continue
        con.execute("INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?)", (oid, c["kind"], label, sent_at, now))
        n += 1
    con.commit()
    con.close()
    return n


def teach(outbox_id, kind, label, sent_at=None, now=None):
    """Record the owner's direct answer to one suggestion (label: welcome, unwelcome, never) without waiting for the
    6 h window. It replaces a label the window may already have given. No outbox row (None): nothing to teach."""
    if outbox_id is None or label not in ("welcome", "unwelcome", "never"):
        return False
    now = now or time.time()
    con = _db()
    con.execute("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?)", (int(outbox_id), kind, label, sent_at or now, now))
    con.commit()
    con.close()
    return True


def odds(kind, now=None):
    """{"kind", "a", "b", "p", "n", "welcome", "ignored", "unwelcome"}: the Beta posterior over 'welcome' for a
    kind of signal, from its labelled outcomes (older ones weigh less)."""
    now = now or time.time()
    con = _db()
    rows = con.execute("SELECT label, sent_at FROM outcomes WHERE kind=? AND sent_at>=?",
                       (kind, now - 120 * 86400)).fetchall()
    con.close()
    a, b = PRIOR_A, PRIOR_B
    count = {"welcome": 0, "ignored": 0, "unwelcome": 0}
    for label, sent_at in rows:
        w = 0.5 ** (max(now - (sent_at or now), 0) / 86400 / HALF_LIFE_DAYS)
        count[label] = count.get(label, 0) + 1
        if label == "welcome":
            a += w
        elif label == "unwelcome":
            b += w * UNWELCOME_WEIGHT
        elif label == "never":  # the owner said "never" to a suggestion: twice as strong as a stop
            b += w * UNWELCOME_WEIGHT * 2
        else:
            b += w * IGNORED_WEIGHT
    return {"kind": kind, "a": round(a, 3), "b": round(b, 3), "p": round(a / (a + b), 3), "n": len(rows), **count}


def value_of(kind):
    return KIND_VALUE.get(kind, DEFAULT_VALUE)


def cost_of(settings, cap_used):
    """The cost of one more unprompted message: more when many already went out today."""
    cap = max(int(getattr(settings, "daily_text_cap", 8) or 8), 1)
    return BASE_COST * (1 + min(max(cap_used, 0) / cap, 1.0))


def allow(sig, settings, now, state):
    """(send, note): the expected-value check for one decided message. State carries cap_used (guard's counter)."""
    if not getattr(settings, "outreach_ev_enabled", True):
        return True, ""
    kind = sig.get("kind") or "?"
    o = odds(kind, now)
    if o["n"] < MIN_LABELS:
        return True, ""
    value, cost = value_of(kind), cost_of(settings, state.get("cap_used", 0))
    score = o["p"] * value - cost
    if score > float(getattr(settings, "outreach_ev_threshold", 0.0) or 0.0):
        return True, ""
    return False, (f"{NOTE}: {kind} welcomed {o['welcome']} of {o['n']} times (p {o['p']:.2f} x value {value:.1f} "
                   f"- cost {cost:.2f} = {score:.2f}): kept for the evening note")


# ---- reporting -------------------------------------------------------------------------------------------

def held_today(now=None):
    """How many decisions today were turned into 'evening' by the expected-value check."""
    import datetime as dt
    now = now or time.time()
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    con = _db()
    n = con.execute("SELECT COUNT(*) FROM decisions WHERE action='evening' AND time>=? AND guard LIKE ?",
                    (start, NOTE + "%")).fetchone()[0]
    con.close()
    return n


def evening_line(now=None, settings=None):
    """'Held back N suggestions (kinds you usually ignore).' for the evening note, '' when none were."""
    n = held_today(now)
    return f"Held back {n} suggestion{'s' * (n != 1)} (kinds you usually ignore)." if n else ""


def stats(now=None, settings=None):
    """Lines for `python -m waggle.initiative stats`: per-kind odds, what would be sent, today's hold-backs."""
    from .config import Settings
    s = settings or Settings.load()
    now = now or time.time()
    con = _db()
    kinds = sorted({r[0] for r in con.execute("SELECT kind FROM outcomes")} |
                   {r[0] for r in con.execute("SELECT kind FROM decisions WHERE action IN ('text','voice')")})
    con.close()
    lines = [f"expected-value outreach: enabled={getattr(s, 'outreach_ev_enabled', True)} "
             f"threshold={getattr(s, 'outreach_ev_threshold', 0.0)}  window {WINDOW_S // 3600} h  "
             f"prior Beta({PRIOR_A:g},{PRIOR_B:g})  ignored counts {IGNORED_WEIGHT:g}, stop/not now {UNWELCOME_WEIGHT:g}",
             f"{'kind':<14}{'welcome':>8}{'ignored':>8}{'unwelc.':>8}{'p':>7}{'value':>7}  next message"]
    labelled = ignored = 0
    for k in kinds:
        o = odds(k, now)
        labelled += o["n"]
        ignored += o["ignored"] + o["unwelcome"]
        send, why = allow({"kind": k}, s, now, {"cap_used": 0})
        lines.append(f"{k:<14}{o['welcome']:>8}{o['ignored']:>8}{o['unwelcome']:>8}{o['p']:>7.2f}{value_of(k):>7.1f}  "
                     f"{'sent' if send else 'held for the evening note'}" +
                     (" (too few outcomes: always tried)" if o["n"] < MIN_LABELS else ""))
    if not kinds:
        lines.append("(no outcomes yet: every kind is sent until it has been tried)")
    if labelled:
        lines.append(f"false-alarm rate (sent, then ignored or unwelcome): {ignored / labelled:.0%} of {labelled}")
    lines.append(evening_line(now) or "nothing held back today")
    return lines


def main(argv=None):
    import sys
    args = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if args[:1] in ([], ["stats"]):
        print("\n".join(stats()))
        return 0
    print("usage: python -m waggle.initiative stats")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
