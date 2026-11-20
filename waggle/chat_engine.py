"""Waggle's turn engine: one user message in, one reply out, with nothing lost and everything recorded.

It knows nothing about Hermes or Telegram. A transport hands it a message and a `Delivery` (waggle/delivery.py)
and the engine does the rest, in whatever process it runs:

    rec = chat_engine.handle_inbound("telegram:123", "what's on my calendar?")       # the core process
    rec = chat_engine.run_turn(chat, text, delivery, platform="telegram", origin="user")  # any host

1. Context: the user's notes for this message (vault memory, projects, graph, past references), open approvals,
   unseen notifications, background results posted since the user's last message, and a style hint for voice
   turns. The conversation itself lives in the chat's Claude session (waggle/chat_session.py).
2. Claude answers in the chat's long-lived process, streaming into the delivery (one edit per ~1.2 s, final
   text always sent). It can use tools and hand heavy work to background_task (waggle/chat_jobs.py).
3. Claude unavailable or failing (quota, CLI missing, sign-in, outage, crash): an honest message says so. The
   small local model answers only when a caller explicitly passes `local=`, and then the reply says so.
4. Delivery failed: the reply goes to the core outbox (waggle.core.outbox, idem key "turn:<turn_id>") and the
   core's sender delivers it, never twice.
5. The truth record (traces.db, waggle/traces.py): user text, reply, who really answered (model id, local or
   none) and why, every tool call with its result, time to first words, total time, delivery result and
   message ids, what the answer claims to have produced, the code version and the message's origin; outcome
   ok / degraded / failed by traces.turn_outcome().

Origin (plan decision 10): only the owner's own channel is "user"; the command-line chat, tests, canaries,
benches and other agents are marked so nothing learns from them (origin_for()).
"""
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import chat_store, claude_cli, events, quota, traces
from .chat_session import VOICE_STYLE, ChatSession, SessionPool
from .config import Settings
from .delivery import StreamWriter, plain_text

ORIGINS = traces.ORIGINS
TEST_PLATFORMS = {"cli", "local", "test", "api", "desktop", ""}  # the command-line chat and test clients
AGENT_PLATFORMS = {"subagent", "delegation", "cron", "kanban", "agent"}

LOCAL_LABEL = "(Answered by Waggle's small local model because Claude is unavailable right now: {why}.)"
FAILED = "I couldn't answer that just now: {why}. Nothing was done with it; please send it again in a few minutes."
CUT_OFF = "\n\n(I was cut off before I finished: {why}.)"

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


def origin_for(platform, env=None):
    """The origin of a message by where it came from. WAGGLE_ORIGIN (a bench or test harness) wins; a process
    run as an agent (WAGGLE_AGENT) is "agent"; the command-line chat and test clients are "test"; a chat
    platform (Telegram) is "user"."""
    env = os.environ if env is None else env
    forced = (env.get("WAGGLE_ORIGIN") or "").strip().lower()
    if forced in ORIGINS:
        return forced
    if env.get("WAGGLE_AGENT"):
        return "agent"
    p = (platform or "").strip().lower()
    if p in ("bench", "canary"):
        return p
    if p in AGENT_PLATFORMS:
        return "agent"
    if p in TEST_PLATFORMS:
        return "test"
    return "user"


@dataclass
class TurnRecord:
    chat: str
    text: str
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    origin: str = "user"
    platform: str = ""
    reply: str = ""
    answered_by: str = "none"  # the Claude model id | local | none
    model: str = ""
    fallback_reason: str = ""
    outcome: str = "failed"  # ok | degraded | failed (traces.turn_outcome)
    outcome_why: list = field(default_factory=list)
    ttfw_s: float = None
    total_s: float = 0.0
    delivered: bool = False
    delivery: str = ""
    delivery_error: str = ""
    message_ids: list = field(default_factory=list)
    outbox_id: int = None
    steps: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    claims: dict = None
    partial: bool = False
    failure_notice: bool = False
    trace_id: int = None
    session: dict = field(default_factory=dict)

    def settle(self):
        """Outcome and claims from what happened (no model)."""
        self.outcome, self.outcome_why = traces.turn_outcome(
            answered_by=self.answered_by, delivered=self.delivered, queued=self.outbox_id is not None,
            steps=self.steps, partial=self.partial, failure_notice=self.failure_notice)
        self.claims = traces.extract_claims("" if self.failure_notice else self.reply, self.steps)
        return self


# ---- context ------------------------------------------------------------------------------------------------

