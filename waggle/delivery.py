"""How a reply reaches the user, independent of any chat platform.

The turn engine (waggle/chat_engine.py) only ever talks to a `Delivery`. A transport implements five calls:

    post(text)              -> handle or None      new message; the handle identifies it for edits
    edit(handle, text)      -> bool                replace that message's text in place
    send_file(path, caption="") -> bool            a document / image
    send_voice(path, caption="") -> bool           a voice note (.ogg/Opus)
    typing()                                        "typing..." indicator, best effort

plus two attributes: `can_edit` (False = edits are not shown in place, so stream nothing and post the final
text once) and `max_len` (characters per message). The Hermes plugin implements it with post_message/edit
(hermes_plugin/waggle/__init__.py); BotApiDelivery below implements it over Waggle's own Telegram client
(waggle.core.telegram.BotApi).

StreamWriter turns a growing reply into a few edits: the first words posted at once, then at most one edit per
`interval` seconds (Telegram allows about one per second per chat and silently skips faster interim edits); text
that has already arrived is never held back, so the final text is sent immediately; while tools run and nothing has
been written yet it shows a one-line status derived from the tool call (tool_status), replaced by the answer in
the same message. Long replies continue in a new message.
plain_text() makes Markdown/LaTeX safe for chat apps that render neither reliably.
"""
import contextvars
import json
import re
import threading
import time


class Delivery:
    """Interface. Subclasses override what their transport supports."""
    can_edit = True
    max_len = 4000
    name = "delivery"

    def post(self, text):
        raise NotImplementedError

    def edit(self, handle, text):
        return False

    def send_file(self, path, caption=""):
        return False

    def send_voice(self, path, caption=""):
        return False

    def typing(self):
        return None

    def message_id(self, handle):
        """The transport's id for the message behind `handle` (for the trace), or None."""
        if isinstance(handle, (int, str)) and not isinstance(handle, bool):
            return handle
        return getattr(handle, "message_id", None)


class CollectDelivery(Delivery):
    """Keeps the reply in memory instead of sending it: for surfaces where the host delivers the final text
    itself (desktop CLI, voice), so the engine's answer is returned to the caller."""
    can_edit = True
    max_len = 1_000_000
    name = "collect"

    def __init__(self):
        self.messages = []

    def post(self, text):
        self.messages.append(text)
        return len(self.messages) - 1

    def edit(self, handle, text):
        self.messages[handle] = text
        return True

    @property
    def text(self):
        return "\n\n".join(self.messages)


# ---- plain text for chat apps --------------------------------------------------------------------------------

_LATEX_SYMBOLS = {
    r"\times": "×", r"\cdot": "·", r"\div": "÷", r"\pm": "±", r"\approx": "≈", r"\neq": "≠", r"\ne": "≠",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\infty": "∞", r"\pi": "π", r"\theta": "θ",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\Delta": "Δ", r"\omega": "ω", r"\mu": "μ",
    r"\sigma": "σ", r"\lambda": "λ", r"\tau": "τ", r"\phi": "φ", r"\rightarrow": "→", r"\to": "→",
    r"\Rightarrow": "⇒", r"\leftarrow": "←", r"\degree": "°", r"\circ": "°", r"\sum": "Σ", r"\int": "∫",
    r"\partial": "∂", r"\sqrt": "√", r"\quad": " ", r"\qquad": " ", r"\,": " ", r"\;": " ", r"\!": "",
    r"\left": "", r"\right": "", r"\displaystyle": "",
}
_SYM_RE = re.compile("|".join(re.escape(k) + (r"(?![A-Za-z])" if k[-1].isalpha() else "")
                              for k in sorted(_LATEX_SYMBOLS, key=len, reverse=True)))


