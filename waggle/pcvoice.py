"""The PC voice: talk to Waggle at the PC, hands-free, and hear it answer through the PC speakers.

    python -m waggle.pcvoice run        the long-lived, windowless process (started at logon by
                                        tools/install-pcvoice-task.ps1, under pythonw)
    python -m waggle.pcvoice status     what the running process last wrote about itself
    python -m waggle.pcvoice stop       ask the running process to stop (it exits within a second)
    python -m waggle.pcvoice devices    the recording devices Windows offers
    python -m waggle.pcvoice levels     three seconds of microphone levels (kept in memory, never written)
    python -m waggle.pcvoice once --inject FILE.wav
                                        one conversation turn with FILE in place of the microphone, as if the
                                        hotkey had been pressed (checks and measurements)

How a conversation goes
  1. The hotkey (default Ctrl+Alt+` , the key under Esc; Ctrl+Alt+Space was asked for, but on the target PC
     another program already holds it, measured with RegisterHotKey) starts listening: a short rising tone, then the microphone is recorded
     until about 1.2 s of silence (30 s at most), then a short falling tone. Pressing the hotkey again while
     recording sends what was said at once; pressing it while Waggle speaks cuts Waggle off and listens.
  2. The recording goes to the core's POST /voice on 127.0.0.1 (?format=wav&client=pc) with the voice token,
     which is read from its file and never logged or printed. That runs one normal turn in the owner's chat
     (the same conversation as Telegram), and the written answer still goes to Telegram as the record, without
     a notification sound.
  3. The spoken answer plays through the default output device. Afterwards Waggle listens for a follow-up for a
     few seconds (pc_voice_followup_s, default 6) without the hotkey; silence ends the conversation.
  4. When the core is down or the request fails, a short pre-made spoken line says so (made once by the local
     speech server and kept on disk); if even that cannot be made, two low tones play. Never silence.

The wake word (pc_voice_wake, on by default after the measurement in docs/PC_VOICE.md: 10 minutes of room sound
and 31 played sentences, no false trigger, 0.16% of the machine's CPU, no measurable GPU): the microphone is
read all the time; an energy gate cuts out each utterance, which the core transcribes (POST /voice/transcribe,
same token). When the words begin with a wake name (pc_voice_wake_names, default ["Waggle"]; "Hey Waggle" works
too) the rest is the request, or Waggle answers "Sir?" and listens when it was only the name. Audio that does not
begin with a wake name is dropped at once: it is never written to disk here, and the core deletes its copy as
soon as the words are known; the words are not logged.

Waggle's own messages (reminders, outreach, finished background jobs) are spoken here when the owner is at the
PC: the core's sender drops a small JSON file into <home>/pcvoice/say/ (waggle/pcspeak.py) and this process says
it between conversations.

Who is being addressed, and speaking sentence by sentence (waggle/voice_intent.py; "pc_voice_smart": false turns all
of this off and gives back the behaviour above):
  * What is heard without the wake name (the follow-up window after an answer, and after "Sir?") is transcribed first
    and judged: background talk (TV, another person) is ignored, "stop" / "never mind" ends the conversation and cuts
    the speech, and a transcript that mostly repeats what Waggle just said through the speakers (an echo) is dropped.
    The judge is a small local model; when it is slow or down the words are sent, as before. A hotkey press is always
    a request: it is never judged.
  * When Waggle's answer ends in a question the follow-up window is longer (pc_voice_question_s, 10).
  * The answer is requested from POST /voice/stream and said sentence by sentence: the first sentence is spoken
    while the model is still writing the rest. An older core without that path is answered through POST /voice.

Settings (config.json in the Waggle home, all optional): pc_voice_hotkey ("ctrl+alt+backquote"), pc_voice_wake
(true), pc_voice_wake_names (["Waggle"]), pc_voice_followup_s (6), pc_voice_end_silence_s (1.2),
pc_voice_max_s (30), pc_voice_start_timeout_s (6), pc_voice_min_level (120, the lowest frame RMS that can start
speech), pc_voice_speak_messages (true), pc_voice_smart (true: the master switch of the two paragraphs above),
pc_voice_judge (true), pc_voice_stream (true), pc_voice_question_s (10), pc_voice_judge_model ("": gemma4:latest),
pc_voice_judge_timeout_s (1.5), pc_voice_judge_min (0.5, how sure the judge must be that it was said to Waggle).
A failed judgement skips the judge for a minute (the words are sent at once) and loads its model again in the background.
Every turn logs one "turn timing:" line (seconds per stage, never the words); see docs/PC_VOICE.md.

Hearing (waggle/vad.py, two small ONNX models in <home>/models, fetched with `python -m waggle.vad fetch`):
  * pc_voice_vad ("silero", the default | "energy"): which gate finds speech. Silero VAD judges the sound itself, so
    music, thumps and typing stop starting utterances (and stop holding one open); "energy" is the old loudness gate.
  * pc_voice_endturn ("silence", the default | "smart"): when the utterance is over. "silence" is the fixed
    pc_voice_end_silence_s (1.2). "smart" asks Smart Turn v3 at every pause of pc_voice_endturn_pause_ms (200)
    whether the speaker has finished; yes ends the turn at once, no keeps listening up to pc_voice_endturn_max_s
    (1.6) of silence; pc_voice_endturn_min_p (0.5) is how sure the model must be. Off by default: on the measured
    set it ended turns in the middle of thinking pauses (docs/PC_VOICE.md). Set pc_voice_endturn_pause_ms 1200 and
    pc_voice_endturn_max_s 2.0 to use it only to wait LONGER than today (never to end earlier).
  A missing model file, a file that is not the pinned one, or a missing onnxruntime means the old behaviour.
A change is read when the process starts."""
import argparse
import hashlib
import http.client
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from waggle import mic, speaker, voice_intent

log = logging.getLogger("waggle.pcvoice")

FOLDER = "pcvoice"
LOCK_FILE = "pcvoice.lock"
STOP_FILE = "pcvoice.stop"
STATUS_FILE = "status.json"
LOG_FILE = "pcvoice.log"
SPOOL = "say"