def build_context(chat, text, voice=False, settings=None, since=None):
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
    add("deliveries", lambda: recent_deliveries(chat, since=since))
    if voice:
        from .voice_turn import VOICE_HINT
        parts.append(VOICE_HINT + "\n" + VOICE_STYLE)
    parts.append(f"Now: {time.strftime('%A %Y-%m-%d %H:%M')} (user's local time).")
    return "<waggle_context>\n" + "\n\n".join(parts) + "\n</waggle_context>"


def recent_deliveries(chat, since=None, within_s=86400, now=None):
    """Background results delivered into this chat since the user's previous message (from the core outbox),
    so the brain knows what the user has just seen."""
    from waggle.core import outbox
    now = time.time() if now is None else now
    since = max(since or 0.0, now - within_s)
    items = [r for r in outbox.list(state="sent", chat=chat, limit=30)
             if (r.get("sent_at") or 0) >= since and (r.get("meta") or {}).get("kind") == "background"]
    if not items:
        return ""
    items.sort(key=lambda r: r.get("sent_at") or 0)
    return "Posted into this chat since the user's last message (background results):\n" + "\n".join(
        f"- {i['body'][:1500]}" for i in items[-4:])


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

def chat_model(settings):
    name = settings.chat_model or settings.router_model
    return claude_cli.MODEL_IDS.get(name, name)


def claude_available(settings):
    """(ok, reason). A known quota block goes straight to the honest failure; a missing or broken CLI is found
    when the chat's process starts."""
    ok, why = quota.can_escalate(chat_model(settings), settings)
    return (True, "") if ok else (False, f"limit: {why}")


def local_answer(chat, text, context, settings):
    """The small local model, with the recent transcript. Only used when a caller passes it as `local=`."""
    from . import ollama
    convo = traces.chat_transcript(chat, n=4, max_chars=4000)
    system = ("You are Waggle, a calm, helpful personal assistant. Claude (the main brain) is unavailable, so you "
              "answer this one message yourself: be brief and honest about what you don't know.\n\n" + context
              + (f"\n\n<recent_conversation>\n{convo}\n</recent_conversation>" if convo else ""))
    return ollama.chat([{"role": "system", "content": system}, {"role": "user", "content": text}],
                       settings=settings, timeout=120).strip()


def run_turn(chat, text, delivery, *, platform="", origin=None, voice=False, t0=None, note="", context=None,
             settings=None, session=None, local=None, stream=True, record=True, address=None, turn_id=None):
    """Answer one user message into `delivery`. Never raises; returns a TurnRecord (also written to traces)."""
    s = settings or Settings.load()
    t0 = t0 or time.time()
    rec = TurnRecord(chat=chat, text=text, origin=origin or origin_for(platform), platform=platform or "")
    if turn_id:
        rec.turn_id = turn_id
    prev_user = 0.0
    try:
        prev_user = chat_store.touch_chat(chat, platform, address=address, user=True)
    except Exception as e:
        events.log("engine_error", stage="touch_chat", error=str(e)[:200])
    try:
        delivery.typing()
    except Exception:
        pass
    writer = StreamWriter(delivery, interval=s.edit_interval_s, stream=stream)
    reply, why = "", ""
    try:
        ctx = context if context is not None else build_context(chat, text, voice, s, since=prev_user)
    except Exception as e:
        ctx = ""
        events.log("engine_error", stage="context", error=str(e)[:200])
    message = (ctx + "\n\n" if ctx else "") + ("<waggle_note>" + note + "</waggle_note>\n\n" if note else "") + text
    ok, why = claude_available(s)
    if ok:
        try:
            sess = session or pool(s).get(chat)
            res = sess.turn(message, on_text=writer.update)
        except Exception as e:  # a bug in the session layer is a failed turn, never a silent one
            res = None
            why = f"error: {type(e).__name__}: {e}"
        if res is not None:
            rec.steps, rec.usage, rec.model = res.steps, res.usage, res.model
            rec.session = {"restarted": res.restarted, "resumed": res.resumed, "retried": res.retried,
                           "fresh": res.fresh_session, "claude_session": res.session_id, "kind": res.kind,
                           "error": res.error[:300], "api_ttft_ms": res.api_ttft_ms}
            try:
                quota.record(res.model or chat_model(s), res.usage, "chat", headless=True, ok=res.ok,
                             error_kind=res.kind or None)
                if res.kind == "limit":
                    quota.note_limit_hit(reset_in_hours=claude_cli.parse_reset_hours(res.error))
            except Exception:
                pass
            answered_by = res.model or chat_model(s)
            if res.ok and res.text.strip():
                reply, rec.answered_by = res.text, answered_by
            elif res.text.strip():  # it said something, then failed: keep what was said, say it was cut off
                reply, rec.answered_by, rec.partial = res.text + CUT_OFF.format(why=_why_short(res.kind)), answered_by, True
                rec.fallback_reason = f"{res.kind or 'error'}: {res.error}"
            else:
                why = f"{res.kind or 'error'}: {res.error or 'empty answer'}"
    if not reply:
        rec.fallback_reason = why
        answer = ""
        if local is not None:
            try:
                answer = (local(chat, text, ctx, s) or "").strip()
            except Exception as e:
                rec.fallback_reason += f" | local model failed: {type(e).__name__}: {e}"[:200]
        if answer:
            reply, rec.answered_by, rec.model = answer + "\n\n" + LOCAL_LABEL.format(why=_why_short(why)), "local", s.local_model
        else:
            reply, rec.answered_by, rec.failure_notice = FAILED.format(why=_why_short(why)), "none", True
    rec.reply = reply
    out = writer.finish(reply)
    rec.delivered = bool(out["delivered"])
    rec.message_ids = list(out.get("message_ids") or [])
    rec.delivery = getattr(delivery, "name", "delivery") if rec.delivered else "outbox"
    rec.delivery_error = out["error"]
    if out["first_shown_at"]:
        rec.ttfw_s = round(out["first_shown_at"] - t0, 2)
    if not rec.delivered:
        rec.outbox_id = queue_reply(rec, address=address)
        if rec.outbox_id is None:
            rec.delivery = "none"
    rec.total_s = round(time.time() - t0, 2)
    rec.settle()
    if record:
        rec.trace_id = record_turn(rec, voice=voice, note=note)
    return rec


