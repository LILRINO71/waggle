"""The hands-free line: POST /voice on the core's own HTTP server (127.0.0.1 only).

The owner speaks into the phone and the answer plays aloud by itself, which a Telegram voice note cannot do. The
phone's Shortcut posts either the text it dictated (JSON {"text": "..."}) or the audio itself (Content-Type
audio/*, which Waggle transcribes with the same recogniser as a Telegram voice note). The request runs one normal
engine turn for the owner's chat (the same chat as the Telegram conversation, so there is one memory; origin
"user"; a voice turn), and the reply is the spoken part of the answer as audio the iPhone plays: M4A (AAC) made
with the ffmpeg under ~/.hermes/tools, a WAV when that is not possible.

    Authorization: Bearer <contents of voice.token>      its own secret; the core's admin token does not open this
    200  audio/mp4 (or audio/wav) body, header X-Waggle-Spoken = the spoken text, URL-encoded
         ?reply=json: {"spoken": ..., "text": ...} and no audio (and no speech is made)
    401 bad or missing token   413 body over 2 MB   415 other content type   400 bad body
    411 no Content-Length      429 another voice request is running     503 not ready / no owner chat

One request at a time. A turn that is not finished after HOLD_AFTER_S seconds (long tool work) is answered with a
short spoken line, and the turn carries on; its answer then goes to the owner's Telegram chat through the outbox.
With the setting on (default) the full written answer is also queued to Telegram when the answer is returned, so
there is a written record. Settings (config.json, all optional): voice_line_telegram_copy (true), voice_line_chat
(a chat key such as "telegram:123", else the engine chat), voice_line_hold_s (25).

The token is a file, voice.token, in the Waggle home: 32 random bytes, URL-safe, created by the core at start when
it is missing. It is never logged and never put in a response; `python -m waggle.core voice-setup` prints it."""
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from waggle.core import outbox, settings
from waggle.delivery import Delivery, plain_text

log = logging.getLogger("waggle.core.voiceline")

TOKEN_FILE = "voice.token"
PATH = "/voice"
HOLD_LINE = "Working on it, sir. I will message you when it is done."
ERROR_LINE = "I could not answer that just now, sir. Please try again in a moment."
MAX_BODY = 2 * 1024 * 1024
HOLD_AFTER_S = 25.0
MAX_TEXT = 4000
AUDIO_SUFFIX = {"audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac",
                "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
                "audio/wave": ".wav", "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/webm": ".webm",
                "audio/flac": ".flac", "audio/x-flac": ".flac"}
MEDIA_TYPE = {"m4a": "audio/mp4", "wav": "audio/wav"}

_busy = threading.Lock()  # one voice request at a time


# ---- the token ---------------------------------------------------------------------------------------------

def token(create=True):
    """The voice token in the Waggle home. The core creates it at start (atomically, so two starters never end up
    with different tokens); with create=False a missing file gives None."""
    p = settings.path(TOKEN_FILE)
    for _ in range(50):
        try:
            existing = p.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            existing = None
        if existing:
            return existing
        if existing is None:
            if not create:
                return None
            try:
                with open(p, "x", encoding="ascii") as f:
                    f.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
            continue
        time.sleep(0.01)  # another process has created the file and is writing it
    raise RuntimeError("voice.token exists but stays empty")


def authorized(header):
    """True when `header` (the Authorization header) carries the voice token."""
    secret = token(create=False)
    if not secret:
        return False
    return hmac.compare_digest((header or "").encode("utf-8", "replace"), f"Bearer {secret}".encode())


def setup_lines():
    """What `voice-setup` prints: the URL path and the token, for the owner to copy on their own screen."""
    return [f"URL path: {PATH}", f"Token:    {token()}"]


# ---- settings ----------------------------------------------------------------------------------------------

def telegram_copy():
    value = settings._config_value("voice_line_telegram_copy", True)
    return value if isinstance(value, bool) else str(value).strip().lower() not in ("0", "false", "no", "off")


def hold_after_s():
    try:
        return float(os.environ.get("WAGGLE_VOICE_HOLD_S") or settings._config_value("voice_line_hold_s", HOLD_AFTER_S))
    except (TypeError, ValueError):
        return HOLD_AFTER_S


def owner_chat():
    """The chat key the line talks in: voice_line_chat, else the engine chat (the owner's Telegram conversation)."""
    forced = str(settings._config_value("voice_line_chat", "") or "").strip()
    if forced:
        return forced if ":" in forced else f"telegram:{forced}"
    from waggle import chat_engine
    keys = sorted(chat_engine.engine_chat_keys())
    return keys[0] if keys else ""


# ---- the delivery of one line turn -------------------------------------------------------------------------

