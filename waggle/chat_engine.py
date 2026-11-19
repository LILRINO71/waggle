"""Waggle's turn engine: one user message in, one reply out, with nothing lost and everything recorded.

It knows nothing about Hermes or Telegram. A transport hands it a message and a `Delivery` (waggle/delivery.py)
and the engine does the rest, in whatever process it runs:

    rec = chat_engine.run_turn("telegram:123", "what's on my calendar?", delivery, platform="telegram")

1. Context: the user's notes for this message (vault memory, projects, graph, past references), open approvals,
   unseen notifications, and a style hint for voice turns. The conversation itself lives in the chat's Claude
   session (waggle/chat_session.py), so it is not pasted in again.
2. Claude answers in the chat's long-lived process, streaming into the delivery (one edit per ~1.2 s, final
   text always sent). It can use tools and hand heavy work to background_task (waggle/chat_jobs.py).
3. Claude unavailable (quota, CLI missing, sign-in, outage, crash): the local model answers and the reply says
   so. Neither available: an honest message.
4. Delivery failed: the reply goes to the outbox (chat_store) and is retried until delivered, never dropped.
5. The truth record (traces.db): user text, reply, who really answered and why, tools and outcomes, time to
   first words, total time, delivery result.
"""
import re
import threading
import time
from dataclasses import dataclass, field

from . import chat_store, claude_cli, events, quota, traces
from .chat_session import VOICE_STYLE, SessionPool, ChatSession
from .config import Settings
from .delivery import StreamWriter, plain_text

LOCAL_LABEL = "(Answered by Waggle's local model: Claude is unavailable right now - {why}.)"
NOBODY = ("I can't answer right now: Claude is unavailable ({why}) and the local model didn't respond either. "
          "Your message is saved; try again in a few minutes.")

_pool = None
_pool_lock = threading.Lock()


def pool(settings=None):
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = SessionPool(factory=lambda chat: ChatSession(chat, settings=settings,
                                                                 transcript=traces.chat_transcript),
                                settings=settings)
        return _pool


def set_pool(p):
    """Tests and other hosts plug in their own pool (e.g. fake Claude processes)."""
    global _pool
    _pool = p


@dataclass
class TurnRecord:
    chat: str
    text: str
    reply: str = ""
    answerer: str = "none"  # claude | local-fallback | none
    model: str = ""
    fallback_reason: str = ""
    outcome: str = "failed"  # answered | partial | failed
    first_text_s: float = None
    total_s: float = 0.0
    delivered: bool = False
    delivery: str = ""
    delivery_error: str = ""
    outbox_id: str = ""
    steps: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    trace_id: int = None
    session: dict = field(default_factory=dict)


# ---- context ------------------------------------------------------------------------------------------------

def build_context(chat, text, voice=False, settings=None):
    """The per-turn <waggle_context> block. Every source is optional: a failing one is skipped, never fatal."""
    parts = []

    def add(name, fn):
        try:
            out = fn()
            if out and out.strip():
                parts.append(out.strip())
        except Exception as e:
            events.log("context_error", source=name, error=str(e)[:200])

    from . import memory, projects
    add("memory", lambda: memory.context_block(text or "")[:4000])
    add("projects", lambda: projects.context_for(text or "")[:2000])
    add("graph", lambda: __import__("waggle.graph", fromlist=["x"]).context_for(text))
    add("recall", lambda: traces.context_for(text))
    add("approvals", lambda: approvals_now(chat))
    add("news", unseen_news)
    add("deliveries", lambda: recent_deliveries(chat))
    if voice:
        from .voice_turn import VOICE_HINT
        parts.append(VOICE_HINT + "\n" + VOICE_STYLE)
    parts.append(f"Now: {time.strftime('%A %Y-%m-%d %H:%M')} (user's local time).")
    return "<waggle_context>\n" + "\n\n".join(parts) + "\n</waggle_context>"