def queue_reply(rec, address=None):
    """Put an undelivered reply into the core outbox (once per turn); returns the row id, or None."""
    from waggle.core import outbox
    try:
        return outbox.enqueue("text", plain_text(rec.reply), chat=rec.chat, idem_key=f"turn:{rec.turn_id}",
                              origin="turn", meta={"kind": "reply", "turn_id": rec.turn_id, "platform": rec.platform,
                                                   "address": address or "", "turn_origin": rec.origin,
                                                   "partial_ids": rec.message_ids})
    except Exception as e:
        rec.delivery_error = (rec.delivery_error + f" | outbox failed: {type(e).__name__}: {e}").strip(" |")[:500]
        events.log("engine_error", stage="outbox", error=str(e)[:200])
        return None


def _why_short(why):
    kind, _, msg = (why or "unknown").partition(":")
    return {"limit": "the Claude usage limit is reached", "missing": "the Claude program isn't installed or found",
            "auth": "the Claude sign-in expired", "timeout": "Claude took too long", "crash": "Claude's process failed",
            "error": "Claude reported an error",
            }.get(kind.strip(), (msg or kind).strip()[:120] or "an error")


def record_turn(rec, voice=False, note="", route="chat"):
    try:
        return traces.record(rec.text, session=rec.chat, platform=rec.platform, route=route, model=rec.model,
                             latency_s=rec.total_s, ok=rec.outcome == "ok",
                             error=rec.delivery_error or rec.fallback_reason, reply=rec.reply, tokens=rec.usage,
                             steps=rec.steps, turn_id=rec.turn_id, chat=rec.chat, origin=rec.origin,
                             answered_by=rec.answered_by, fallback_reason=rec.fallback_reason or None,
                             ttfw_s=rec.ttfw_s, total_s=rec.total_s, delivered=rec.delivered, delivery=rec.delivery,
                             delivery_error=rec.delivery_error or None, message_ids=rec.message_ids,
                             turn_outcome=rec.outcome, outcome_why=rec.outcome_why, claims=rec.claims,
                             extra={"voice": voice, "note": bool(note), "outbox": rec.outbox_id,
                                    "session": rec.session, "tools": [st.get("tool") for st in rec.steps][:30]})
    except Exception as e:
        events.log("engine_error", stage="trace", error=str(e)[:200])
        return None