def _latex(expr):
    s = expr
    for _ in range(4):  # nested fractions / roots
        s = re.sub(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", lambda m: f"{_wrap(m.group(1))}/{_wrap(m.group(2))}", s)
        s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", lambda m: f"√{_wrap(m.group(1))}", s)
        s = re.sub(r"\\(?:text|mathrm|mathbf|mathit|operatorname|textbf)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: "^" + _wrap(m.group(1)), s)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: "_" + _wrap(m.group(1)), s)
    s = _SYM_RE.sub(lambda m: _LATEX_SYMBOLS[m.group(0)], s)
    s = re.sub(r"\\([A-Za-z]+)", r"\1", s)  # unknown command: keep its name
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def _wrap(x):
    x = x.strip()
    return x if re.fullmatch(r"[\w.√π]+", x) else f"({x})"


def _table_row(line):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return " · ".join(c for c in cells if c)


def plain_text(text):
    """Markdown + LaTeX -> text a chat app shows correctly: no $$, no headings, no tables, no ** markers.
    Bullets become "• ", links "label (url)", code keeps its content."""
    if not text:
        return ""
    s = str(text).replace("\r\n", "\n")
    s = re.sub(r"\$\$(.+?)\$\$", lambda m: _latex(m.group(1)), s, flags=re.S)
    s = re.sub(r"\\\[(.+?)\\\]", lambda m: _latex(m.group(1)), s, flags=re.S)
    s = re.sub(r"\\\((.+?)\\\)", lambda m: _latex(m.group(1)), s, flags=re.S)
    # inline $...$ only when it really is math (a backslash, ^ or _ inside): "$5 and $10" stays money
    s = re.sub(r"(?<![\w$])\$([^$\n]{1,200}?[\\^_][^$\n]*?)\$(?![\w$])", lambda m: _latex(m.group(1)), s)
    out, in_code = [], False
    for line in s.split("\n"):
        if re.match(r"^\s*```", line):
            in_code = not in_code
            continue
        if in_code:
            out.append(line)
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", line):
            continue  # table separator row
        if line.strip().startswith("|") and line.count("|") >= 2:
            line = _table_row(line)
        elif re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", line):
            continue  # horizontal rule
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
        line = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), line)
        line = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", line)
        line = re.sub(r"`([^`\n]+)`", r"\1", line)
        line = re.sub(r"!?\[([^\]\n]+)\]\((\S+?)\)", r"\1 (\2)", line)
        out.append(line)
    s = "\n".join(out)
    s = s.replace("**", "").replace("```", "")
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# ---- streaming into edits --------------------------------------------------------------------------------------

