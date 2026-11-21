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
   none) and why, every tool call with its name, success and result (redacted, 500 characters), time to first
   words, total time, delivery result and message ids, what the answer claims to have produced, the code version
   and the message's origin; outcome ok / recovered / degraded / failed by traces.turn_outcome().

Origin (plan decision 10): only the owner's own channel is "user"; the command-line chat, tests, canaries,
benches and other agents are marked so nothing learns from them (origin_for()).
"""
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import chat_store, claude_cli, engine_commands, events, media_intake, quota, stt, traces, voice_reply, voice_turn
from . import guard  # turn state for the owner-instruction rule
from .chat_session import VOICE_STYLE, ChatSession, SessionPool
from .config import Settings, data_path
from .delivery import StreamWriter, Typing, plain_text

ORIGINS = traces.ORIGINS
TEST_PLATFORMS = {"cli", "local", "test", "api", "desktop", ""}  # the command-line chat and test clients
AGENT_PLATFORMS = {"subagent", "delegation", "cron", "kanban", "agent"}

LOCAL_LABEL = "(Answered by Waggle's small local model because Claude is unavailable right now: {why}.)"
FAILED = "I couldn't answer that just now: {why}. Nothing was done with it; please send it again in a few minutes."
CUT_OFF = "\n\n(I was cut off before I finished: {why}.)"
INTERRUPTED_NOTE = ("Waggle restarted while you were answering this message before. Before you repeat any action "
                    "(writing or changing files, commands, commits, messages, background_task), check what was "
                    "already done (files, git log, background_status) and only do what is still missing. Say "
                    "briefly what you found already done.")
INTERRUPTED_LINE = "\n\n(A restart interrupted my first try at this, so I checked what was already done first.)"

CONTEXT_BUDGET_S = 0.8  # the notes search may take this long before the model is asked without it
VOICE_CONTEXT_BUDGET_S = 0.4  # ...and on a voice turn, where every tenth of a second is heard as waiting
AVAILABLE_TTL_S = 30.0  # the usage-limit check reads Claude Code's transcripts (0.3 s and growing): once per 30 s
TYPING_EVERY_S = 4.0  # Telegram shows "typing..." for about 5 s per call

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


TEST_MARK = re.compile(r"^\s*\[waggle-test\]\s*", re.I)


def test_marked(text):
    """(is_test, text without the mark). The owner's own account may prefix a message with "[waggle-test]" (the
    end-to-end test client does) so it is answered normally but recorded as origin "canary": nothing learns from it,
    and what it sets up (a reminder) is still delivered to that chat, so the test proves delivery. The mark can only
    lower trust, never raise it: only the owner's own message ("user") becomes "canary"."""
    m = TEST_MARK.match(text or "")
    return (True, (text or "")[m.end():]) if m else (False, text)


def origin_for_message(platform, text, env=None):
    """origin_for(platform), demoted when the text carries the test mark (see marked_origin)."""
    origin = origin_for(platform, env)
    return marked_origin(origin) if test_marked(text)[0] else origin


def marked_origin(origin):
    """The origin of a test-marked message: the owner's own message becomes "canary" (an end-to-end check from the
    owner's account: answered, delivered, never learned from); any other origin stays as it is."""
    return "canary" if origin == "user" else origin


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
    outcome: str = "failed"  # ok | recovered | degraded | failed (traces.turn_outcome)
    outcome_why: list = field(default_factory=list)
    ttfw_s: float = None  # message received -> the first real words of the answer on screen
    first_visible_s: float = None  # ...the first post of any kind (a tool status line included)
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
    voice: dict = field(default_factory=dict)  # a voice turn: transcript source, stage times, the spoken reply
    context: dict = field(default_factory=dict)  # which parts of the context block this turn really carried

    def _voice_why(self):
        """A voice turn whose spoken reply was asked for but not sent is degraded, with the reason."""
        if not self.voice.get("voice_requested") or self.voice.get("voice_delivered") or self.failure_notice:
            return ()
        return (f"spoken reply not sent: {self.voice.get('voice_error') or 'unknown'}",)

    def settle(self):
        """Outcome and claims from what happened (no model)."""
        self.outcome, self.outcome_why = traces.turn_outcome(
            answered_by=self.answered_by, delivered=self.delivered, queued=self.outbox_id is not None,
            steps=self.steps, partial=self.partial, failure_notice=self.failure_notice,
            degraded_why=self._voice_why())
        self.claims = traces.extract_claims("" if self.failure_notice else self.reply, self.steps)
        return self


# ---- context ------------------------------------------------------------------------------------------------

class ContextBlock(str):
    """The context text of one turn, with `marker`: what it really carried. The marker is written on the turn's
    trace (extra.context) so "did the context step work?" can be checked afterwards (context_alive.py: doctor and
    the daily canary). {"v", "core": the core block was in it, "sources": the parts that produced text,
    "lessons_checked": the lessons step ran, "lessons": ids injected, "chars"}."""
    marker = None