class LineDelivery(Delivery):
    """Collects the written reply and the spoken audio of one turn instead of sending them anywhere."""
    can_edit = False
    can_voice = True
    max_len = 1_000_000
    name = "voice-line"
    voice_capability = None  # not a Telegram voice message: the capability ledger is not told about it

    def __init__(self, audio=True):
        self.voice_format = "m4a" if audio else "none"
        self.messages, self.audio, self.audio_s = [], None, None

    def post(self, text):
        self.messages.append(text)
        return len(self.messages) - 1

    def send_voice(self, path, caption="", duration=None):
        self.audio, self.audio_s = Path(path).read_bytes(), duration
        return 1

    @property
    def text(self):
        return "\n\n".join(self.messages)


class _Run:
    """One turn running in its own thread, so the request can stop waiting for it."""

    def __init__(self, chat, text, audio_path, want_audio):
        self.chat, self.text, self.audio_path = chat, text, audio_path
        self.delivery = LineDelivery(audio=want_audio)
        self.rec, self.error = None, ""
        self.done, self.lock, self.detached = threading.Event(), threading.Lock(), False
        self.thread = threading.Thread(target=self._go, daemon=True, name="waggle-voice-line")

    def start(self):
        self.thread.start()
        return self

    def _go(self):
        try:
            from waggle import chat_engine
            self.rec = chat_engine.handle_inbound(
                self.chat, self.text, kind="voice", audio=self.audio_path, platform="telegram", origin="user",
                delivery=self.delivery, voice_source="" if self.audio_path else "phone dictation")
        except Exception as e:  # the request answers with a spoken apology; the trace holds what there is
            self.error = f"{type(e).__name__}: {e}"[:300]
            log.exception("voice line turn failed")
        finally:
            if self.audio_path:
                try:
                    Path(self.audio_path).unlink(missing_ok=True)
                except OSError:
                    pass
            with self.lock:
                detached = self.detached
                self.done.set()
            if detached:  # the request already answered "working on it": the answer goes to Telegram
                self.queue_copy()

    def written(self):
        """The written reply as the user sees it (the Heard line, a blank line, the answer), as plain text."""
        if self.delivery.text:
            return self.delivery.text
        if self.rec is not None:
            from waggle import chat_engine
            return plain_text(chat_engine.shown_reply(self.rec))
        return ""

    def queue_copy(self):
        """The written answer to the owner's Telegram chat through the outbox, once per turn."""
        text = self.written()
        if not text or self.rec is None:
            return None
        try:
            return outbox.enqueue("text", text, chat=self.chat, idem_key=f"voice:{self.rec.turn_id}", origin="voice",
                                  meta={"kind": "reply", "turn_id": self.rec.turn_id, "platform": "telegram",
                                        "address": self.chat.partition(":")[2], "turn_origin": "user",
                                        "via": "voice line"})
        except Exception as e:
            log.error("voice line copy not queued: %s: %s", type(e).__name__, e)
            return None


# ---- the response ------------------------------------------------------------------------------------------

def _json(code, body, headers=None):
    return code, {"Content-Type": "application/json", **(headers or {})}, json.dumps(body).encode("utf-8")


def _spoken_headers(spoken, status, turn_id=""):
    h = {"X-Waggle-Spoken": urllib.parse.quote(spoken or "", safe=""), "X-Waggle-Status": status}
    if turn_id:
        h["X-Waggle-Turn"] = turn_id
    return h


def _audio_of(spoken, delivery_audio):
    """(bytes, kind) of the audio to return: the turn's own, else the spoken text made into sound here."""
    from waggle import voice_reply
    if delivery_audio:
        return delivery_audio, voice_reply.sniff(delivery_audio) or "m4a"
    from waggle.config import data_path
    made = voice_reply.make_voice(spoken, data_path("voice_out"), ext="m4a", synth=voice_reply.synth_wav,
                                  convert=voice_reply.to_m4a, speed=_speed(), voice=_voice())
    if not made["ok"]:
        return b"", ""
    p = Path(made["path"])
    try:
        data = p.read_bytes()
    finally:
        p.unlink(missing_ok=True)
    return data, voice_reply.sniff(data) or "m4a"


def _voice():
    from waggle.config import Settings
    return Settings.load().voice_note_voice


def _speed():
    from waggle.config import Settings
    return Settings.load().voice_note_speed