def recent_deliveries(chat, within_s=86400):
    """Background results posted into this chat since the user's previous message, so the brain knows them."""
    row = chat_store.get_chat(chat) or {}
    since = max(row.get("prev_user") or 0, time.time() - within_s)
    items = [i for i in chat_store.delivered_since(chat, since) if i["kind"] != "reply"]
    if not items:
        return ""
    return "Posted into this chat since the user's last message (background results):\n" + "\n".join(
        f"- {i['text'][:1500]}" for i in items[-4:])


def unseen_news():
    from . import notify
    news = notify.unseen()
    if not news:
        return ""
    return "Tell the user these updates first, briefly:\n" + "\n".join(f"- {n['text']}" for n in news)


def approvals_now(chat):
    from . import policy
    open_ = policy.pending(chat)
    if not open_:
        return ""
    return "Approvals still open for this chat: " + "; ".join(f"{a['code']} ({a['reason']})" for a in open_)


# ---- approvals ----------------------------------------------------------------------------------------------

APPROVAL_CMD = re.compile(r"^\s*(approve|allow|ok|yes|deny|reject|refuse|no)\s+#?([A-Za-z0-9]{4})\s*[.!]*\s*$", re.I)
YES = re.compile(r"^\s*(y|yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|approved?|confirm(ed)?|allow( it)?)"
                 r"([\s,!.]+(do it|go ahead|please|you can|approved?))*[\s!.]*$", re.I)


def approval_preflight(chat, text, prev_msg_at=0.0):
    """Approval answers before Claude sees the message.
    Returns ("reply", text) to answer directly, ("note", text) to tell Claude what was approved, or None."""
    from . import objectives, policy
    m = APPROVAL_CMD.match(text or "")
    if m and (m.group(1).lower() not in ("yes", "ok", "no") or policy.get(m.group(2))):
        verb, code = m.group(1).lower(), m.group(2)
        item = policy.get(code)
        if not item:
            return "reply", f"I don't have an approval with code {code.upper()}. Nothing was done."
        if item["state"] != "pending" or item["expires"] <= time.time():
            state = "expired" if item["state"] == "pending" else item["state"]
            return "reply", f"Approval {item['code']} is no longer open ({state}). Nothing was done."
        sess = item.get("session") or ""
        if verb in ("deny", "reject", "refuse", "no"):
            policy.deny(item["id"])
            if sess.startswith("objective-"):
                objectives.settle_approvals()
            if sess == chat:
                return "note", f"The user denied: {item['summary']}. Don't do it; finish without it."
            return "reply", f"Denied {item['code']}: {item['summary']}. It won't run."
        granted = policy.grant(item["id"])
        if not granted:
            return "reply", f"Approval {item['code']} could not be granted (it changed meanwhile). Nothing was done."
        if sess.startswith("objective-"):
            oid = objectives.resume_after_approval(sess, granted)
            if oid:
                return "reply", f"Approved once: {item['summary']}. Continuing objective '{(objectives.status(oid) or {}).get('title', oid)}'."
            left = policy.pending(sess)
            return "reply", (f"Approved once: {item['summary']}. That objective still waits on: "
                             + "; ".join(policy.describe(p) for p in left[:3]))
        if sess == chat:
            return "note", f"The user approved: {item['summary']}. Do exactly that now (it is allowed once) and finish."
        return "reply", f"Approved once: {item['summary']}. It will be allowed the next time exactly that action runs."
    if not YES.match(text or ""):
        return None
    mine = policy.pending(chat)
    if mine:
        granted = policy.grant(session=chat)
        return "note", ("The user approved: " + "; ".join(g["summary"] for g in granted)
                        + ". Do those now (each is allowed once) and finish.")
    bg = [p for p in policy.pending() if str(p.get("session", "")).startswith(("objective-", "routine-"))]
    if any(p["created"] > prev_msg_at for p in bg):
        lines = "\n".join("- " + policy.describe(p) for p in bg[:6])
        one = bg[0]["code"]
        return "reply", (f"To be safe I don't approve on a plain yes while background work is waiting. Open "
                         f"requests:\n{lines}\nReply \"approve {one}\" (or \"deny {one}\") for the one you mean.")
    return None