def record_direct(chat, text, reply, *, platform="", origin=None, t0=None, route="direct", delivered=True,
                  delivery="block", message_ids=None, failure_notice=False, address=None):
    """A reply Waggle gave without a model (approval answers, feedback acks): still one full turn record.
    An undelivered one is queued in the core outbox first."""
    t0 = t0 or time.time()
    took = round(time.time() - t0, 2)
    rec = TurnRecord(chat=chat, text=text, reply=reply, origin=origin or origin_for(platform), platform=platform,
                     answered_by="none", delivered=delivered, delivery=delivery, total_s=took,
                     ttfw_s=took if delivered else None, message_ids=list(message_ids or []),
                     failure_notice=failure_notice)
    if not delivered:
        rec.outbox_id = queue_reply(rec, address=address)
        rec.delivery = "outbox" if rec.outbox_id is not None else "none"
    rec.settle()
    rec.trace_id = record_turn(rec, route=route)
    return rec


# ---- the core's entry point ---------------------------------------------------------------------------------

_delivery_factories = {}


def set_delivery_factory(platform, factory):
    """factory(address) -> Delivery for one chat on `platform` (tests plug in fakes; None removes it)."""
    if factory is None:
        _delivery_factories.pop(platform, None)
    else:
        _delivery_factories[platform] = factory


def address_for(chat, platform):
    """The transport address of a chat key: "telegram:123" -> "123"; anything else is its own address."""
    prefix = f"{platform}:"
    return chat[len(prefix):] if platform and str(chat).startswith(prefix) else str(chat)


def delivery_for(chat, platform, address=None):
    address = address or address_for(chat, platform)
    factory = _delivery_factories.get(platform)
    if factory is not None:
        return factory(address)
    if platform == "telegram":
        from .delivery import BotApiDelivery
        return BotApiDelivery(address)
    raise ValueError(f"no delivery for platform {platform!r}")


def media_note(media):
    """What the brain is told about attachments: their local paths (it can Read them)."""
    items = media if isinstance(media, (list, tuple)) else [media]
    lines = []
    for m in items:
        if isinstance(m, dict):
            path, kind = m.get("path") or m.get("file") or "", m.get("kind") or m.get("type") or "file"
        else:
            path, kind = str(m), "file"
        if path:
            lines.append(f"- {kind}: {path}")
    return ("The user attached:\n" + "\n".join(lines)) if lines else ""


NO_TEXT = ("I got your message but couldn't read anything in it{what}. Could you send it again, or type it?")


def handle_inbound(chat, text, *, kind="text", media=None, platform="telegram", origin="user", delivery=None,
                   settings=None, session=None, t0=None, local=None):
    """One inbound message, end to end: approvals first, then the chat's Claude session, streamed into the
    chat's delivery, the reply queued in the core outbox if delivery fails, and the full turn record.

    kind: "text" | "voice" (text is the transcript; the reply is written to be spoken) | anything else with
    `media` (paths, or dicts with path/kind) the brain is told about. origin: "user" only for the owner's own
    channel. Never raises for turn failures; returns the TurnRecord."""
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}, not {origin!r}")
    s = settings or Settings.load()
    t0 = t0 or time.time()
    address = address_for(chat, platform)
    d = delivery or delivery_for(chat, platform, address)
    voice = kind == "voice"
    body = (text or "").strip()
    extra = media_note(media) if media else ""
    if extra:
        body = (body + "\n\n" + extra).strip()
    prev = (chat_store.get_chat(chat) or {}).get("last_user") or 0.0

    def direct(reply, route, failure_notice=False):
        chat_store.touch_chat(chat, platform, address=address, user=True)
        out = StreamWriter(d, stream=False).finish(reply)
        return record_direct(chat, body, reply, platform=platform, origin=origin, t0=t0, route=route,
                             delivered=out["delivered"], delivery=getattr(d, "name", "delivery"),
                             message_ids=out.get("message_ids"), failure_notice=failure_notice, address=address)

    if not body:
        what = " (the voice note could not be transcribed)" if voice else ""
        return direct(NO_TEXT.format(what=what), "empty", failure_notice=True)
    try:
        pre = approval_preflight(chat, body, prev)
    except Exception as e:
        events.log("engine_error", stage="approvals", error=str(e)[:200])
        pre = None
    if pre and pre[0] == "reply":
        return direct(pre[1], "approval")
    return run_turn(chat, body, d, platform=platform, origin=origin, voice=voice, t0=t0,
                    note=pre[1] if pre else "", settings=s, session=session, local=local, address=address)


def register_core():
    """Register the engine's job kinds with the core (waggle.core.jobs). Returns their names."""
    from . import chat_jobs
    return chat_jobs.register_core()
