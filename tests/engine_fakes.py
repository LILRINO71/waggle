"""Fakes for the turn engine tests: a `claude` process that speaks stream-json, deliveries, a Telegram client.

FakeClaude is a Popen replacement: each call starts a FakeProcess whose stdin takes the stream-json user
messages ChatSession writes and whose stdout yields the events a script pushes in reply. A script is
`script(proc, text)`; it may emit events at once or from a thread (to stream with delays), or kill the process.
"""
import json
import queue
import threading
import time


# ---- stream-json events ---------------------------------------------------------------------------------------

MODEL = "claude-haiku-5-5"


def ev_init(sid, model=MODEL):
    return {"type": "system", "subtype": "init", "session_id": sid, "model": model, "tools": []}


def ev_text(sid, text, msg_id="msg_1", pieces=3):
    """A streamed text message: message_start, block start, deltas, block stop, then the whole assistant message."""
    n = max(1, min(pieces, len(text)))
    size = -(-len(text) // n)
    chunks = [text[i:i + size] for i in range(0, len(text), size)]
    out = [{"type": "stream_event", "session_id": sid, "parent_tool_use_id": None,
            "event": {"type": "message_start", "message": {"id": msg_id, "model": MODEL}}},
           {"type": "stream_event", "session_id": sid, "parent_tool_use_id": None,
            "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}}]
    out += [{"type": "stream_event", "session_id": sid, "parent_tool_use_id": None,
             "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": c}}}
            for c in chunks]
    out += [{"type": "stream_event", "session_id": sid, "parent_tool_use_id": None,
             "event": {"type": "content_block_stop", "index": 0}},
            {"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
             "message": {"id": msg_id, "role": "assistant", "content": [{"type": "text", "text": text}]}}]
    return out


def ev_tool(sid, name, tool_input, result, is_error=False, tool_id="toolu_1", msg_id="msg_t"):
    return [{"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
             "message": {"id": msg_id, "role": "assistant",
                         "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}},
            {"type": "user", "session_id": sid, "parent_tool_use_id": None,
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                      "content": result, "is_error": is_error}]}}]


def ev_result(sid, text="", model=MODEL, is_error=False, subtype="success", **extra):
    return {"type": "result", "subtype": subtype, "is_error": is_error, "result": text, "session_id": sid,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "modelUsage": {model: {"inputTokens": 10, "outputTokens": 5}}, **extra}


# ---- the fake process ---------------------------------------------------------------------------------------

class _Pipe:
    def __init__(self):
        self.q = queue.Queue()

    def put(self, line):
        self.q.put(line)

    def eof(self):
        self.q.put(None)

    def __iter__(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            yield item


class _Stdin:
    def __init__(self, proc):
        self.proc, self.closed, self.buf = proc, False, ""

    def write(self, data):
        if self.closed or self.proc.returncode is not None:
            raise OSError("broken pipe")
        self.buf += data
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.proc.on_input(line)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, claude, cmd, kwargs):
        self.claude, self.cmd, self.kwargs = claude, list(cmd), kwargs
        self.stdout, self.stderr, self.stdin = _Pipe(), _Pipe(), _Stdin(self)
        self.returncode, self.inputs, self._lock = None, [], threading.Lock()
        self.resume = self.arg("--resume")
        self.session_id = self.resume or self.arg("--session-id") or "sess-default"
        if self.resume and self.resume not in claude.known_sessions:
            self.die(1, f"No conversation found with session ID: {self.resume}")
            return
        claude.known_sessions.add(self.session_id)

    def arg(self, flag):
        return self.cmd[self.cmd.index(flag) + 1] if flag in self.cmd else ""

    def on_input(self, line):
        msg = json.loads(line)
        text = msg["message"]["content"][0]["text"]
        self.inputs.append(text)
        self.claude.script(self, text)

    def emit(self, *events):
        for ev in events:
            if isinstance(ev, list):
                self.emit(*ev)
            elif self.returncode is None:
                self.stdout.put((ev if isinstance(ev, str) else json.dumps(ev)) + "\n")

    def answer(self, text, pieces=3, delay=0.0, model=MODEL):
        """Stream `text` as one message and finish the turn (in a thread when delay > 0)."""
        def go():
            self.emit(ev_init(self.session_id, model))
            events = ev_text(self.session_id, text, msg_id=f"msg_{len(self.inputs)}", pieces=pieces)
            for ev in events:
                if delay and ev["type"] == "stream_event" and ev["event"]["type"] == "content_block_delta":
                    time.sleep(delay)
                self.emit(ev)
            self.emit(ev_result(self.session_id, text, model=model))
        if delay:
            threading.Thread(target=go, daemon=True).start()
        else:
            go()

    def die(self, code=1, stderr=""):
        with self._lock:
            if self.returncode is not None:
                return
            self.returncode = code
        if stderr:
            self.stderr.put(stderr + "\n")
        self.stdout.eof()
        self.stderr.eof()

    def poll(self):
        return self.returncode

    def kill(self):
        self.die(-9)

    def wait(self, timeout=None):
        return self.returncode