# ---- the turn -----------------------------------------------------------------------------------------------

def claude_available(settings):
    """(ok, reason). Checked before using Claude so a known outage goes straight to the fallback."""
    if not claude_cli.find_claude():
        return False, "missing: Claude Code CLI not found"
    model = settings.chat_model or settings.router_model
    ok, why = quota.can_escalate(model, settings)
    return (True, "") if ok else (False, f"limit: {why}")


def local_answer(chat, text, context, settings):
    """The local model, with the recent transcript so it can follow the conversation. Raises on failure."""
    from . import ollama
    convo = traces.chat_transcript(chat, n=4, max_chars=4000)
    system = ("You are Waggle, a calm, helpful personal assistant. Claude (the main brain) is unavailable, so you "
              "answer this one message yourself: be brief and honest about what you don't know.\n\n" + context
              + (f"\n\n<recent_conversation>\n{convo}\n</recent_conversation>" if convo else ""))
    return ollama.chat([{"role": "system", "content": system}, {"role": "user", "content": text}],
                       settings=settings, timeout=120).strip()


def run_turn(chat, text, delivery, *, platform="", voice=False, t0=None, note="", context=None, settings=None,
             session=None, local=None, stream=True, record=True, address=None):
    """Answer one user message into `delivery`. Never raises; returns a TurnRecord (also written to traces)."""
    s = settings or Settings.load()
    t0 = t0 or time.time()
    rec = TurnRecord(chat=chat, text=text)
    try:
        chat_store.touch_chat(chat, platform, address=address, user=True)
    except Exception as e:
        events.log("engine_error", stage="touch_chat", error=str(e)[:200])
    writer = StreamWriter(delivery, interval=s.edit_interval_s, stream=stream)
    reply, why = "", ""
    try:
        ctx = context if context is not None else build_context(chat, text, voice, s)
    except Exception as e:
        ctx, why = "", ""
        events.log("engine_error", stage="context", error=str(e)[:200])
    message = ctx + ("\n\n<waggle_note>" + note + "</waggle_note>" if note else "") + "\n\n" + text
    ok, why = claude_available(s)
    res = None
    if ok:
        sess = session or pool(s).get(chat)
        res = sess.turn(message, on_text=writer.update)
        rec.steps, rec.usage, rec.model = res.steps, res.usage, res.model
        rec.session = {"restarted": res.restarted, "resumed": res.resumed, "retried": res.retried,
                       "fresh": res.fresh_session, "claude_session": res.session_id, "kind": res.kind}
        try:
            quota.record(res.model or s.chat_model, res.usage, "chat", headless=True, ok=res.ok,
                         error_kind=res.kind or None)
            if res.kind == "limit":
                quota.note_limit_hit(reset_in_hours=claude_cli.parse_reset_hours(res.error))
        except Exception:
            pass
        if res.ok and res.text.strip():
            reply, rec.answerer, rec.outcome = res.text, "claude", "answered"
        elif res.text.strip():  # it said something, then failed: keep what was said, say it was cut off
            reply, rec.answerer, rec.outcome = res.text + f"\n\n(I was cut off: {res.kind or 'error'}.)", "claude", "partial"
            rec.fallback_reason = f"{res.kind}: {res.error}"
        else:
            why = f"{res.kind or 'error'}: {res.error or 'empty answer'}"
    if not reply:
        rec.fallback_reason = why
        try:
            answer = (local or local_answer)(chat, text, ctx, s)
        except Exception as e:
            answer = ""
            rec.fallback_reason += f" | local model failed: {type(e).__name__}: {e}"[:200]
        short = _why_short(why)
        if answer:
            reply, rec.answerer, rec.outcome, rec.model = (answer + "\n\n" + LOCAL_LABEL.format(why=short),
                                                           "local-fallback", "answered", s.local_model)
        else:
            reply, rec.answerer, rec.outcome = NOBODY.format(why=short), "none", "failed"
    rec.reply = reply
    out = writer.finish(reply)
    rec.delivered = bool(out["delivered"])
    rec.delivery = getattr(delivery, "name", "delivery") if rec.delivered else "outbox"
    rec.delivery_error = out["error"]
    if out["first_shown_at"]:
        rec.first_text_s = round(out["first_shown_at"] - t0, 2)
    if not rec.delivered:
        rec.outbox_id = chat_store.enqueue(chat, plain_text(reply), kind="reply", source="turn")
    rec.total_s = round(time.time() - t0, 2)
    if record:
        rec.trace_id = record_turn(rec, platform=platform, voice=voice, note=note)
    return rec


