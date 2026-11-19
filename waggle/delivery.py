"""How a reply reaches the user, independent of any chat platform.

The turn engine (waggle/chat_engine.py) only ever talks to a `Delivery`. A transport implements five calls:

    post(text)              -> handle or None      new message; the handle identifies it for edits
    edit(handle, text)      -> bool                replace that message's text in place
    send_file(path, caption="") -> bool            a document / image
    send_voice(path, caption="") -> bool           a voice note (.ogg/Opus)
    typing()                                        "typing..." indicator, best effort

plus two attributes: `can_edit` (False = edits are not shown in place, so stream nothing and post the final
text once) and `max_len` (characters per message). The Hermes plugin implements it with post_message/edit
(hermes_plugin/waggle/__init__.py); a direct Telegram Bot API transport would implement the same five calls.

StreamWriter turns a growing reply into a few edits: at most one per `interval` seconds (Telegram allows about
one per second per chat and silently skips faster interim edits), the final text always sent (it waits out the
interval first, so the last edit is never the one that gets skipped), long replies continue in a new message.
plain_text() makes Markdown/LaTeX safe for chat apps that render neither reliably.
"""
import contextvars
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


class StreamWriter:
    """Shows a reply while it is written. update(full_text) any number of times, then finish(final_text).

    A background thread pushes at most one change per `interval` seconds; finish() stops it, waits out the
    interval and sends the final text (edit, or a new message if the edit failed). Returns from finish():
    {"delivered", "messages", "error", "edits", "first_shown_at"}."""

    def __init__(self, delivery, interval=1.2, stream=True, clean=plain_text, clock=time.time, sleep=time.sleep,
                 tick=0.2):
        self.d, self.interval, self.clean, self.clock, self.sleep, self.tick = delivery, interval, clean, clock, sleep, tick
        self.stream = stream and getattr(delivery, "can_edit", True)
        self.limit = max(200, int(getattr(delivery, "max_len", 4000) or 4000) - 96)
        self.handles, self.shown = [], []  # one per message: its handle and the text it shows now
        self._latest, self._last_push, self._lock = "", 0.0, threading.Lock()
        self._stop, self._thread = threading.Event(), None
        self.edits, self.first_shown_at, self.error = 0, None, ""

    # -- feeding
    def update(self, text):
        with self._lock:
            self._latest = text or ""
        if self.stream and self._thread is None:
            ctx = contextvars.copy_context()  # a transport may read the caller's context (e.g. which chat)
            self._thread = threading.Thread(target=ctx.run, args=(self._loop,), daemon=True, name="waggle-stream")
            self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            if self.clock() - self._last_push >= self.interval:
                with self._lock:
                    latest = self._latest
                if latest and self.clean(latest) != "\n\n".join(self.shown):
                    self._push(latest, final=False)
            self._stop.wait(self.tick)

    # -- sending
    def _push(self, text, final):
        chunks = split_long(self.clean(text), self.limit)
        ok = True
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            if i < len(self.shown) and self.shown[i] == chunk:
                continue
            if i < len(self.handles) and self.handles[i] is not None:
                if final and self.clock() - self._last_push < self.interval:
                    self.sleep(self.interval - (self.clock() - self._last_push))
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
                if self.first_shown_at is None:
                    self.first_shown_at = self.clock()
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
        if self._thread is not None:
            self._thread.join(timeout=35)
        final = final_text or ""
        delivered = self._push(final, final=True) if final.strip() else False
        if not delivered and not self.error:
            self.error = "the message could not be posted or updated"
        return {"delivered": delivered, "messages": len(self.handles), "error": "" if delivered else self.error,
                "edits": self.edits, "first_shown_at": self.first_shown_at}