class FakeClaude:
    """Popen replacement. `script(proc, text)` answers each user message (default: a fixed reply)."""

    def __init__(self, script=None):
        self.procs, self.known_sessions = [], set()
        self.script = script or (lambda proc, text: proc.answer("Hello from Claude."))

    def __call__(self, cmd, **kwargs):
        p = FakeProcess(self, cmd, kwargs)
        self.procs.append(p)
        return p


# ---- deliveries -------------------------------------------------------------------------------------------

class FakeDelivery:
    """Records every post and edit with its time; can be told to fail."""
    can_edit = True
    max_len = 4000
    name = "fake"

    def __init__(self, fail_post=False, fail_edit=False, max_len=None, can_edit=True):
        self.messages, self.log, self.fail_post, self.fail_edit = [], [], fail_post, fail_edit
        self.can_edit = can_edit
        if max_len:
            self.max_len = max_len
        self.typed = 0

    def post(self, text):
        self.log.append(("post", time.monotonic(), text))
        if self.fail_post:
            return None
        self.messages.append(text)
        return 100 + len(self.messages) - 1

    def edit(self, handle, text):
        self.log.append(("edit", time.monotonic(), text))
        if self.fail_edit:
            return False
        self.messages[handle - 100] = text
        return True

    def typing(self):
        self.typed += 1

    def message_id(self, handle):
        return handle


class TelegramError(Exception):
    def __init__(self, message, kind="retryable", retry_after=None):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after
        self.permanent = kind in ("permanent", "auth", "conflict")


class FakeBotApi:
    """Exactly the interface of waggle.core.telegram.BotApi that BotApiDelivery uses."""

    def __init__(self, clock=None):
        self.calls, self.messages, self.next_id = [], {}, 500
        self.clock = clock or time.monotonic
        self.fail = {}  # method name -> list of exceptions to raise on the next calls

    def _maybe_fail(self, name):
        errs = self.fail.get(name)
        if errs:
            raise errs.pop(0)

    def send_message(self, chat_id, text, *, parse_mode=None, reply_to=None):
        self.calls.append(("send_message", self.clock(), chat_id, text, reply_to))
        self._maybe_fail("send_message")
        ids = []
        for i in range(0, max(len(text), 1), 4096):
            self.next_id += 1
            self.messages[self.next_id] = text[i:i + 4096]
            ids.append(self.next_id)
        return ids

    def edit_message_text(self, chat_id, message_id, text):
        self.calls.append(("edit_message_text", self.clock(), chat_id, message_id, text))
        self._maybe_fail("edit_message_text")
        if len(text) > 4096:
            raise TelegramError("message is too long", "permanent")
        self.messages[message_id] = text
        return True

    def send_chat_action(self, chat_id, action="typing"):
        self.calls.append(("send_chat_action", self.clock(), chat_id, action))
        self._maybe_fail("send_chat_action")
        return True

    def send_voice(self, chat_id, path, *, caption=None, duration=None):
        self.calls.append(("send_voice", self.clock(), chat_id, path, caption, duration))
        self.next_id += 1
        return self.next_id

    def send_document(self, chat_id, path, *, caption=None):
        self.calls.append(("send_document", self.clock(), chat_id, path, caption))
        self.next_id += 1
        return self.next_id


class FakeClock:
    """A clock that only moves when someone sleeps."""

    def __init__(self, t=1000.0):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += max(s, 0)