def _answer(spoken, text, as_json, status, turn_id="", audio=None):
    """The 200 response: JSON {spoken, text}, or the audio with the spoken text in a header."""
    if as_json:
        return _json(200, {"spoken": spoken, "text": text}, _spoken_headers(spoken, status, turn_id))
    data, kind = _audio_of(spoken, audio)
    if not data:
        # No sound could be made (speech server down): the spoken text still reaches the phone, as JSON.
        return _json(200, {"spoken": spoken, "text": text}, {**_spoken_headers(spoken, status, turn_id),
                                                              "X-Waggle-Audio": "unavailable"})
    ext = "m4a" if kind == "m4a" else "wav"
    return 200, {"Content-Type": MEDIA_TYPE[kind], "Content-Disposition": f'inline; filename="waggle.{ext}"',
                 **_spoken_headers(spoken, status, turn_id)}, data


def _spoken_of(run):
    """What is said for a finished turn: the model's own spoken part, else the first sentences of the answer."""
    from waggle import voice_reply
    rec = run.rec
    spoken = (rec.voice or {}).get("spoken") or ""
    if not spoken:
        spoken = voice_reply.fallback_spoken(rec.reply)
    return spoken or "That is done, sir."


def respond(headers, rfile, query="", *, accepting=lambda: True):
    """One POST /voice. Returns (status, headers, body bytes); never raises. `headers` is the request's header
    mapping, `rfile` its body stream, `query` the URL's query string."""
    try:
        return _respond(headers, rfile, query, accepting)
    except Exception as e:
        log.exception("voice request failed")
        return _json(500, {"error": f"{type(e).__name__}: {e}"[:200]})


def _length(headers):
    try:
        return int(headers.get("Content-Length") or -1)
    except ValueError:
        return -1


def _drain(rfile, length):
    """Read and drop a request body (at most 4 x MAX_BODY) so that an early refusal is not lost when the connection
    closes with unread data (on Windows that resets the connection before the client has read the answer)."""
    left = min(max(length, 0), 4 * MAX_BODY)
    while left > 0:
        chunk = rfile.read(min(left, 65536))
        if not chunk:
            break
        left -= len(chunk)


def _respond(headers, rfile, query, accepting):
    length = _length(headers)
    if not authorized(headers.get("Authorization", "")):
        _drain(rfile, length)
        return _json(401, {"error": "unauthorized"})
    if not _busy.acquire(blocking=False):
        _drain(rfile, length)
        return _json(429, {"error": "another voice request is running"}, {"Retry-After": "5"})
    try:
        return _serve(headers, rfile, query, accepting, length)
    finally:
        _busy.release()


def _serve(headers, rfile, query, accepting, length):
    as_json = urllib.parse.parse_qs(query).get("reply", [""])[0].lower() == "json"
    if length < 0:
        return _json(411, {"error": "Content-Length is required"})
    if length > MAX_BODY:
        _drain(rfile, length)
        return _json(413, {"error": "the body is at most 2 MB"})
    if length == 0:
        return _json(400, {"error": "an empty body"})
    body = rfile.read(length)  # read before judging it, so that a refusal below reaches the client
    kind = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not (kind == "application/json" or kind.startswith("audio/")):
        return _json(415, {"error": "send JSON {\"text\": ...} or audio/*"})
    text, audio_path = "", ""
    if kind == "application/json":
        try:
            data = json.loads(body.decode("utf-8"))
            text = data.get("text") if isinstance(data, dict) else None
        except (UnicodeDecodeError, ValueError):
            text = None
        if not isinstance(text, str) or not text.strip():
            return _json(400, {"error": "a JSON object with a non-empty \"text\" is required"})
        if len(text) > MAX_TEXT:
            return _json(400, {"error": f"the text is at most {MAX_TEXT} characters"})
    if not accepting():
        return _json(503, {"error": "the core is not accepting turns now"})
    chat = owner_chat()
    if not chat:
        return _json(503, {"error": "no engine chat is set up (engine_chats in config.json)"})
    if kind != "application/json":
        from waggle.config import data_path
        folder = data_path("voice_in")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{uuid.uuid4().hex[:12]}{AUDIO_SUFFIX.get(kind, '.m4a')}"
        path.write_bytes(body)
        audio_path = str(path)
    t0 = time.time()
    run = _Run(chat, text.strip(), audio_path, want_audio=not as_json).start()
    run.done.wait(hold_after_s())
    with run.lock:
        finished = run.done.is_set()
        if not finished:
            run.detached = True
    if not finished:
        log.info("voice line: not finished after %.0f s, answering with the holding line", time.time() - t0)
        return _answer(HOLD_LINE, HOLD_LINE, as_json, "working")
    if run.rec is None:
        return _answer(ERROR_LINE, ERROR_LINE, as_json, "error")
    spoken, written = _spoken_of(run), run.written()
    if telegram_copy():
        run.queue_copy()
    return _answer(spoken, written, as_json, "done", run.rec.turn_id, audio=run.delivery.audio)