def build_context(chat, text, voice=False, settings=None, since=None, budget_s=None):
    """The per-turn <waggle_context> block (a ContextBlock: a str that also carries `.marker`). Every source is
    optional: a failing one is skipped, never fatal. budget_s: how long the notes search may take (default
    CONTEXT_BUDGET_S)."""
    parts = []
    marker = {"v": 1, "core": False, "sources": [], "lessons_checked": False, "lessons": []}

    def add(name, fn, budget=None):
        """budget: seconds this source may take; a slower one (the memory search embeds the message through
        Ollama: 2 s measured when Ollama is cold or off) is left out, because the model must not wait for it."""
        try:
            if budget is None:
                out = fn()
            else:
                box = {}

                def go():
                    try:
                        box["out"] = fn()
                    except Exception as e:
                        box["err"] = e
                th = threading.Thread(target=go, daemon=True, name=f"waggle-context-{name}")
                th.start()
                th.join(budget)
                if th.is_alive():
                    events.log("context_slow", source=name, budget_s=budget)
                    return
                if "err" in box:
                    raise box["err"]
                out = box.get("out")
            if out and out.strip():
                parts.append(out.strip())
                marker["sources"].append(name)
                if name == "core" and "<waggle_core>" in out:
                    marker["core"] = True
        except Exception as e:
            events.log("context_error", source=name, error=str(e)[:200])

    def lessons_step():
        from . import lessons
        marker["lessons_checked"] = True
        got, ids = lessons.block(text or "")
        marker["lessons"] = ids
        return got

    from . import memory, projects
    # The small maintained core (who the user is, current focus, commitments, deadlines, yesterday) is read from
    # files and the facts table: always there, never waiting on the embedder. Retrieval is gated: only notes that
    # clearly stand out for this message, none at all otherwise, and the memory tools (note_read, related_to,
    # vault_search, recall) are there for the rest. Lessons (waggle/lessons.py) are the owner's behaviour rules,
    # added only when the message contains one of their keywords (at most 3, 600 characters).
    add("core", lambda: memory.safe_core_block(settings)[:6500])
    add("lessons", lessons_step)
    add("memory", lambda: memory.retrieval_block(text or "", settings, session=chat)[:4500], budget=budget_s or CONTEXT_BUDGET_S)
    add("projects", lambda: projects.context_for(text or "")[:2000])
    add("graph", lambda: __import__("waggle.graph", fromlist=["x"]).context_for(text))
    add("recall", lambda: traces.context_for(text))
    add("approvals", lambda: approvals_now(chat))
    add("news", unseen_news)
    add("deliveries", lambda: recent_deliveries(chat, since=since))
    if voice:
        from .voice_turn import VOICE_HINT
        parts.append(VOICE_HINT + "\n" + VOICE_STYLE)
    parts.append(f"Now: {time.strftime('%A %Y-%m-%d %#I:%M %p')} (user's local time). Write every time for the user "
                 "in 12-hour form with AM or PM (5:44 PM), never in 24-hour form.")
    block = ContextBlock("<waggle_context>\n" + "\n\n".join(parts) + "\n</waggle_context>")
    marker["chars"] = len(block)
    block.marker = marker
    return block


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
    from . import learnloop, objectives, policy
    um = learnloop.UNDO_CMD.match(text or "")
    if um:  # "undo L7": reverts one change of the learning loop, no model involved
        return "reply", learnloop.undo_reply(int(um.group(1)))
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
        if item.get("tool") == engine_commands.MEDIA_CLEAN_TOOL and sess == chat:  # Waggle's own question: no model
            return "reply", engine_commands.media_clean_answer(
                item, chat, Settings.load(), approve=verb not in ("deny", "reject", "refuse", "no"))
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
    cleans = [p for p in mine if p.get("tool") == engine_commands.MEDIA_CLEAN_TOOL]
    if cleans:  # a yes to "clean the saved attachments?" answers that question and nothing else
        others = [p for p in mine if p not in cleans]
        if others:
            lines = "\n".join("- " + policy.describe(p) for p in mine[:6])
            return "reply", (f"More than one thing is waiting, so I won't act on a plain yes. Open requests:\n{lines}\n"
                             f"Reply \"approve CODE\" (or \"deny CODE\") for the one you mean.")
        newest = cleans[-1]  # pending() is oldest first; an older snapshot of the folder is superseded
        for old in cleans[:-1]:
            policy.deny(old["id"])
        return "reply", engine_commands.media_clean_answer(newest, chat, Settings.load())
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


_available = {}  # model -> when the usage check last said yes