DEFAULTS = {
    "pc_voice_hotkey": "ctrl+alt+backquote",
    "pc_voice_wake": True,
    "pc_voice_wake_names": ["Waggle"],
    "pc_voice_followup_s": 6.0,
    "pc_voice_end_silence_s": 1.2,
    "pc_voice_max_s": 30.0,
    "pc_voice_start_timeout_s": 6.0,
    "pc_voice_min_level": 120.0,
    "pc_voice_vad": "silero",
    "pc_voice_endturn": "silence",
    "pc_voice_endturn_max_s": 1.6,
    "pc_voice_endturn_pause_ms": 200.0,
    "pc_voice_endturn_min_p": 0.5,
    "pc_voice_speak_messages": True,
    "pc_voice_smart": True,
    "pc_voice_judge": True,
    "pc_voice_stream": True,
    "pc_voice_question_s": 10.0,
    "pc_voice_judge_model": "",
    "pc_voice_judge_timeout_s": voice_intent.JUDGE_TIMEOUT_S,
    "pc_voice_judge_min": voice_intent.JUDGE_MIN_CONFIDENCE,
}
WAKE_END_SILENCE_S, WAKE_MAX_S, WAKE_MIN_SPEECH_MS = 0.8, 10.0, 300
MAX_TURNS = 12
SPOOL_MAX_AGE_S = 180.0
REQUEST_TIMEOUT_S = 120.0
STT_WARM_S = 120.0  # the core unloads its recogniser after 180 idle seconds: ask for it again after this long

LINES = {
    "core_down": "I can't reach Waggle's core right now, sir. It may be restarting.",
    "failed": "Something went wrong with that request, sir. Please try again.",
    "busy": "I'm still answering your last request, sir.",
    "sir": "Sir?",
}
GREETINGS = {"hey", "hi", "ok", "okay", "yo", "hello", "oh"}


# ---- settings and files ------------------------------------------------------------------------------------

def setting(key):
    from waggle.core import settings
    value = settings._config_value(key, DEFAULTS[key])
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).strip().lower() not in ("0", "false", "no", "off")
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, list):
        if isinstance(value, str):
            value = [value]
        return [str(v) for v in value if str(v).strip()] or list(default)
    return str(value or default)


def home():
    from waggle import config
    return Path(config.HOME)


def folder(*parts):
    p = home().joinpath(FOLDER, *parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def lock_path():
    home().mkdir(parents=True, exist_ok=True)
    return home() / LOCK_FILE


def running():
    """Is a PC voice process running for this Waggle home (it holds the lock file)?"""
    from waggle.core import procs
    p = home() / LOCK_FILE
    return p.exists() and procs.lock_held(p)


# ---- the wake name -----------------------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z0-9']+")


def match_wake(text, names):
    """None when `text` does not begin with one of `names` (after an optional greeting such as "hey"); else the
    request that follows the name ("" when it was only the name)."""
    tokens = list(_WORD.finditer(text or ""))
    if not tokens:
        return None
    i = 1 if tokens[0].group().lower() in GREETINGS and len(tokens) > 1 else 0
    for name in names:
        parts = [w.lower() for w in _WORD.findall(name)]
        if not parts or i + len(parts) > len(tokens):
            continue
        said = [t.group().lower() for t in tokens[i:i + len(parts)]]
        last = said[-1]
        if said[:-1] == parts[:-1] and last in (parts[-1], parts[-1] + "s", parts[-1] + "'s"):
            rest = text[tokens[i + len(parts) - 1].end():]
            return rest.lstrip(" ,.!?;:-—–").strip()
    return None


# ---- the hotkey --------------------------------------------------------------------------------------------

MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23,
        "pageup": 0x21, "pagedown": 0x22, "scrolllock": 0x91, "`": 0xC0, "backquote": 0xC0}
MOD_NOREPEAT, WM_HOTKEY, WM_QUIT = 0x4000, 0x0312, 0x0012


def parse_hotkey(text):
    """"ctrl+alt+space" -> (modifier bits, virtual key). Raises ValueError for an unknown key."""
    mods, vk = 0, None
    for part in [p.strip().lower() for p in str(text).split("+") if p.strip()]:
        if part in MODS:
            mods |= MODS[part]
        elif part in KEYS:
            vk = KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", part):
            vk = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError(f"unknown key {part!r} in hotkey {text!r}")
    if vk is None:
        raise ValueError(f"hotkey {text!r} has no key")
    return mods, vk


class Hotkey:
    """A system-wide hotkey (RegisterHotKey on a thread of its own, no window). on_press() runs on that thread.
    The registration is released when the thread ends (stop(), or the process exiting)."""

    def __init__(self, text, on_press):
        self.text, self.on_press = text, on_press
        self.mods, self.vk = parse_hotkey(text)
        self.ok, self.error = False, ""
        self._ready = threading.Event()
        self._tid = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="waggle-pcvoice-hotkey")

    def start(self, timeout=5.0):
        self._thread.start()
        self._ready.wait(timeout)
        return self

    def _run(self):
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32")
        u32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        u32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        u32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        u32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                     wintypes.UINT)
        msg = wintypes.MSG()
        u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # creates this thread's message queue
        self._tid = k32.GetCurrentThreadId()
        if not u32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.vk):
            self.error = f"RegisterHotKey failed (error {ctypes.get_last_error()}): another program may own {self.text}"
            self._ready.set()
            return
        self.ok = True
        self._ready.set()
        try:
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.on_press()
                    except Exception:
                        log.exception("hotkey handler failed")
        finally:
            u32.UnregisterHotKey(None, 1)
            self.ok = False

    def stop(self):
        if self._tid:
            import ctypes
            ctypes.WinDLL("user32").PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        self._thread.join(5)


# ---- talking to the core -----------------------------------------------------------------------------------

def format_timing(stages, extra=None):
    """The one log line per voice turn: seconds per stage in the order they happen (a dash: not measured this
    turn). end = silence waited to be sure you had finished; stt, judge: this process asking the core and the local
    model; context: the core building the notes block; first_words, first_audio: from sending the request;
    total: the end of your last word to the first sound of the answer."""
    def sec(v):
        return "-" if v is None else f"{v:.2f}s"
    out = "turn timing: " + " ".join(f"{k}={sec(stages.get(k))}" for k in
                                     ("end", "stt", "judge", "context", "first_words", "first_audio", "total"))
    extra = {k: v for k, v in (extra or {}).items() if v not in (None, "")}
    if extra:
        out += " (" + " ".join(f"{k}={v}" for k, v in extra.items()) + ")"
    return out