def split_long(text, limit):
    """Chunks of at most `limit` chars, cut at a paragraph, line or sentence end when possible."""
    chunks, rest = [], text
    while len(rest) > limit:
        cut = max(rest.rfind("\n\n", 0, limit), rest.rfind("\n", 0, limit))
        if cut < limit // 2:
            cut = max(rest.rfind(". ", 0, limit) + 1, rest.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    chunks.append(rest)
    return chunks


def _tool_command(step):
    raw = str(step.get("input") or "")
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return str(data.get("command") or "")
    except ValueError:
        pass
    m = re.search(r'"command":\s*"((?:[^"\\]|\\.)*)', raw)
    return m.group(1) if m else ""


def _tool_kind(step):
    """The kind of work one tool call is, by a fixed rule: shell | github | read | files | web | page | edit |
    mail | calendar | drive | notes | background | other."""
    name = str(step.get("tool") or "")
    low = name.lower()
    if low in ("bash", "powershell"):
        cmd = _tool_command(step)
        return "github" if re.search(r"(^|[;&|(]\s*)gh(\.exe)?\s", cmd.strip() + " ") else "shell"
    simple = {"read": "read", "glob": "files", "grep": "files", "websearch": "web", "webfetch": "page",
              "write": "edit", "edit": "edit", "multiedit": "edit", "notebookedit": "edit"}
    if low in simple:
        return simple[low]
    if low.startswith("mcp__"):
        if "gmail" in low:
            return "mail"
        if "calendar" in low or "gcal" in low:
            return "calendar"
        if "drive" in low or "docs" in low or "sheets" in low:
            return "drive"
        if "background_task" in low or "start_build" in low:
            return "background"
        if any(w in low for w in ("recall", "vault", "memory", "remember", "search_notes")):
            return "notes"
    return "other"


_STATUS = {"shell": ("Running a command…", "Running {n} commands…"), "github": ("Checking GitHub…", None),
           "read": ("Reading a file…", "Reading {n} files…"), "files": ("Searching files…", None),
           "web": ("Searching the web…", None), "page": ("Reading a web page…", "Reading {n} web pages…"),
           "edit": ("Editing a file…", "Editing {n} files…"), "mail": ("Checking your email…", None),
           "calendar": ("Checking your calendar…", None), "drive": ("Checking your files…", None),
           "notes": ("Checking my notes…", None), "background": ("Starting a background job…", None),
           "other": ("Working on it…", None)}


def tool_status(steps):
    """One short plain-words status line for the tool calls of a turn so far, from the latest call and how many
    calls of the same kind there have been ("Reading 2 files…", "Checking GitHub…"). No model is involved."""
    if not steps:
        return ""
    kind = _tool_kind(steps[-1])
    n = sum(1 for s in steps if _tool_kind(s) == kind)
    one, many = _STATUS[kind]
    return many.format(n=n) if many and n > 1 else one


class StreamWriter:
    """Shows a reply while it is written. update(full_text) any number of times, then finish(final_text).

    - Text is shown as it arrives: the first words are posted at once (after a hold of at most first_hold_s so a
      one-line answer goes out as one message), then the message is edited at most once per `interval` seconds.
    - Text that has already arrived is never held back: finish() sends the complete text immediately (an edit of
      the message on screen, or one post), without waiting out the interval.
    - tool(step) shows progress while tools run and no words have been written yet: one short status line is
      posted at the first tool call and edited (at most once per `interval`) as the work moves on; when the answer
      arrives it replaces the status in the same message. Words the model wrote before a tool call are shown
      instead of the status.
    finish() returns {"delivered", "messages", "error", "edits", "first_shown_at" (first real words on screen),
    "first_visible_at" (first post of any kind, a status line included), "message_ids"}."""

    def __init__(self, delivery, interval=1.2, stream=True, clean=plain_text, clock=time.time, sleep=time.sleep,
                 tick=0.2, on_first_shown=None, first_hold_s=0.4, first_min_chars=80, first_settle_s=0.012):
        self.d, self.interval, self.clean, self.clock, self.sleep, self.tick = delivery, interval, clean, clock, sleep, tick
        self.on_first_shown = on_first_shown  # called once, right after the first post (status line or words)
        # The first words wait until the text has first_min_chars or first_hold_s passed since they began: a
        # one-line answer that completes inside the hold goes out once, whole, with no edit.
        self.first_hold_s, self.first_min_chars, self._first_update = first_hold_s, first_min_chars, None
        self.first_settle_s, self._last_update = first_settle_s, 0.0
        self.stream = stream and getattr(delivery, "can_edit", True)
        self.limit = max(200, int(getattr(delivery, "max_len", 4000) or 4000) - 96)
        self.handles, self.shown = [], []  # one per message: its handle and the text it shows now
        self._latest, self._last_push, self._lock = "", 0.0, threading.Lock()
        self._stop, self._wake, self._thread = threading.Event(), threading.Event(), None
        self.edits, self.first_shown_at, self.first_visible_at, self.error = 0, None, None, ""
        self._tools, self._status, self._status_on_screen, self._real_shown = [], "", "", False

    # -- feeding
    def _ensure_thread(self):
        if self.stream and self._thread is None:
            ctx = contextvars.copy_context()  # a transport may read the caller's context (e.g. which chat)
            self._thread = threading.Thread(target=ctx.run, args=(self._loop,), daemon=True, name="waggle-stream")
            self._thread.start()

    def update(self, text):
        with self._lock:
            self._latest = text or ""
            self._last_update = self.clock()
            if self._first_update is None and self._latest.strip():
                self._first_update = self.clock()
        self._ensure_thread()
        if not self._real_shown:
            self._wake.set()  # re-check the first words' hold now, not at the next tick

    def tool(self, step):
        """A tool call started (pass as the session's on_tool)."""
        if not self.stream:
            return
        with self._lock:
            self._tools.append(step)
            self._status = tool_status(self._tools)
        self._ensure_thread()
        self._wake.set()

    def _shown_text(self):
        return "\n\n".join(self.shown)

    def _hold_left(self, latest, first):
        """Seconds the first words still wait (0 when they may go out): until they have first_min_chars and have
        stopped growing for first_settle_s (a burst the CLI hands over at once is posted whole, not in two parts),
        but never longer than first_hold_s from the first words."""
        if self._real_shown or first is None:
            return 0.0
        now = self.clock()
        cap = self.first_hold_s - (now - first)
        if cap <= 0:
            return 0.0
        if len(latest) < self.first_min_chars:
            return cap
        return min(cap, max(0.0, self.first_settle_s - (now - self._last_update)))

    def _loop(self):
        while not self._stop.is_set():
            wait = self.tick
            since = self.clock() - self._last_push
            with self._lock:
                latest, first, status = self._latest, self._first_update, self._status
            if latest.strip():
                hold = self._hold_left(latest, first)
                if hold > 0:
                    wait = min(wait, hold)
                elif since >= self.interval or not self.handles:
                    cleaned = self.clean(latest)
                    if cleaned != self._shown_text():
                        self._push_view(cleaned, final=False)
                else:
                    wait = min(wait, max(0.005, self.interval - since))
            elif status and status != self._status_on_screen:
                if since >= self.interval or not self.handles:
                    self._push_status(status)
                else:
                    wait = min(wait, max(0.005, self.interval - since))
            self._wake.wait(wait)
            self._wake.clear()

    # -- sending
    def _visible(self):
        if self.first_visible_at is None:
            self.first_visible_at = self.clock()
            if self.on_first_shown:
                try:
                    self.on_first_shown()
                except Exception:
                    pass

    def _push_status(self, status):
        if not self.handles:
            h = self._safe(self.d.post, status)
            if h is None or h is False:
                return False
            self.handles.append(h)
            self.shown.append(status)
            self._visible()
        else:
            if not self._safe(self.d.edit, self.handles[0], status):
                return False
            self.edits += 1
            self.shown[0] = status
        self._status_on_screen = status
        self._last_push = self.clock()
        return True

    def _push(self, text, final):
        return self._push_view(self.clean(text), final)

    def _push_view(self, cleaned, final):
        chunks = split_long(cleaned, self.limit)
        ok = True
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            if i < len(self.shown) and self.shown[i] == chunk:
                continue
            first_post = self.first_visible_at is None  # nothing was on screen before this push
            if i < len(self.handles) and self.handles[i] is not None:
                done = self._safe(self.d.edit, self.handles[i], chunk)
                self.edits += 1
                if not done and final:  # the edit did not land: the final text must, as a new message
                    h = self._safe(self.d.post, chunk)
                    done = h is not None and h is not False
                    if done:
                        self.handles[i] = h
                if done:
                    self.shown[i] = chunk
                ok = ok and bool(done)
            else:
                h = self._safe(self.d.post, chunk)
                if h is None or h is False:
                    ok = False
                    break
                self.handles.append(h)
                self.shown.append(chunk)
                self._visible()
            if ok and not self._real_shown:
                self._real_shown = True
                self.first_shown_at = self.first_visible_at if first_post else self.clock()
            self._last_push = self.clock()
        return ok

    def _safe(self, fn, *a):
        try:
            return fn(*a)
        except Exception as e:  # a transport error is a failed delivery, never a crashed turn
            self.error = f"{type(e).__name__}: {e}"[:300]
            return None

    def finish(self, final_text):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=35)
        final = final_text or ""
        delivered = self._push_view(self.clean(final), final=True) if final.strip() else False
        if not delivered and not self.error:
            self.error = "the message could not be posted or updated"
        ids = []
        for h in self.handles:
            try:
                mid = self.d.message_id(h) if hasattr(self.d, "message_id") else Delivery.message_id(self.d, h)
            except Exception:
                mid = None
            if mid is not None:
                ids.append(mid)
        return {"delivered": delivered, "messages": len(self.handles), "error": "" if delivered else self.error,
                "edits": self.edits, "first_shown_at": self.first_shown_at, "first_visible_at": self.first_visible_at,
                "message_ids": ids}