def claude_available(settings):
    """(ok, reason). A known quota block goes straight to the honest failure; a missing or broken CLI is found
    when the chat's process starts."""
    key = chat_model(settings)
    from . import cooldowns
    rest = cooldowns.describe("claude", settings=settings)
    if rest:  # it just failed (a limit, billing, overload or sign-in error): the local model answers at once
        _available.pop(key, None)
        return False, f"{cooldowns.cooling('claude', settings=settings)[1]}: Claude is {rest}"
    cached = _available.get(key)
    if cached and time.time() - cached < AVAILABLE_TTL_S:
        return True, ""
    # The owner's own chat is refused only after Claude itself said the limit is reached (a recorded block).
    # Waggle's estimate of the window is for background work: it read 97% while the real window was 51%
    # (2026-11-20) and turned the owner away. A wrong "yes" costs one failed call, then the local model answers.
    blocked = float((quota._state() or {}).get("blocked_until") or 0)
    if blocked > time.time():
        _available.pop(key, None)
        return False, f"limit: Claude limit reached; retry in about {int((blocked - time.time()) / 60)} min"
    _available[key] = time.time()
    return True, ""


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
             settings=None, session=None, local=None, stream=True, record=True, address=None, turn_id=None,
             typing=None, attempt=1, voice_info=None, attachments="", owner_text=None):
    """Answer one user message into `delivery`. Never raises; returns a TurnRecord (also written to traces).

    typing: a running delivery.Typing (else one is started here); it stops when the first words are posted.
    attempt >= 2: the core restarted while this message was being answered. The model is told to check what was
    already done before repeating any action, and the reply tells the user a restart interrupted the work.
    attachments: the <waggle_attachments> block (media_note) that goes to the brain after the message; it is data,
    so owner_text (the owner's own words: the caption, "" when there is none; default: `text`) is what the
    safety turn state and the notes search see, never the block."""
    s = settings or Settings.load()
    t0 = t0 or time.time()
    rec = TurnRecord(chat=chat, text=text, origin=origin or origin_for(platform), platform=platform or "")
    rec.voice = dict(voice_info or {})
    if voice:
        heard = heard_line(rec.voice.get("transcript_cleaned") or text)
        if heard:
            rec.voice["heard_line"] = heard  # shown at the top of the written reply, never spoken
    vr = None
    if voice and getattr(delivery, "can_voice", False):  # a chat that takes voice messages: spoken answer + text
        ext, convert, wants_audio = voice_reply.pipeline(getattr(delivery, "voice_format", "ogg"))
        vr = voice_reply.VoiceReply(data_path("voice_out"), voice=s.voice_note_voice, speed=s.voice_note_speed,
                                    synth=voice_reply.synth_wav, convert=convert, ext=ext, audio=wants_audio,
                                    on_sentence=getattr(delivery, "on_sentence", None))  # the PC voice's stream
        rec.voice.update(voice=True, voice_requested=wants_audio)
        stream = False  # the voice message comes first, then the whole text
    if turn_id:
        rec.turn_id = turn_id
    keeper = typing or Typing(delivery, every=TYPING_EVERY_S).start()
    prev_user = 0.0
    try:
        prev_user = chat_store.touch_chat(chat, platform, address=address, user=True)
    except Exception as e:
        events.log("engine_error", stage="touch_chat", error=str(e)[:200])
    writer = StreamWriter(delivery, interval=s.edit_interval_s, stream=stream, on_first_shown=keeper.stop)  # first post of any kind stops the typing indicator
    if attempt and attempt >= 2:
        note = (note + " " if note else "") + INTERRUPTED_NOTE
    reply, why = "", ""
    try:
        t_ctx = time.time()
        ctx = context if context is not None else build_context(
            chat, text if owner_text is None else owner_text, voice and vr is None, s, since=prev_user,
            budget_s=VOICE_CONTEXT_BUDGET_S if voice else None)
        if voice:
            rec.voice["context_s"] = round(time.time() - t_ctx, 2)
        rec.context = dict(getattr(ctx, "marker", None) or {"v": 1, "given": True})
    except Exception as e:
        ctx = ""
        rec.context = {"v": 1, "core": False, "built": False, "error": type(e).__name__}
        events.log("engine_error", stage="context", error=str(e)[:200])
    message = ((ctx + "\n\n" if ctx else "") + ("<waggle_note>" + note + "</waggle_note>\n\n" if note else "")
               + ("<voice_reply_format>" + voice_reply.FORMAT_NOTE + "</voice_reply_format>\n\n" if vr else "") + text
               + ("\n\n" + attachments if attachments else ""))
    ok, why = claude_available(s)
    if ok:
        try:
            sess = session or pool(s).get(chat)
            t_model = time.time()
            guard.write_turn_state(chat, rec.turn_id, rec.origin, text if owner_text is None else owner_text,
                                   voice=voice, confident=rec.voice.get(
                "stt_confident", True), max_age_s=(s.chat_turn_timeout_s or 600) + 60)  # what the owner said (policy)
            res = sess.turn(message, on_text=vr.feed if vr else _after_heard(writer.update, rec.voice.get("heard_line")),
                            on_tool=None if vr else writer.tool,
                            origin=rec.origin)
            guard.end_turn_state(chat, rec.turn_id)
            rec.voice["model_s"] = round(time.time() - t_model, 2) if vr else None
        except Exception as e:  # a bug in the session layer is a failed turn, never a silent one
            res = None
            why = f"error: {type(e).__name__}: {e}"
        if res is not None:
            rec.steps, rec.usage, rec.model = res.steps, res.usage, res.model
            rec.session = {"restarted": res.restarted, "resumed": res.resumed, "retried": res.retried,
                           "fresh": res.fresh_session, "claude_session": res.session_id, "kind": res.kind,
                           "error": res.error[:300], "api_ttft_ms": res.api_ttft_ms, "attempt": attempt,
                           "model_first_text_s": res.ttfw_s}  # when Claude's first words arrived (ttfw_s: shown)
            try:
                quota.record(res.model or chat_model(s), res.usage, "chat", headless=True, ok=res.ok,
                             error_kind=res.kind or None)
                if res.kind == "limit":
                    _available.clear()  # the next message is checked against the new block
                    quota.note_limit_hit(reset_in_hours=claude_cli.parse_reset_hours(res.error))
                if res.kind and not res.ok:
                    from . import cooldowns
                    if cooldowns.note_error("claude", res.kind, res.error, settings=s,
                                            reset_in_hours=claude_cli.parse_reset_hours(res.error)):
                        _available.clear()
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
    if vr and not rec.failure_notice:
        reply, _ = vr.finish(reply)  # the written answer without its SPOKEN part
    elif not vr:
        _, answer_only, had_spoken = voice_reply.split_reply(reply)
        if had_spoken and answer_only.strip():
            # a typed turn in a chat that recently had voice turns: the brain copied the voice format. Its labels
            # must never reach the user (seen 2026-11-20: "SPOKEN: ... ANSWER: ..." sent as text).
            reply = answer_only
    if attempt and attempt >= 2 and not rec.failure_notice:
        reply += INTERRUPTED_LINE
    rec.reply = reply
    if vr:
        send_spoken(rec, vr, delivery, t0)
    out = writer.finish(shown_reply(rec))
    keeper.stop()
    rec.delivered = bool(out["delivered"])
    rec.message_ids = list(out.get("message_ids") or [])
    rec.delivery = getattr(delivery, "name", "delivery") if rec.delivered else "outbox"
    rec.delivery_error = out["error"]
    if out["first_shown_at"]:
        rec.ttfw_s = round(out["first_shown_at"] - t0, 2)
    if out.get("first_visible_at"):
        rec.first_visible_s = round(out["first_visible_at"] - t0, 2)
    if not rec.delivered:
        rec.outbox_id = queue_reply(rec, address=address)
        if rec.outbox_id is None:
            rec.delivery = "none"
    rec.total_s = round(time.time() - t0, 2)
    rec.settle()
    if record:
        rec.trace_id = record_turn(rec, voice=voice, note=note)
    return rec