def _why_short(why):
    kind, _, msg = (why or "unknown").partition(":")
    return {"limit": "the Claude usage limit is reached", "missing": "the Claude CLI isn't installed or found",
            "auth": "the Claude sign-in expired", "timeout": "Claude took too long", "crash": "Claude's process failed",
            }.get(kind.strip(), (msg or kind).strip()[:120] or "an error")


def record_turn(rec, platform="", voice=False, note="", route="chat"):
    try:
        return traces.record(rec.text, session=rec.chat, platform=platform, route=route, model=rec.model,
                             latency_s=rec.total_s, ok=rec.outcome == "answered" and rec.delivered,
                             error=rec.delivery_error or ("" if rec.answerer == "claude" else rec.fallback_reason),
                             reply=rec.reply, tokens=rec.usage, steps=rec.steps, answerer=rec.answerer,
                             fallback_reason=rec.fallback_reason or None, first_text_s=rec.first_text_s,
                             total_s=rec.total_s, delivered=rec.delivered, delivery=rec.delivery, outcome=rec.outcome,
                             chat=rec.chat, extra={"voice": voice, "note": bool(note), "outbox": rec.outbox_id or None,
                                                   "session": rec.session,
                                                   "tools": [st.get("tool") for st in rec.steps][:30]})
    except Exception as e:
        events.log("engine_error", stage="trace", error=str(e)[:200])
        return None


def record_direct(chat, text, reply, *, platform="", t0=None, route="direct", delivered=True, delivery="block"):
    """A reply Waggle gave without a model (approval answers, feedback acks): still one transcript row."""
    t0 = t0 or time.time()
    rec = TurnRecord(chat=chat, text=text, reply=reply, answerer="waggle", outcome="answered",
                     delivered=delivered, delivery=delivery, total_s=round(time.time() - t0, 2),
                     first_text_s=round(time.time() - t0, 2))
    rec.trace_id = record_turn(rec, platform=platform, route=route)
    return rec


# ---- outbox delivery ----------------------------------------------------------------------------------------

def deliver_pending(chat, delivery, items=None):
    """Send what is waiting for this chat through `delivery`. Returns how many were delivered."""
    n = 0
    for item in items if items is not None else chat_store.pending(chat):
        if not chat_store.claim(item["id"]):
            continue
        try:
            ok = _deliver_item(item, delivery)
        except Exception as e:
            ok = False
            chat_store.mark(item["id"], False, error=f"{type(e).__name__}: {e}")
        if ok:
            chat_store.mark(item["id"], True, via=getattr(delivery, "name", "delivery"))
            n += 1
        else:
            chat_store.mark(item["id"], False, error="transport refused")
    return n


def _deliver_item(item, delivery):
    ok = True
    if item.get("text"):
        h = delivery.post(plain_text(item["text"]))
        ok = h is not None and h is not False
    if ok and item.get("file"):
        ok = bool(delivery.send_file(item["file"]))
    return ok


def deliver_by_phone(item):
    """Last resort when no chat transport can reach the user: notify.send (queued for the next message, toast,
    phone). Returns True when it went out."""
    from . import notify
    notify.send(item.get("text") or "(result ready)")  # queued in notifications.jsonl + phone unless quiet hours
    return True