class Typing:
    """Keeps the "typing..." indicator alive until the first words are posted: the first call at once, then one
    every `every` seconds (Telegram shows it for about 5 s), until stop(). Best effort: errors are ignored."""

    def __init__(self, delivery, every=4.0, limit_s=600.0):
        self.d, self.every, self.limit_s = delivery, every, limit_s
        self._stop, self._thread, self.sent = threading.Event(), None, 0

    def start(self):
        if self._thread is None and not self._stop.is_set():
            ctx = contextvars.copy_context()
            self._thread = threading.Thread(target=ctx.run, args=(self._loop,), daemon=True, name="waggle-typing")
            self._thread.start()
        return self

    def _loop(self):
        t0 = time.monotonic()
        while not self._stop.is_set() and time.monotonic() - t0 < self.limit_s:
            try:
                self.d.typing()
                self.sent += 1
            except Exception:
                pass
            self._stop.wait(self.every)

    def stop(self):
        self._stop.set()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()


# ---- Telegram through Waggle's own Bot API client ------------------------------------------------------------

class BotApiDelivery(Delivery):
    """Delivery over waggle.core.telegram.BotApi (imported only when no `api` is given), for one Telegram chat.

    - post(text) -> the id of the new message (the last part, if the client split it); edit(id, text) -> bool.
    - At least `min_edit_gap` seconds (1.0) between edits of one message: an edit that comes sooner waits.
    - A retryable or rate-limited failure waits once (its retry_after, capped at MAX_WAIT_S) and tries again;
      anything else raises (StreamWriter records it as a failed delivery; a failed final edit becomes a new
      message there, so the final text always lands). "Message is not modified" counts as done.
    - max_len 4096 (Telegram's limit): StreamWriter continues a longer reply in a new message.
    The first message of a turn can reply to the user's message (`reply_to`)."""
    can_edit = True
    max_len = 4096
    name = "telegram"
    MAX_WAIT_S = 30.0

    def __init__(self, chat_id, api=None, *, reply_to=None, min_edit_gap=1.0, clock=time.monotonic,
                 sleep=time.sleep):
        self.chat_id, self._api, self.reply_to = chat_id, api, reply_to
        self.min_edit_gap, self.clock, self.sleep = float(min_edit_gap), clock, sleep
        self._touched = {}  # message id -> clock of its post or last edit
        self.message_ids, self.edits, self.last_error = [], 0, ""

    @property
    def api(self):
        if self._api is None:
            import importlib
            telegram = importlib.import_module("waggle.core.telegram")  # lazy: only when this transport is used
            make = getattr(telegram, "default_api", None)
            self._api = make() if make else telegram.BotApi()  # one shared keep-alive client per process
        return self._api

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            kind = getattr(e, "kind", None)
            if kind in ("retryable", "rate_limited") and not getattr(e, "permanent", False):
                self.last_error = f"{kind}: {e}"[:300]
                self.sleep(min(max(float(getattr(e, "retry_after", None) or 1.0), 0.0), self.MAX_WAIT_S))
                return fn(*args, **kwargs)
            self.last_error = f"{kind or type(e).__name__}: {e}"[:300]
            raise

    def post(self, text):
        kwargs = {"reply_to": self.reply_to} if self.reply_to and not self.message_ids else {}
        ids = [i for i in (self._call(self.api.send_message, self.chat_id, text, **kwargs) or []) if i is not None]
        if not ids:
            return None
        now = self.clock()
        for i in ids:
            self._touched[i] = now
        self.message_ids += ids
        return ids[-1]

    def edit(self, handle, text):
        last = self._touched.get(handle)
        if last is not None:
            wait = self.min_edit_gap - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        try:
            ok = self._call(self.api.edit_message_text, self.chat_id, handle, text)
        except Exception as e:
            if "not modified" in str(e).lower():
                ok = True
            elif getattr(e, "kind", None) == "permanent" or getattr(e, "permanent", False):
                return False  # e.g. the message was deleted: the writer posts the final text anew
            else:
                raise
        self._touched[handle] = self.clock()
        self.edits += 1
        return bool(ok)

    def send_file(self, path, caption=""):
        return self._call(self.api.send_document, self.chat_id, str(path), caption=caption or None)

    def send_voice(self, path, caption="", duration=None):
        return self._call(self.api.send_voice, self.chat_id, str(path), caption=caption or None, duration=duration)

    def typing(self):
        try:
            return self.api.send_chat_action(self.chat_id, "typing")
        except Exception:
            return None  # best effort

    def message_id(self, handle):
        return handle