def send_spoken(rec, vr, delivery, t0):
    """The voice message of a voice turn, before its text: waits for the audio the speech started while the model
    wrote, sends it, and records what happened (stage times, message id) in rec.voice and the capability ledger.
    A failure never fails the turn: the text goes out alone and the turn is marked degraded with the reason."""
    if rec.failure_notice:
        rec.voice.update(voice_delivered=False, voice_error="no answer to speak")
        return
    try:
        info = vr.send(delivery)
    except Exception as e:  # nothing about audio may fail the turn
        info = {"voice_delivered": False, "voice_message_id": None, "voice_error": f"{type(e).__name__}: {e}"[:200]}
    rec.voice.update(info)
    rec.voice["voice_sent_s"] = round(time.time() - t0, 2)
    if not vr.audio or getattr(delivery, "voice_capability", "telegram_voice") is None:
        return  # only the spoken text was wanted, or this is not a Telegram voice message: the ledger is not about it
    try:
        from waggle.core import ledger
        if info.get("voice_delivered"):
            ledger.record("telegram_voice", True, method="bot api message id",
                          evidence_ref=f"voice message id {info.get('voice_message_id')} in {rec.chat}")
        else:
            ledger.record("telegram_voice", False, method="bot api voice message",
                          detail=str(info.get("voice_error") or "not sent")[:200])
    except Exception as e:
        events.log("engine_error", stage="voice_ledger", error=str(e)[:200])


HEARD_MAX = 200


def heard_line(transcript):
    """The first line of the written reply to a voice turn: what Waggle heard, in quotes, cut to HEARD_MAX
    characters with an ellipsis. "" when there is nothing to show."""
    t = " ".join(str(transcript or "").split())
    if not t:
        return ""
    if len(t) > HEARD_MAX:
        t = t[:HEARD_MAX - 1].rstrip() + "…"
    return f'Heard: "{t}"'


def shown_reply(rec):
    """The reply as the user sees it: a voice turn's text starts with its Heard line and a blank line; the record
    keeps the answer alone (its claims are read from the answer, not from the user's own words)."""
    line = rec.voice.get("heard_line") if rec.voice else ""
    return f"{line}\n\n{rec.reply}" if line else rec.reply


def _after_heard(update, line):
    """A streaming callback that shows the Heard line above the text so far (no line: the callback itself)."""
    return (lambda text: update(f"{line}\n\n{text}")) if line else update