def ends_in_question(text):
    """Does what Waggle said end in a question (so the owner's next words are probably the answer)?"""
    return str(text or "").rstrip().rstrip("\"')]}*_ \u201d\u2019").endswith("?")


def core_timings(raw):
    """The seconds the core reports for a turn (numbers only, whatever else it sends is dropped)."""
    if not isinstance(raw, dict):
        return {}
    return {k: round(float(v), 2) for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)}


class Reply:
    def __init__(self, status, *, wav=None, spoken="", http=0, error="", line="", streamed=0, timings=None):
        self.status, self.wav, self.spoken, self.http, self.error, self.line = status, wav, spoken, http, error, line
        self.streamed = streamed  # sentences handed to the speaker while the answer was still coming
        self.timings = timings or {}  # what the core measured: stt_s, context_s, model_first_s (seconds)

    def __repr__(self):
        return f"Reply({self.status!r}, http={self.http}, wav={bool(self.wav)}, line={self.line!r})"


class CoreClient:
    """POST /voice and /voice/transcribe on the local core. The token is read from voice.token for each request
    (the core may have been set up after this process started) and is never logged."""

    def __init__(self, port=None, timeout=REQUEST_TIMEOUT_S, out_dir=None):
        from waggle.core import settings
        self.port = int(port or settings.port())
        self.timeout = timeout
        self.out_dir = Path(out_dir) if out_dir else None

    def _token(self):
        try:
            return (home() / "voice.token").read_text(encoding="ascii").strip()
        except OSError:
            return ""

    def _post(self, path, body, ctype, timeout=None):
        token = self._token()
        if not token:
            raise ConnectionError("no voice.token in the Waggle home (has the core ever started?)")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout or self.timeout)
        try:
            conn.request("POST", path, body=body, headers={"Content-Type": ctype, "Authorization": f"Bearer {token}"})
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def _reply(self, path, body, ctype):
        try:
            code, headers, data = self._post(path, body, ctype)
        except (OSError, http.client.HTTPException) as e:
            return Reply("core_down", error=f"{type(e).__name__}: {e}"[:200], line="core_down")
        spoken = urllib.parse.unquote(headers.get("x-waggle-spoken", ""))
        status = headers.get("x-waggle-status", "")
        if code == 200 and headers.get("content-type", "").startswith("audio/"):
            out = self.out_dir or folder("tmp")
            out.mkdir(parents=True, exist_ok=True)
            p = out / f"reply-{uuid.uuid4().hex[:10]}.wav"
            p.write_bytes(data)
            return Reply(status or "done", wav=p, spoken=spoken, http=code)
        if code == 200:  # JSON: the core could not make sound (speech server down)
            return Reply(status or "done", spoken=spoken, http=code, error="no audio from the core", line="failed")
        if code == 429:
            return Reply("busy", http=code, line="busy")
        if code == 503:
            return Reply("core_down", http=code, error=data[:200].decode("utf-8", "replace"), line="core_down")
        return Reply("failed", http=code, error=data[:200].decode("utf-8", "replace"), line="failed")

    def voice_audio(self, pcm):
        return self._reply("/voice?format=wav&client=pc", mic.wav_bytes(pcm), "audio/wav")

    def voice_text(self, text):
        return self._reply("/voice?format=wav&client=pc", json.dumps({"text": text}).encode("utf-8"),
                           "application/json")

    def voice_stream(self, kind, payload, on_sentence):
        """POST /voice/stream: kind "audio" (payload: PCM) or "text" (payload: the words). on_sentence(text) is
        called for each sentence as the core finishes it; returns the Reply of the last line (no wav: the caller
        made the sound). Status "stream_unavailable" (an older core, nothing was run): ask /voice instead."""
        body, ctype = ((mic.wav_bytes(payload), "audio/wav") if kind == "audio"
                       else (json.dumps({"text": payload}).encode("utf-8"), "application/json"))
        token = self._token()
        if not token:
            return Reply("core_down", error="no voice.token in the Waggle home (has the core ever started?)",
                         line="core_down")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        count, final = 0, None
        try:
            conn.request("POST", "/voice/stream?client=pc", body=body,
                         headers={"Content-Type": ctype, "Authorization": f"Bearer {token}"})
            r = conn.getresponse()
            if r.status != 200:
                data = r.read()
                err = data[:200].decode("utf-8", "replace")
                if r.status in (401, 404, 405):  # an older core: the path is not there, nothing ran
                    return Reply("stream_unavailable", http=r.status, error=err)
                if r.status == 429:
                    return Reply("busy", http=r.status, line="busy")
                if r.status == 503:
                    return Reply("core_down", http=r.status, error=err, line="core_down")
                return Reply("failed", http=r.status, error=err, line="failed")
            while True:
                raw = r.readline()
                if not raw:
                    break
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if isinstance(obj.get("sentence"), str) and obj["sentence"].strip():
                    count += 1
                    on_sentence(obj["sentence"].strip())
                if obj.get("done"):
                    final = obj
                    break
        except (OSError, http.client.HTTPException) as e:
            err = f"{type(e).__name__}: {e}"[:200]
            return (Reply("failed", error=err, streamed=count) if count
                    else Reply("core_down", error=err, line="core_down"))
        finally:
            conn.close()
        if final is None:
            return Reply("failed", error="the stream ended early", streamed=count, line="" if count else "failed")
        return Reply(str(final.get("status") or "done"), spoken=str(final.get("spoken") or ""), http=200, streamed=count,
                     timings=core_timings(final.get("timings")))

    def warm_stt(self):
        """Have the core load its recogniser now: a quarter second of silence goes through /voice/transcribe (the
        core unloads the recogniser after 3 idle minutes and a cold load takes seconds). True when it answered."""
        try:
            code, _, _ = self._post("/voice/transcribe", mic.wav_bytes(bytes(2) * (mic.RATE // 4)), "audio/wav",
                                    timeout=60)
        except (OSError, http.client.HTTPException):
            return False
        return code == 200

    def transcribe(self, pcm):
        """{"text", "ok", ...} or {"error"}; the words are for the caller only (never logged here)."""
        try:
            code, _, data = self._post("/voice/transcribe", mic.wav_bytes(pcm), "audio/wav", timeout=60)
        except (OSError, http.client.HTTPException) as e:
            return {"error": f"{type(e).__name__}: {e}"[:200], "text": ""}
        if code != 200:
            return {"error": f"HTTP {code}", "text": ""}
        try:
            return json.loads(data)
        except ValueError:
            return {"error": "bad JSON", "text": ""}


# ---- sounds ------------------------------------------------------------------------------------------------

class Sounds:
    """The listening tones and the pre-made spoken lines, as WAV files under <home>/pcvoice/."""

    def __init__(self, synth=None):
        self.synth = synth  # (text, out_path) -> bool; default: the local speech server
        self.tones = {
            "start": speaker.tone_wav(folder("tones") / "start.wav", freq=660, ms=90, amp=0.06),
            "stop": speaker.tone_wav(folder("tones") / "stop.wav", freq=495, ms=90, amp=0.06),
            "err1": speaker.tone_wav(folder("tones") / "err1.wav", freq=330, ms=160, amp=0.06),
            "err2": speaker.tone_wav(folder("tones") / "err2.wav", freq=247, ms=220, amp=0.06),
        }
        self.lines = {}

    def _voice(self):
        from waggle.config import Settings
        s = Settings.load()
        return s.voice_note_voice, s.voice_note_speed

    def make(self, text, out):
        """Speak `text` into the WAV `out` with the local speech server. True when it worked."""
        if self.synth:
            return bool(self.synth(text, out))
        from waggle import voice_reply
        voice, speed = self._voice()
        r = voice_reply.make_voice(text, folder("tmp"), voice=voice, speed=speed, synth=voice_reply.synth_wav,
                                   convert=voice_reply.keep_wav, ext="out.wav", budget_s=20)
        if not r["ok"]:
            return False
        os.replace(r["path"], out)
        return True

    def line(self, key):
        """The WAV of a pre-made line, made (and kept) the first time it is needed; None if it cannot be made."""
        text = LINES[key]
        digest = hashlib.sha1((text + "|" + "|".join(map(str, self._voice()))).encode()).hexdigest()[:8]
        p = folder("lines") / f"{key}-{digest}.wav"
        if p.exists():
            return p
        try:
            return p if self.make(text, p) else None
        except Exception as e:
            log.warning("line %s not made: %s", key, e)
            return None

    def prepare(self):
        """Make every line now (at start, while nothing is waiting), so a failure later can still be spoken."""
        return {k: bool(self.line(k)) for k in LINES}


class SentenceSpeaker:
    """Says an answer sentence by sentence: add(text) as each sentence arrives; a thread of its own makes the sound
    (one sentence at a time, in order) and queues it on the speaker, so the first sentence plays while the next is
    being made. close() says no more are coming; cancel() drops what is left (the hotkey)."""

    def __init__(self, sounds, spk, log=None):
        self.sounds, self.spk, self.log = sounds, spk, log
        self.items, self.errors = [], []
        self.first_at = None  # time.monotonic() when the first sentence arrived
        self._q = queue.Queue()
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="waggle-pcvoice-sentences")
        self._thread.start()

    def add(self, text):
        if self.first_at is None:
            self.first_at = time.monotonic()
        if self.log is not None:
            self.log.add(text)
        self._q.put(text)

    def close(self):
        self._q.put(None)

    def cancel(self):
        self._cancelled.set()

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def _run(self):
        while True:
            text = self._q.get()
            if text is None:
                return
            if self._cancelled.is_set():
                continue
            out = folder("tmp") / f"sent-{uuid.uuid4().hex[:10]}.wav"
            try:
                ok = self.sounds.make(text, out)
            except Exception as e:  # noqa: BLE001
                ok = False
                self.errors.append(f"sentence speech: {e}"[:200])
            if ok and not self._cancelled.is_set():
                self.items.append(self.spk.say(out, delete=True, label="reply"))
            elif ok:
                Path(out).unlink(missing_ok=True)

    def finished(self, timeout=30.0):
        """Wait until every sentence has been made and queued. The last item (or None when nothing was made)."""
        self._thread.join(timeout)
        return self.items[-1] if self.items else None


# ---- the process -------------------------------------------------------------------------------------------

class PcVoice:
    """The brain: one thread that owns the microphone and runs conversations; the hotkey thread only signals."""

    def __init__(self, *, mic_factory=None, spk=None, client=None, sounds=None, wake=None, names=None,
                 dry_wake=False, on_event=None, smart=None, decide=None):
        self.mic_factory = mic_factory or mic.WaveInSource
        self.spk = spk
        self.client = client or CoreClient()
        self.sounds = sounds
        self.wake = setting("pc_voice_wake") if wake is None else bool(wake)
        self.names = names or setting("pc_voice_wake_names")
        self.dry_wake = dry_wake  # measurement: a wake match is counted, nothing is sent
        self.on_event = on_event  # (name, **info) for timings in checks
        self.events = queue.Queue()
        self.stopping = threading.Event()
        self.in_conversation = False
        self.stats = {"started": time.time(), "conversations": 0, "turns": 0, "utterances": 0, "transcribed": 0,
                      "transcribe_s": 0.0, "wake_triggers": 0, "spoken_messages": 0, "errors": 0, "last_error": ""}
        self.trigger_texts = []  # dry-run measurement only: what a (false) trigger heard
        self._mic = None
        self.smart = setting("pc_voice_smart") if smart is None else bool(smart)  # judge + echo + stream, see the docstring
        self.decide = decide or self._decide  # (text, asked_question=) -> voice_intent.Decision; replaced in tests
        self.spoken = voice_intent.SpokenLog()  # what Waggle said lately, for echo rejection
        self.asked = False  # Waggle's last words were a question
        self._sink = None
        self._warmed = 0.0
        self._judge_off = voice_intent.Backoff()  # a failed judgement: skip the judge for a minute, never wait twice
        self._stt_used = 0.0  # when the core's recogniser was last known to be loaded
        self._turn = {}  # this turn's stage times, logged once the answer has started (see _log_turn)
        self._gate_notes = set()
        self.stats.update(ignored=0, echoes=0, stops=0, judged=0, judge_fallbacks=0, streamed_turns=0)

    # -- signals
    def hotkey_pressed(self):
        sink = self._sink
        if sink is not None:
            sink.cancel()  # sentences still to be made are dropped too
        if self.spk is not None and self.spk.speaking:
            self.spk.stop()  # cut Waggle off at once, from the hotkey thread
        self.events.put("hotkey")

    def judging(self):
        return self.smart and setting("pc_voice_judge")

    def streaming(self):
        return self.smart and setting("pc_voice_stream") and callable(getattr(self.client, "voice_stream", None))

    def _decide(self, text, asked_question=False):
        """voice_intent.decide with the settings: the local model (kept loaded by the judge itself)."""
        from waggle.config import Settings
        s = Settings.load()
        recent = self.spoken.recent()
        skip = self._judge_off.active()
        d = voice_intent.decide(
            text, spoken_texts=recent, last_spoken=recent[-1] if recent else "", asked_question=asked_question,
            model=self.judge_model(), url=s.ollama_url, use_model=not skip,
            timeout=setting("pc_voice_judge_timeout_s"), min_confidence=setting("pc_voice_judge_min"))
        if d.source == "fallback" and d.verdict is not None:  # it was asked and did not answer in time
            self._judge_off.failed()
            self._warm_judge(force=True)  # load it properly in the background (a cut-off request aborts a load)
        elif d.source == "model":
            self._judge_off.worked()
        elif skip and d.source == "fallback":
            d.reason = "the judge failed a moment ago: skipped"
        return d

    def judge_model(self):
        return setting("pc_voice_judge_model") or voice_intent.DEFAULT_JUDGE_MODEL

    def _warm_judge(self, force=False):
        """Load the judge's model in the background: when the process starts, and when a conversation begins (at
        most once in ten minutes), so the first judgement does not wait for a cold model."""
        if not self.judging() or self.decide != self._decide:
            return
        if not force and time.monotonic() - self._warmed < 600:
            return
        self._warmed = time.monotonic()
        from waggle.config import Settings
        s = Settings.load()
        threading.Thread(target=voice_intent.warm, args=(self.judge_model(),), kwargs={"url": s.ollama_url},
                         daemon=True, name="waggle-pcvoice-warm").start()

    def _warm_stt(self, force=False):
        """Have the core load its speech recogniser in the background (it unloads after 3 idle minutes; a cold load
        took 3-12 s of the first turn). Called when speech has begun or a conversation starts."""
        warm = getattr(self.client, "warm_stt", None)
        if not callable(warm) or (not force and time.monotonic() - self._stt_used < STT_WARM_S):
            return
        self._stt_used = time.monotonic()
        threading.Thread(target=warm, daemon=True, name="waggle-pcvoice-stt-warm").start()

    def warm_models(self):
        """At start: the judge's model (the recogniser is loaded when speech begins)."""
        self._warm_judge(force=True)

    def _event(self, name, **info):
        if self.on_event:
            try:
                self.on_event(name, time.monotonic(), **info)
            except Exception:
                pass

    def _pressed(self):
        """True (and consumed) when the hotkey was pressed since the last look."""
        got = False
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return got
            if ev == "hotkey":
                got = True

    def _error(self, text):
        self.stats["errors"] += 1
        self.stats["last_error"] = text[:200]
        log.warning("%s", text)

    # -- sounds
    def tone(self, key, wait=True):
        t = self.sounds.tones[key]
        return self.spk.play_now(t, label=f"tone:{key}") if wait else self.spk.say(t, label=f"tone:{key}")

    def say_line(self, key):
        self.spoken.add(LINES[key])
        p = self.sounds.line(key)
        if p:
            return self.spk.say(p, label=f"line:{key}")
        self.spk.say(self.sounds.tones["err1"], label="tone:err1")
        return self.spk.say(self.sounds.tones["err2"], label="tone:err2")

    def wait_speech(self, item):
        """Wait for `item` to finish playing. True when the hotkey cut it off."""
        while not item.done.wait(0.05):
            if self.stopping.is_set():
                self.spk.stop()
                return False
            if self._pressed():
                self.spk.stop()
                return True
        if self._pressed():
            return True
        return item.cut

    # -- listening
    def new_endpointer(self, end_silence_s, max_s):
        """The endpointer the settings ask for (Silero VAD, Smart Turn), or the energy gate with the fixed silence
        when a model is not available; each fallback is logged once."""
        def note(text):
            if text not in self._gate_notes:
                self._gate_notes.add(text)
                log.warning("hearing: %s", text)
        ep = mic.make_endpointer(
            end_silence_s, max_s, min_on=setting("pc_voice_min_level"), min_off=setting("pc_voice_min_level") * 2 / 3,
            vad=setting("pc_voice_vad").lower(), endturn=setting("pc_voice_endturn").lower(),
            turn_max_silence_s=max(end_silence_s, setting("pc_voice_endturn_max_s")),
            turn_pause_ms=setting("pc_voice_endturn_pause_ms"), turn_min_p=setting("pc_voice_endturn_min_p"), note=note)
        self.stats["hearing"] = ep.mode
        return ep

    def warm_hearing(self):
        """Load the models the settings ask for now (onnxruntime and the two sessions take a few hundred ms), so the
        first utterance does not wait for them."""
        try:
            self.new_endpointer(setting("pc_voice_end_silence_s"), setting("pc_voice_max_s"))
        except Exception as e:  # noqa: BLE001
            log.warning("hearing: warm-up failed (%s: %s)", type(e).__name__, e)

    def listen(self, src, start_timeout_s, *, end_silence_s=None, max_s=None):
        """One utterance from `src`: (pcm, info) or (None, info) when nobody spoke in time. A hotkey press while
        recording ends it at once (and sends what there is)."""
        ep = self.new_endpointer(end_silence_s or setting("pc_voice_end_silence_s"), max_s or setting("pc_voice_max_s"))
        t0 = time.monotonic()
        info = {"ended_by": "", "speech_ms": 0}
        last_loud = ended_at = end_s = None
        while not self.stopping.is_set():
            if self._pressed():
                info["ended_by"] = "hotkey"
                break
            f = src.read(0.5)
            if f is None:
                if isinstance(src, mic.FileSource):
                    info["ended_by"] = "end of file"
                    break
                continue
            r = ep.feed(f)
            if ep.in_speech and ep.voiced:
                last_loud = time.monotonic()
            if r == "start":
                self._event("speech_start")
            if r == "end":
                info["ended_by"] = ep.ended_by or "silence"
                ended_at, end_s = time.monotonic(), round(ep.silence_s, 2)
                break
            if not ep.in_speech and time.monotonic() - t0 > start_timeout_s:
                info["ended_by"] = "no speech"
                break
        info.update(speech_ms=ep.speech_ms, floor=round(ep.floor(), 1), peak=ep.peak,
                    speech_rms=round(ep.speech_rms(), 1), last_loud=last_loud, ended_at=ended_at,
                    end_s=end_s)
        if not ep.in_speech:
            return None, info
        self._event("speech_end", last_loud=last_loud)
        return ep.pcm(), info

    def _hear(self, src, mode):
        """The next request in a conversation: ("audio", pcm), "stop" (the owner said stop), or None (nobody spoke
        in time: the conversation is over). A follow-up and what comes after "Sir?" are judged when pc_voice_judge
        is on: the recording is transcribed once to decide, background talk and echoes are skipped and listening goes
        on for what is left of the window; an accepted recording is then sent as audio, so the core recognises it
        itself and keeps its own sureness figures (the guard reads them); the hotkey is never judged."""
        judged = mode in ("followup", "listen") and self.judging()
        if mode == "followup":  # a question from Waggle gets a longer window
            window = setting("pc_voice_question_s") if self.asked and self.smart else setting("pc_voice_followup_s")
        else:
            window = setting("pc_voice_start_timeout_s")
        deadline = time.monotonic() + window
        while True:
            pcm, info = self.listen(src, max(0.5, deadline - time.monotonic()))
            if pcm is None:
                if mode != "followup":
                    self.tone("stop")
                log.info("conversation ends: %s", info["ended_by"])
                return None
            self._turn = {"end_s": info.get("end_s"), "ended_at": info.get("ended_at") or time.monotonic(),
                          "speech_ms": info.get("speech_ms"), "rms": info.get("speech_rms"), "peak": info.get("peak"),
                          "floor": info.get("floor")}
            if not judged:
                self.tone("stop", wait=False)
                return ("audio", pcm)
            t_stt = time.monotonic()
            r = self.client.transcribe(pcm)
            self._turn["stt_s"] = time.monotonic() - t_stt
            self._stt_used = time.monotonic()
            self.stats["transcribed"] += 1
            words = (r.get("text") or "").strip()
            if r.get("error"):  # cannot judge what cannot be read: send the audio, as before
                self._error(f"transcribe: {r['error']}")
                self.tone("stop", wait=False)
                return ("audio", pcm)
            if not words:
                del pcm
                if time.monotonic() >= deadline:
                    return None
                continue
            t_judge = time.monotonic()
            d = self.decide(words, asked_question=self.asked)
            self._turn["judge_s"] = time.monotonic() - t_judge
            self._turn["judge"] = d.source
            self.stats["judged"] += 1
            if d.source == "fallback":
                self.stats["judge_fallbacks"] += 1
            # never the words; a fallback says why (a timeout, no such model, the judge skipped)
            log.info("heard in a conversation window: %s (%s%s)", d.action, d.source,
                     f": {d.reason}" if d.source == "fallback" and d.reason else "")
            if d.action == "send":
                self.tone("stop", wait=False)
                return ("audio", pcm)
            del pcm
            if d.action == "stop":
                self.stats["stops"] += 1
                return "stop"
            self.stats["echoes" if d.action == "echo" else "ignored"] += 1
            if time.monotonic() >= deadline:
                return None

    def _stopped(self):
        """The owner said stop / never mind: cut Waggle off, say nothing more, end the conversation."""
        self.spk.stop()
        self.asked = False
        self.tone("stop")

    def _ask(self, request):
        """Send one request. Returns (reply, sink): sink is the SentenceSpeaker that is saying the answer, or None
        when the answer comes as one WAV (the buffered /voice path, always used when streaming is off or the core
        has no stream)."""
        kind, payload = request
        if self.streaming():
            sink = SentenceSpeaker(self.sounds, self.spk, self.spoken)
            self._sink = sink
            reply = self.client.voice_stream(kind, payload, sink.add)
            sink.close()
            if reply.status != "stream_unavailable":
                self.stats["streamed_turns"] += 1
                return reply, sink
            sink.cancel()
            self._sink = None
        reply = self.client.voice_text(payload) if kind == "text" else self.client.voice_audio(payload)
        return reply, None

    # -- one conversation
    def conversation(self, src, first="hotkey", text=None):
        """first: "hotkey" (start tone, then listen), "listen" (listen without a tone: after "Sir?") or "text"
        (the request is `text`, already heard through the wake word)."""
        self.in_conversation = True
        self.stats["conversations"] += 1
        mode = first
        if first != "listen":
            self.asked = False
        self._warm_judge()
        try:
            for _ in range(MAX_TURNS):
                if self.stopping.is_set():
                    return
                if mode == "text":
                    request = ("text", text)
                else:
                    self._turn = {}
                    if mode == "hotkey":
                        self._warm_stt()
                        self.tone("start")
                    src.flush()
                    request = self._hear(src, mode)
                    if request is None:
                        return
                    if request == "stop":
                        self._stopped()
                        return
                self._event("request_sent")
                self._turn["sent_at"] = time.monotonic()
                reply, sink = self._ask(request)
                self._turn["reply_at"] = time.monotonic()
                del request
                self.stats["turns"] += 1
                self._event("reply", status=reply.status, http=reply.http)
                if reply.error:
                    self._error(f"turn: {reply.status} {reply.error}")
                if self._pressed():  # pressed while waiting: the owner wants to say something else
                    if sink is not None:
                        sink.cancel()
                    if reply.wav:
                        Path(reply.wav).unlink(missing_ok=True)
                    mode = "hotkey"
                    continue
                if sink is not None:
                    item = sink.finished()
                    self._sink = None
                    for e in sink.errors:
                        self._error(e)
                    if item is None:  # nothing could be said (the speech server is down, or the turn failed)
                        item = self.say_line(reply.line or "failed")
                        if reply.status == "done":
                            reply.status = "failed"
                else:
                    if reply.wav:
                        self.spoken.add(reply.spoken)
                    item = self.spk.say(reply.wav, delete=True, label="reply") if reply.wav else self.say_line(
                        reply.line or "failed")
                cut = self.wait_speech(item)
                self._log_turn(reply, sink, item)
                if cut:
                    mode = "hotkey"
                    continue
                if reply.status != "done" or (sink is None and not reply.wav):
                    return  # a holding line or a failure: no follow-up (the answer comes later)
                self.asked = ends_in_question(reply.spoken)
                time.sleep(0.15)  # the room's echo of the last word
                mode = "followup"
        finally:
            self.in_conversation = False
            self._sink = None

    def _log_turn(self, reply, sink, item):
        """One INFO line with the seconds each stage of this voice turn took (never the words)."""
        t, self._turn = self._turn, {}
        try:
            first = (sink.items[0] if sink is not None and sink.items else item)
            audio_at = first.started if first is not None and first.label == "reply" else None
            sent = t.get("sent_at")
            words_at = (sink.first_at if sink is not None else t.get("reply_at"))
            end_s = t.get("end_s")
            f = {"end": end_s, "stt": t.get("stt_s"), "judge": t.get("judge_s"),
                 "context": reply.timings.get("context_s"),
                 "first_words": words_at - sent if words_at and sent else None,
                 "first_audio": audio_at - sent if audio_at and sent else None,
                 "total": (audio_at - t["ended_at"] + (end_s or 0.0)) if audio_at and t.get("ended_at") else None}
            extra = {"judge": t.get("judge"), "core_stt": reply.timings.get("stt_s"),
                     "model_first": reply.timings.get("model_first_s"), "speech_ms": t.get("speech_ms"),
                     "rms": t.get("rms"), "peak": t.get("peak"), "floor": t.get("floor"), "status": reply.status}
            log.info("%s", format_timing(f, extra))
        except Exception as e:  # noqa: BLE001  (a timing line must never break a turn)
            log.debug("turn timing not logged: %s", e)

    # -- the wake word
    def heard(self, src, ep):
        """An utterance ended while idle: transcribe it, act on a wake name, drop everything else."""
        pcm = ep.pcm(keep_tail_s=0.2)
        self.stats["utterances"] += 1
        if ep.speech_ms < WAKE_MIN_SPEECH_MS:
            return
        t0 = time.monotonic()
        r = self.client.transcribe(pcm)
        del pcm
        self._stt_used = time.monotonic()
        self._turn = {"end_s": round(ep.silence_s, 2), "ended_at": t0,
                      "stt_s": time.monotonic() - t0, "speech_ms": ep.speech_ms, "rms": round(ep.speech_rms(), 1),
                      "peak": ep.peak, "floor": round(ep.floor(), 1)}
        self.stats["transcribed"] += 1
        self.stats["transcribe_s"] = round(self.stats["transcribe_s"] + time.monotonic() - t0, 2)
        if r.get("error"):
            self._error(f"transcribe: {r['error']}")
            return
        words = r.get("text") or ""
        if self.judging() and voice_intent.is_echo(words, self.spoken.recent()):
            self.stats["echoes"] += 1  # Waggle's own voice coming back through the speakers
            return
        rest = match_wake(words, self.names)
        if rest is None:
            return  # not for Waggle: the words go out of scope here, unread
        if rest and self.judging() and voice_intent.stop_phrase(rest):
            self.stats["stops"] += 1  # "Waggle, stop": nothing to send, whatever is speaking is cut off
            self.spk.stop()
            return
        self.stats["wake_triggers"] += 1
        if self.dry_wake:
            self.trigger_texts.append(r.get("text") or "")
            log.info("wake word matched (dry run: nothing sent)")
            return
        log.info("wake word")
        if rest:
            self.conversation(src, "text", rest)
        else:
            self.wait_speech(self.say_line("sir"))
            self.asked = True  # "Sir?" was a question: what follows is probably meant for Waggle
            self.conversation(src, "listen")

    # -- Waggle's own messages
    def speak_spooled(self):
        """Say the messages the core left in the spool (oldest first). Returns how many were said."""
        if not setting("pc_voice_speak_messages"):
            return 0
        said = 0
        for p in sorted(folder(SPOOL).glob("*.json")):
            try:
                item = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                item = {}
            p.unlink(missing_ok=True)
            text = str(item.get("text") or "").strip()
            if not text or time.time() - float(item.get("created") or 0) > SPOOL_MAX_AGE_S:
                continue
            out = folder("tmp") / f"msg-{uuid.uuid4().hex[:10]}.wav"
            try:
                ok = self.sounds.make(text, out)
            except Exception as e:
                ok = False
                self._error(f"message speech: {e}")
            if ok:
                self.spoken.add(text)
                self.spk.say(out, delete=True, label="message")
                said += 1
                self.stats["spoken_messages"] += 1
        return said

    # -- the loop
    def run(self, stop_file=None, status=None):
        status = status or (lambda: None)
        last_status = last_spool = 0.0
        src = None
        ep = None
        try:
            while not self.stopping.is_set():
                if stop_file is not None and stop_file.exists():
                    stop_file.unlink(missing_ok=True)
                    break
                if time.monotonic() - last_status > 5:
                    status()
                    last_status = time.monotonic()
                if time.monotonic() - last_spool > 0.5 and not self.spk.speaking and not self.in_conversation:
                    last_spool = time.monotonic()
                    self.speak_spooled()
                if self.wake:
                    if src is None:
                        src = self._open()
                        ep = None
                        if src is None:
                            time.sleep(1.0)
                            continue
                    if self._pressed():
                        self.conversation(src, "hotkey")
                        ep = None
                        continue
                    if self.spk.speaking:  # never listen to Waggle's own voice
                        src.flush()
                        ep = None
                        time.sleep(0.05)
                        continue
                    f = src.read(0.5)
                    if f is None:
                        continue
                    ep = ep or self.new_endpointer(WAKE_END_SILENCE_S, WAKE_MAX_S)
                    r = ep.feed(f)
                    if ep.in_speech and ep.speech_ms >= WAKE_MIN_SPEECH_MS:
                        self._warm_stt()  # real speech is coming: have the recogniser loaded when it ends
                    if r == "end":
                        done, ep = ep, None
                        self.heard(src, done)
                        src.flush()
                else:
                    try:
                        ev = self.events.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    if ev != "hotkey":
                        continue
                    src = self._open()
                    if src is None:
                        self.say_line("failed")
                        continue
                    try:
                        self.conversation(src, "hotkey")
                    finally:
                        src.close()
                        src = None
        finally:
            if src is not None:
                src.close()
            status()

    def _open(self):
        try:
            src = self.mic_factory()
            src.open()
            self._mic = getattr(src, "name", "")
            return src
        except Exception as e:
            self._error(f"microphone: {type(e).__name__}: {e}")
            return None


# ---- the command line --------------------------------------------------------------------------------------

def _setup_logging():
    p = folder() / LOG_FILE
    try:
        if p.exists() and p.stat().st_size > 1_000_000:
            os.replace(p, p.with_suffix(".log.1"))
    except OSError:
        pass
    h = logging.FileHandler(p, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("waggle")
    root.addHandler(h)
    root.setLevel(logging.INFO)


def _write_status(pv, hotkey):
    data = {"pid": os.getpid(), "updated": time.time(), "wake": pv.wake, "wake_names": pv.names,
            "hotkey": hotkey.text if hotkey else "", "hotkey_ok": bool(hotkey and hotkey.ok),
            "hotkey_error": hotkey.error if hotkey else "", "microphone": pv._mic or "", **pv.stats}
    p = folder() / STATUS_FILE
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def cmd_run(args):
    from waggle.core import procs
    procs.install_quiet_children()
    lock = procs.InstanceLock(lock_path())
    if not lock.acquire():
        print("the PC voice is already running")
        return 1
    _setup_logging()
    from waggle.core import offswitch
    if offswitch.refuse_if_off("PC voice", logger=log):  # started at logon while Waggle is switched off: leave at once, exit 0
        lock.release()
        print("the PC voice is not started: Waggle is switched off (python -m waggle.core on)")
        return 0
    for old in folder("tmp").glob("*.wav"):  # left by a speech request that timed out before the server wrote it
        old.unlink(missing_ok=True)
    spk = speaker.Speaker().start()
    sounds = Sounds()
    pv = PcVoice(spk=spk, sounds=sounds, wake=args.wake, dry_wake=args.dry_wake)
    log.info("start: wake=%s hotkey=%s", pv.wake, setting("pc_voice_hotkey"))
    hotkey = None
    if not args.no_hotkey:
        try:
            hotkey = Hotkey(setting("pc_voice_hotkey"), pv.hotkey_pressed).start()
            if not hotkey.ok:
                pv._error(hotkey.error)
        except ValueError as e:
            pv._error(str(e))
    threading.Thread(target=sounds.prepare, daemon=True, name="waggle-pcvoice-lines").start()
    threading.Thread(target=pv.warm_hearing, daemon=True, name="waggle-pcvoice-hearing").start()
    threading.Thread(target=pv.warm_models, daemon=True, name="waggle-pcvoice-models").start()
    stop_file = home() / STOP_FILE
    stop_file.unlink(missing_ok=True)
    if args.seconds:
        timer = threading.Timer(args.seconds, pv.stopping.set)
        timer.daemon = True
        timer.start()
    try:
        pv.run(stop_file=stop_file, status=lambda: _write_status(pv, hotkey))
    finally:
        if hotkey:
            hotkey.stop()
        spk.close()
        lock.release()
        log.info("stop")
    if args.dry_wake:
        print(json.dumps({**pv.stats, "trigger_texts": pv.trigger_texts}))
    return 0


def cmd_once(args):
    """One conversation turn with a WAV file in place of the microphone; prints the timings as JSON."""
    from waggle.core import procs
    procs.install_quiet_children()
    marks = {}

    def on_event(name, t, **info):
        marks.setdefault(name, t)
        if name == "speech_end" and info.get("last_loud"):
            marks["last_loud"] = info["last_loud"]
        if name == "reply":
            marks["reply_status"] = info.get("status")

    started, played = {}, []

    def on_start(item):
        started.setdefault(item.label, time.monotonic())
        played.append(item.label)
    spk = speaker.Speaker(on_start=on_start).start()
    sounds = Sounds()
    pv = PcVoice(mic_factory=lambda: mic.FileSource(args.inject, realtime=True, lead_s=0.5, trail_s=8.0),
                 spk=spk, sounds=sounds, wake=False, on_event=on_event)
    src = pv._open()
    t0 = time.monotonic()
    pv.conversation(src, "hotkey")
    spk.wait_idle(60)
    spk.close()
    out = {"reply_status": marks.get("reply_status"), "played": played, "errors": pv.stats["errors"],
           "last_error": pv.stats["last_error"]}
    if "reply" in started and "request_sent" in marks:
        out["request_to_first_voice_s"] = round(started["reply"] - marks["request_sent"], 2)
    if "reply" in started and "last_loud" in marks:
        out["end_of_speech_to_voice_s"] = round(started["reply"] - marks["last_loud"], 2)
        out["endpoint_wait_s"] = round(marks.get("request_sent", 0) - marks["last_loud"], 2)
        out["request_to_reply_s"] = round(marks.get("reply", 0) - marks.get("request_sent", 0), 2)
        out["reply_to_voice_s"] = round(started["reply"] - marks.get("reply", 0), 2)  # negative: spoken while streaming
    out["total_s"] = round(time.monotonic() - t0, 2)
    print(json.dumps(out))
    return 0 if out["reply_status"] == "done" else 1


def cmd_status(args):
    p = home() / FOLDER / STATUS_FILE
    alive = running()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    print(json.dumps({"running": alive, **data}, indent=1))
    return 0


def cmd_stop(args):
    if not running():
        print("the PC voice is not running")
        return 0
    (home() / STOP_FILE).write_text("stop", encoding="ascii")
    for _ in range(50):
        if not running():
            print("stopped")
            return 0
        time.sleep(0.1)
    print("asked to stop; still running")
    return 1


def cmd_devices(args):
    print(json.dumps(mic.device_names()))
    return 0


def cmd_levels(args):
    print(json.dumps(mic.report(mic.capture(args.seconds))))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m waggle.pcvoice", description="Waggle's voice at the PC")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--wake", dest="wake", action="store_true", default=None, help="listen for the wake word")
    r.add_argument("--no-wake", dest="wake", action="store_false")
    r.add_argument("--no-hotkey", action="store_true")
    r.add_argument("--dry-wake", action="store_true", help="measurement: count wake matches, send nothing")
    r.add_argument("--seconds", type=float, default=0.0, help="stop after this long (checks)")
    o = sub.add_parser("once")
    o.add_argument("--inject", required=True, help="a WAV file in place of the microphone")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("devices")
    lv = sub.add_parser("levels")
    lv.add_argument("--seconds", type=float, default=3.0)
    args = ap.parse_args(argv)
    return {"run": cmd_run, "once": cmd_once, "status": cmd_status, "stop": cmd_stop, "devices": cmd_devices,
            "levels": cmd_levels}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
