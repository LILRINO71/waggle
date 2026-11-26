"""What Waggle is doing right now, for the owner to watch (the Home page's Activity feed).

The chat engine (waggle/chat_engine.py run_turn) tells this module when a turn starts, each tool call it makes, the
words of the answer as they are written, and when it ends. The state is one small JSON file, <data>/live/turn.json,
because the page is served by another process (the dashboard) than the one that runs the turn (the core).

    begin(turn_id, chat, text, origin)   a turn started
    tool(step)                           a tool call started (the session's on_tool step: tool, input)
    words(full_text)                     the answer so far (written at most every WRITE_EVERY_S)
    end(model=, outcome=, reply=)        the turn is over
    read()                               the state for the page, or None

One turn at a time is shown: the latest. Nothing here can fail a turn (every write is best effort), and a tool's
input is cut to one short line (progress.brief), never shown whole.
"""
import json
import threading
import time

from . import progress
from .config import data_path

MAX_STEPS = 60
TEXT_TAIL = 2400
WRITE_EVERY_S = 0.35
STALE_S = 1800  # a turn that never ended (the core was killed) stops counting as running after this

_lock = threading.Lock()
_state = {}
_last_write = 0.0


def _file():
    return data_path("live") / "turn.json"


def _save(force=False, now=None):
    global _last_write
    now = now or time.time()
    if not force and now - _last_write < WRITE_EVERY_S:
        return
    _last_write = now
    try:
        f = _file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state), encoding="utf-8")
        tmp.replace(f)
    except OSError:
        pass  # watching is a nicety: it never breaks a turn


def begin(turn_id, chat, text, origin="user", now=None):
    now = now or time.time()
    with _lock:
        _state.clear()
        _state.update(turn=str(turn_id or ""), chat=str(chat or ""), origin=str(origin or ""), started=now,
                      updated=now, state="thinking", asked=" ".join(str(text or "").split())[:300], steps=[],
                      text="", model="", outcome="", finished=None)
        _save(force=True, now=now)


def _line(step):
    step = step if isinstance(step, dict) else {}
    inp = step.get("input")
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:  # the recorded input is cut to 400 characters, so long ones are not whole JSON
            inp = {"description": inp[:90]} if inp else {}
    return progress.brief(step.get("tool") or "tool", inp)


def tool(step, now=None):
    now = now or time.time()
    with _lock:
        if not _state or _state.get("finished"):
            return
        _state["steps"] = (_state["steps"] + [{"t": now, "line": _line(step)}])[-MAX_STEPS:]
        _state.update(state="working", updated=now)
        _save(force=True, now=now)


def words(full_text, now=None):
    now = now or time.time()
    with _lock:
        if not _state or _state.get("finished"):
            return
        _state.update(text=str(full_text or "")[-TEXT_TAIL:], state="writing", updated=now)
        _save(now=now)


def end(model="", outcome="", reply=None, now=None):
    now = now or time.time()
    with _lock:
        if not _state or _state.get("finished"):
            return
        if reply is not None:
            _state["text"] = str(reply)[-TEXT_TAIL:]
        _state.update(model=str(model or ""), outcome=str(outcome or ""), state="done", updated=now, finished=now)
        _save(force=True, now=now)


def read(now=None):
    """The latest turn as the page shows it, or None. `running` is False once it ended, or when it has been silent
    so long that its process must be gone."""
    try:
        st = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict) or not st.get("turn"):
        return None
    now = now or time.time()
    st["running"] = not st.get("finished") and now - (st.get("updated") or 0) < STALE_S
    st["seconds"] = round((st.get("finished") or now) - (st.get("started") or now), 1)
    return st