def queue_reply(rec, address=None):
    """Put an undelivered reply into the core outbox (once per turn); returns the row id, or None."""
    from waggle.core import outbox
    try:
        return outbox.enqueue("text", plain_text(shown_reply(rec)), chat=rec.chat, idem_key=f"turn:{rec.turn_id}",
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


def tool_calls(steps, limit=40):
    """Every tool call of a turn for the trace's extra: name, whether it worked, whether the safety hook refused
    it, and its result or error, redacted and cut to traces.STEP_RESULT_CAP characters (the steps table holds the
    same, row by row, plus the input)."""
    out = []
    for st in (steps or [])[:limit]:
        out.append({"tool": st.get("tool") or "", "status": traces.step_status(st),
                    "ok": st.get("ok"), "denied": bool(st.get("denied")),
                    "result": traces.redact(str(st.get("result") or ""))[:traces.STEP_RESULT_CAP]})
    return out


def record_turn(rec, voice=False, note="", route="chat"):
    try:
        return traces.record(rec.text, session=rec.chat, platform=rec.platform, route=route, model=rec.model,
                             latency_s=rec.total_s, ok=rec.outcome in ("ok", "recovered"),
                             error=rec.delivery_error or rec.fallback_reason, reply=rec.reply, tokens=rec.usage,
                             steps=rec.steps, turn_id=rec.turn_id, chat=rec.chat, origin=rec.origin,
                             answered_by=rec.answered_by, fallback_reason=rec.fallback_reason or None,
                             ttfw_s=rec.ttfw_s, total_s=rec.total_s, delivered=rec.delivered, delivery=rec.delivery,
                             delivery_error=rec.delivery_error or None, message_ids=rec.message_ids,
                             turn_outcome=rec.outcome, outcome_why=rec.outcome_why, claims=rec.claims,
                             extra={"voice": voice, "voice_turn": rec.voice or None, "note": bool(note), "outbox": rec.outbox_id,
                                    "session": rec.session, "tools": tool_calls(rec.steps),
                                    "first_visible_s": rec.first_visible_s, "context": rec.context or None})
    except Exception as e:
        events.log("engine_error", stage="trace", error=str(e)[:200])
        return None


def record_direct(chat, text, reply, *, platform="", origin=None, t0=None, route="direct", delivered=True,
                  delivery="block", message_ids=None, failure_notice=False, address=None, turn_id=None, voice=None):
    """A reply Waggle gave without a model (approval answers, feedback acks): still one full turn record.
    An undelivered one is queued in the core outbox first."""
    t0 = t0 or time.time()
    took = round(time.time() - t0, 2)
    rec = TurnRecord(chat=chat, text=text, reply=reply, origin=origin or origin_for(platform), platform=platform,
                     answered_by="none", delivered=delivered, delivery=delivery, total_s=took,
                     ttfw_s=took if delivered else None, message_ids=list(message_ids or []),
                     failure_notice=failure_notice)
    rec.voice = dict(voice or {})
    if turn_id:
        rec.turn_id = turn_id
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


def media_note(media, failed=()):
    """What the brain is told about attachments: a <waggle_attachments> block with one <attachment path=... type=...
    .../> per file (it Reads them). Attachments are data, so the block is passed beside the owner's words (run_turn's
    `attachments`), never inside them."""
    items = media if isinstance(media, (list, tuple)) else [media]
    return media_intake.attachment_note([m for m in items if m], failed)


MEDIA_KINDS = ("photo", "image", "document", "file", "audio", "video", "location")
TRANSCRIBE_MAX_UNKNOWN_BYTES = 12 * 1024 * 1024  # an audio file of unknown length above this is attached, not transcribed
TRANSCRIPT_MAX_CHARS = 8000


def transcribe_attachment(att, settings):
    """Give an audio file attachment its transcript when it is short (settings.audio_transcribe_max_s, 10 minutes),
    with the same local recogniser as voice notes (waggle/stt.py); otherwise say in `note` why it was only attached.
    The words are the file's content, not the owner's: they stay inside the attachment block."""
    limit = float(getattr(settings, "audio_transcribe_max_s", 600) or 600)
    dur = att.get("duration") or media_intake.audio_duration_s(att["path"])
    if dur is None and att.get("size", 0) > TRANSCRIBE_MAX_UNKNOWN_BYTES:
        att["note"] = "length unknown and the file is large: not transcribed"
        return att
    if dur is not None and float(dur) > limit:
        att["note"] = f"{float(dur) / 60:.0f} minutes long: not transcribed (the limit is {limit / 60:g})"
        return att
    res = stt.transcribe(att["path"])
    if res.get("ok") and (res.get("text") or "").strip():
        att["transcript"] = res["text"].strip()[:TRANSCRIPT_MAX_CHARS]
        att["note"] = "transcript made by Waggle's local speech recogniser; it may contain mistakes"
    else:
        att["note"] = f"could not be transcribed: {str(res.get('error') or 'no speech')[:100]}"
    return att


def _length_words(seconds):
    s = int(round(float(seconds)))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def describe_video(att, settings):
    """Look at a saved video with local tools only: its length and size, up to three still frames saved beside it
    (the brain Reads them), the words in its sound when it is short (settings.video_transcribe_max_s, 120 s, the
    same local recogniser as voice notes), and a one-line summary of all that. Nothing here calls Claude, and no
    step is needed for the file to be filed: without ffmpeg the video is only attached, and the note says so.
    The transcript is the video's content, not the owner's words: it stays inside the attachment block."""
    path = att["path"]
    try:
        info = media_intake.video_info(path)
        if not info["ok"]:
            att["summary"] = "a video, not looked at"
            att["note"] = "ffmpeg is not available or could not read it: no length, size or frames"
            return att
        dur = info["duration"] if info["duration"] is not None else att.get("duration")
        if dur is not None:
            att["duration"] = round(float(dur), 1)
        if info["width"]:
            att["width"], att["height"] = info["width"], info["height"]
        frames = media_intake.extract_frames(path, dur)
        if frames:
            att["frames"] = frames
        limit = float(getattr(settings, "video_transcribe_max_s", 120) or 120)
        parts = [f"{_length_words(dur)} video" if dur is not None else "video"]
        if info["width"]:
            parts.append(f"{info['width']}x{info['height']}")
        parts.append(f"{len(frames)} frame(s) saved" if frames else "no frames could be taken")
        if info["has_audio"] is False:
            parts.append("no sound")
        elif info["has_audio"] and dur is None:
            parts.append("sound not transcribed (length unknown)")
        elif info["has_audio"] and float(dur) > limit:
            parts.append(f"sound not transcribed (over {limit / 60:g} min)")
        elif info["has_audio"]:
            wav = Path(path).with_name(Path(path).stem + "-sound.wav")
            heard = ""
            if media_intake.extract_audio(path, wav):
                try:
                    res = stt.transcribe(str(wav))
                finally:
                    wav.unlink(missing_ok=True)
                heard = (res.get("text") or "").strip() if res.get("ok") else ""
            if heard:
                att["transcript"] = heard[:TRANSCRIPT_MAX_CHARS]
                att["note"] = "transcript made by Waggle's local speech recogniser; it may contain mistakes"
                parts.append("speech: " + (heard[:120] + "..." if len(heard) > 120 else heard))
            else:
                parts.append("no speech recognised")
        att["summary"] = "; ".join(parts)
    except Exception as e:  # a bad file must never stop the turn: it is still attached
        events.log("engine_error", stage="describe_video", error=str(e)[:200])
        att.setdefault("summary", "a video, not looked at")
        att["note"] = f"could not be looked at ({type(e).__name__})"
    return att


def _ingest_location(chat, media):
    """media_intake.ingest's answer for a shared location: the pin saved as a file, or why it was not."""
    ok, failed = [], []
    for item in list(media or [])[:1]:  # one pin per message
        try:
            ok.append(media_intake.save_location(chat, item if isinstance(item, dict) else {}))
        except ValueError as e:
            failed.append({"name": "location", "why": str(e)})
        except Exception as e:
            events.log("engine_error", stage="save_location", error=str(e)[:200])
            failed.append({"name": "location", "why": "it could not be saved"})
    if not ok and not failed:
        failed.append({"name": "location", "why": "no coordinates came with it"})
    return {"ok": ok, "failed": failed}


def _failed_reply(failed):
    what = "; ".join(f"{f['name']}: {f['why']}" for f in failed[:3]) or "it was not available"
    return f"I couldn't use what you sent ({what}). Nothing was done with it; try sending it again, or a smaller file."


NO_TEXT = ("I got your message but couldn't read anything in it{what}. Could you send it again, or type it?")


def handle_inbound(chat, text, *, kind="text", media=None, platform="telegram", origin="user", delivery=None,
                   settings=None, session=None, t0=None, local=None, turn_id=None, attempt=1, address=None,
                   audio=None, voice_source=""):
    """One inbound message, end to end: the typing indicator at once, approvals first, then the chat's Claude
    session, streamed into the chat's delivery, the reply queued in the core outbox if delivery fails, and the
    full turn record.

    kind: "text" | "voice" (text is the transcript, or empty with `audio` = the voice note's file, which is
    transcribed here; the reply is a short spoken version plus the full text) | anything else with
    `media` (paths, or dicts with path/kind) the brain is told about. origin: "user" only for the owner's own
    channel. attempt 2: a restart interrupted the first run (see run_turn); a turn already delivered under the
    same turn_id is not answered again. Never raises for turn failures; returns the TurnRecord."""
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}, not {origin!r}")
    s = settings or Settings.load()
    t0 = t0 or time.time()
    address = str(address or address_for(chat, platform))
    d = delivery or delivery_for(chat, platform, address)
    keeper = Typing(d, every=TYPING_EVERY_S).start()  # something visible at once
    try:  # the sender resolves the chat key through this row: it exists before anything is queued for the chat
        chat_store.touch_chat(chat, platform, address=address)
    except Exception as e:
        events.log("engine_error", stage="touch_chat", error=str(e)[:200])
    if turn_id and attempt and attempt >= 2:
        done = traces.by_turn_id(turn_id)
        if done and done.get("delivered") == 1:
            keeper.stop()
            rec = TurnRecord(chat=chat, text=text, turn_id=turn_id, origin=origin, platform=platform,
                             reply=done.get("reply") or "", answered_by=done.get("answered_by") or "none",
                             delivered=True, delivery="already", outcome=done.get("turn_outcome") or "ok",
                             trace_id=done.get("id"))
            events.log("engine_turn_already_done", turn_id=turn_id, trace_id=done.get("id"))
            return rec
    voice = kind == "voice"
    vinfo = {}
    if voice and audio and not (text or "").strip():
        text, vinfo = transcribe_note(audio, s)
    elif voice and (text or "").strip():  # a transcript that still carries Hermes's media marker: words only
        words, k = voice_turn.parse(text, getattr(s, "voice_fixes", None))
        if k == "voice":
            vinfo = {"voice": True, "transcript_source": "hermes gateway", "audio_path": voice_turn.audio_path(text),
                     "transcript_raw": voice_turn.parse(text, apply_fixes=False)[0]}
            text = stt.clean_transcript(words)
            vinfo["transcript_cleaned"] = text
            vinfo.update(stt_confident=False, stt_confident_why=["the gateway gave no recogniser numbers"])
        elif voice_source:  # words dictated elsewhere (the phone's own dictation): cleaned, but no recogniser numbers
            raw = text.strip()
            text = stt.clean_transcript(voice_turn.fix(raw, getattr(s, "voice_fixes", None)))
            vinfo = {"voice": True, "transcript_source": voice_source, "transcript_raw": raw,
                     "transcript_cleaned": text, "stt_confident": False,
                     "stt_confident_why": [f"{voice_source}: no recogniser numbers"]}
    body = (text or "").strip()
    prev = (chat_store.get_chat(chat) or {}).get("last_user") or 0.0

    def direct(reply, route, failure_notice=False):
        keeper.stop()
        chat_store.touch_chat(chat, platform, address=address, user=True)
        line = heard_line(vinfo.get("transcript_cleaned") or body) if voice and body else ""
        info = dict(vinfo) or ({"voice": True} if voice else {})
        if line:
            info["heard_line"] = line  # a voice turn's written reply starts with what was heard; the record keeps the answer
        out = StreamWriter(d, stream=False).finish(f"{line}\n\n{reply}" if line else reply)
        rec = record_direct(chat, body, reply, platform=platform, origin=origin, t0=t0, route=route,
                            delivered=out["delivered"], delivery=getattr(d, "name", "delivery"),
                            message_ids=out.get("message_ids"), failure_notice=failure_notice, address=address,
                            turn_id=turn_id, voice=info or None)
        return rec

    if kind == "command":  # a command Waggle serves itself (waggle/engine_commands.py): no model
        return direct(engine_commands.serve(body, s, chat=chat, origin=origin), "command")
    if kind == "sticker":  # a short local answer, never a model call
        emoji = next((m.get("emoji") for m in (media or []) if isinstance(m, dict) and m.get("emoji")), "")
        body = f"(sticker {emoji})".replace(" )", ")")
        return direct(engine_commands.sticker_reply(emoji), "sticker")
    attachments, owner_text = "", None
    if kind == "location" or (media and not voice):
        # Photos, videos, files, audio files, location pins: saved to the chat's media folder, described in a note
        # beside the caption. The caption is the owner's own words (like typed text); the files, their names, a
        # video's frames and any transcript, and a pin's place name are data.
        got = _ingest_location(chat, media) if kind == "location" else media_intake.ingest(chat, media, settings=s)
        for att in got["ok"]:
            if att["kind"] == "audio":
                transcribe_attachment(att, s)
            elif att["kind"] == "video":
                describe_video(att, s)
        attachments = media_note(got["ok"], got["failed"])
        if not got["ok"] and not body:
            return direct(_failed_reply(got["failed"]), "attachment", failure_notice=True)
        owner_text = body
        if not body and got["ok"]:
            body = media_intake.placeholder(got["ok"])
    if not body:
        if voice and vinfo:
            return direct(voice_turn.FAILED_REPLY, "empty", failure_notice=True)
        what = " (the voice note could not be transcribed)" if voice else ""
        return direct(NO_TEXT.format(what=what), "empty", failure_notice=True)
    if kind == "text" and not media:  # "merge CODE" / "drop CODE" on a proposal (waggle/proposals.py): typed words only
        try:
            from . import proposals
            answer = proposals.preflight(chat, body, origin=origin, kind=kind, settings=s)
        except Exception as e:
            events.log("engine_error", stage="proposals", error=str(e)[:200])
            answer = None
        if answer:
            return direct(answer, "proposal")
    try:
        pre = approval_preflight(chat, body, prev) if owner_text != "" else None  # a caption can answer an approval
    except Exception as e:
        events.log("engine_error", stage="approvals", error=str(e)[:200])
        pre = None
    if pre and pre[0] == "reply":
        return direct(pre[1], "approval")
    return run_turn(chat, body, d, platform=platform, origin=origin, voice=voice, t0=t0,
                    note=pre[1] if pre else "", settings=s, session=session, local=local, address=address,
                    turn_id=turn_id, typing=keeper, attempt=attempt, voice_info=vinfo,
                    attachments=attachments, owner_text=owner_text)


def transcribe_note(path, settings):
    """(transcript, voice info) for a voice note's audio file: the engine's own speech-to-text (waggle/stt.py),
    with the known mishearings fixed and the fillers and false starts taken out (stt.clean_transcript). The info
    records where the words came from, how long it took, and both the raw and the cleaned transcript."""
    res = stt.transcribe(path)
    info = {"voice": True, "transcript_source": res.get("source") or stt.SOURCE, "audio_path": str(path),
            "stt_s": res.get("seconds"),
            "stt_load_s": res.get("load_s"), "input_audio_s": res.get("audio_s"), "stt_ok": bool(res.get("ok"))}
    if not res.get("ok"):
        info["stt_error"] = str(res.get("error") or "")[:200]
        events.log("engine_stt_failed", error=info["stt_error"])
        return "", info
    conf = res.get("confidence") or {}
    info.update(stt_confident=bool(conf.get("confident")), stt_avg_logprob=conf.get("avg_logprob"),
                stt_no_speech_prob=conf.get("no_speech_prob"), stt_compression_ratio=conf.get("compression_ratio"),
                stt_segments=conf.get("segments"), stt_confident_why=list(conf.get("why") or []))
    cleaned = stt.clean_transcript(voice_turn.fix(res["text"], getattr(settings, "voice_fixes", None)))
    info.update(transcript_raw=res["text"], transcript_cleaned=cleaned)  # the model reads the cleaned words; both are kept
    return cleaned, info


def _audio_path(media):
    for m in (media if isinstance(media, (list, tuple)) else [media]):
        path = (m.get("path") or m.get("file")) if isinstance(m, dict) else m
        if path and str(path).strip():
            return str(path)
    return None


def on_inbound(msg):
    """The core's inbound handler (waggle.core.inbound.register): one message dict from the core, answered in
    the chat through Waggle's own Bot API. Returns a short summary for the core's record."""
    platform = msg.get("platform") or "telegram"
    origin = msg.get("origin") or "user"
    if origin not in ORIGINS:
        origin = "test"  # an unknown origin never counts as the user's own words
    marked, stripped = test_marked(msg.get("text") or "")
    if marked:
        origin, msg = marked_origin(origin), {**msg, "text": stripped}
    address = str(msg.get("address") or "")
    chat = msg.get("chat") or (f"{platform}:{address}" if address else "")
    if not chat:
        raise ValueError("inbound message without chat or address")
    address = address or address_for(chat, platform)
    kind, text, media, audio = msg.get("kind") or "text", msg.get("text") or "", msg.get("media"), None
    if kind == "voice" and not text.strip():
        audio, media = _audio_path(media), None  # the note's file is transcribed in the turn, not shown to the brain
    rec = handle_inbound(chat, text, kind=kind, media=media, audio=audio, local=local_answer,
                         platform=platform, origin=origin, delivery=delivery_for(chat, platform, address),
                         t0=msg.get("received_ts"), turn_id=msg.get("turn_id"), attempt=int(msg.get("attempt") or 1),
                         address=address)
    return {"turn_id": rec.turn_id, "outcome": rec.outcome, "delivered": rec.delivered, "trace_id": rec.trace_id,
            "outbox": rec.outbox_id}


# ---- which chats the engine serves -----------------------------------------------------------------------

def engine_chats(settings=None):
    """Telegram chat ids (strings) whose typed messages the gateway hands to the core (settings.engine_chats)."""
    s = settings or Settings.load()
    return {str(c).strip() for c in (getattr(s, "engine_chats", None) or []) if str(c).strip()}


def set_engine_chat(chat_id, on=True):
    """Switch the engine on or off for one Telegram chat id (config.json engine_chats). Returns the new set."""
    s = Settings.load()
    chats = [str(c) for c in (s.engine_chats or []) if str(c) != str(chat_id)]
    if on:
        chats.append(str(chat_id))
    s.engine_chats = chats
    s.save()
    return set(chats)


def engine_chat_keys(settings=None, platform="telegram"):
    """The chat keys of the engine chats ("telegram:<id>"): they exist before the user's first message, so they
    do not need a row in the chats table."""
    return {f"{platform}:{c}" for c in engine_chats(settings)}


def prewarm(now=None, within_s=86400, limit=3, settings=None):
    """Start the Claude process of every engine chat (settings.engine_chats: the owner's chat, even when it has
    no row or has not written yet) and of the other chats the user wrote in within `within_s`, up to `limit`
    of those (resuming their sessions), so the first message of the day does not pay the cold start.
    Returns {chat: result}."""
    s = settings or Settings.load()
    now = time.time() if now is None else now
    out = {}
    for c in sorted(engine_chats(s)):
        chat = f"telegram:{c}"
        try:
            chat_store.touch_chat(chat, "telegram", address=c)  # the row the sender resolves the chat through
        except Exception as e:
            events.log("engine_error", stage="prewarm_touch", error=str(e)[:200])
        out[chat] = pool(s).get(chat).warm()
    rows = [r for r in chat_store.chats_with_address()
            if r["chat"] not in out and (r.get("last_user") or 0) >= now - within_s]
    rows.sort(key=lambda r: -(r.get("last_user") or 0))
    for r in rows[:limit]:
        out[r["chat"]] = pool(s).get(r["chat"]).warm()
    events.log("engine_prewarm", chats=list(out), results=out)
    return out


def main(argv=None):
    """python -m waggle.chat_engine engine on|off|status [<telegram chat id>]"""
    import sys
    args = sys.argv[1:] if argv is None else argv
    if len(args) >= 2 and args[0] == "engine" and args[1] in ("on", "off") and len(args) == 3:
        chats = set_engine_chat(args[2], on=args[1] == "on")
        print(f"engine {'on' if args[1] == 'on' else 'off'} for {args[2]}; engine chats now: {sorted(chats) or 'none'}")
        return 0
    if args[:2] == ["engine", "status"]:
        print(f"engine chats: {sorted(engine_chats()) or 'none'}")
        return 0
    print("usage: python -m waggle.chat_engine engine on|off <telegram chat id> | engine status")
    return 64


def register_core():
    """Register the engine's job kinds with the core (waggle.core.jobs). Returns their names."""
    from . import chat_jobs
    return chat_jobs.register_core()


if __name__ == "__main__":
    raise SystemExit(main())
