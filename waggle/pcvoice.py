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

Settings (config.json in the Waggle home, all optional): pc_voice_hotkey ("ctrl+alt+backquote"), pc_voice_wake
(true), pc_voice_wake_names (["Waggle"]), pc_voice_followup_s (6), pc_voice_end_silence_s (1.2),
pc_voice_max_s (30), pc_voice_start_timeout_s (6), pc_voice_min_level (120, the lowest frame RMS that can start
speech), pc_voice_speak_messages (true). A change is read when the process starts."""
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

from waggle import mic, speaker

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
    "pc_voice_speak_messages": True,
}
WAKE_END_SILENCE_S, WAKE_MAX_S, WAKE_MIN_SPEECH_MS = 0.8, 10.0, 300
MAX_TURNS = 12
SPOOL_MAX_AGE_S = 180.0
REQUEST_TIMEOUT_S = 120.0

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

class Reply:
    def __init__(self, status, *, wav=None, spoken="", http=0, error="", line=""):
        self.status, self.wav, self.spoken, self.http, self.error, self.line = status, wav, spoken, http, error, line

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


# ---- the process -------------------------------------------------------------------------------------------

class PcVoice:
    """The brain: one thread that owns the microphone and runs conversations; the hotkey thread only signals."""

    def __init__(self, *, mic_factory=None, spk=None, client=None, sounds=None, wake=None, names=None,
                 dry_wake=False, on_event=None):
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

    # -- signals
    def hotkey_pressed(self):
        if self.spk is not None and self.spk.speaking:
            self.spk.stop()  # cut Waggle off at once, from the hotkey thread
        self.events.put("hotkey")

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
    def listen(self, src, start_timeout_s, *, end_silence_s=None, max_s=None):
        """One utterance from `src`: (pcm, info) or (None, info) when nobody spoke in time. A hotkey press while
        recording ends it at once (and sends what there is)."""
        ep = mic.Endpointer(end_silence_s=end_silence_s or setting("pc_voice_end_silence_s"),
                            max_s=max_s or setting("pc_voice_max_s"), min_on=setting("pc_voice_min_level"),
                            min_off=setting("pc_voice_min_level") * 2 / 3)
        t0 = time.monotonic()
        info = {"ended_by": "", "speech_ms": 0}
        last_loud = None
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
            if ep.in_speech and mic.level(f)[0] >= ep.off_bar():
                last_loud = time.monotonic()
            if r == "start":
                self._event("speech_start")
            if r == "end":
                info["ended_by"] = "silence" if ep._silent_ms >= ep.end_silence_s * 1000 else "max length"
                break
            if not ep.in_speech and time.monotonic() - t0 > start_timeout_s:
                info["ended_by"] = "no speech"
                break
        info.update(speech_ms=ep.speech_ms, floor=round(ep.floor(), 1), peak=ep.peak,
                    speech_rms=round(ep.speech_rms(), 1), last_loud=last_loud)
        if not ep.in_speech:
            return None, info
        self._event("speech_end", last_loud=last_loud)
        return ep.pcm(), info

    # -- one conversation
    def conversation(self, src, first="hotkey", text=None):
        """first: "hotkey" (start tone, then listen), "listen" (listen without a tone: after "Sir?") or "text"
        (the request is `text`, already heard through the wake word)."""
        self.in_conversation = True
        self.stats["conversations"] += 1
        mode = first
        try:
            for _ in range(MAX_TURNS):
                if self.stopping.is_set():
                    return
                if mode == "text":
                    self._event("request_sent")
                    reply = self.client.voice_text(text)
                else:
                    if mode == "hotkey":
                        self.tone("start")
                    src.flush()
                    timeout = setting("pc_voice_followup_s") if mode == "followup" else setting(
                        "pc_voice_start_timeout_s")
                    pcm, info = self.listen(src, timeout)
                    if pcm is None:
                        if mode != "followup":
                            self.tone("stop")
                        log.info("conversation ends: %s", info["ended_by"])
                        return
                    self.tone("stop", wait=False)
                    self._event("request_sent")
                    reply = self.client.voice_audio(pcm)
                    del pcm
                self.stats["turns"] += 1
                self._event("reply", status=reply.status, http=reply.http)
                if reply.error:
                    self._error(f"turn: {reply.status} {reply.error}")
                if self._pressed():  # pressed while waiting: the owner wants to say something else
                    if reply.wav:
                        Path(reply.wav).unlink(missing_ok=True)
                    mode = "hotkey"
                    continue
                item = self.spk.say(reply.wav, delete=True, label="reply") if reply.wav else self.say_line(
                    reply.line or "failed")
                cut = self.wait_speech(item)
                if cut:
                    mode = "hotkey"
                    continue
                if reply.status != "done" or not reply.wav:
                    return  # a holding line or a failure: no follow-up (the answer comes later)
                time.sleep(0.15)  # the room's echo of the last word
                mode = "followup"
        finally:
            self.in_conversation = False

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
        self.stats["transcribed"] += 1
        self.stats["transcribe_s"] = round(self.stats["transcribe_s"] + time.monotonic() - t0, 2)
        if r.get("error"):
            self._error(f"transcribe: {r['error']}")
            return
        rest = match_wake(r.get("text") or "", self.names)
        if rest is None:
            return  # not for Waggle: the words go out of scope here, unread
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
                    ep = ep or mic.Endpointer(WAKE_END_SILENCE_S, WAKE_MAX_S, min_on=setting("pc_voice_min_level"),
                                              min_off=setting("pc_voice_min_level") * 2 / 3)
                    if ep.feed(f) == "end":
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
    if "reply" in started and "last_loud" in marks:
        out["end_of_speech_to_voice_s"] = round(started["reply"] - marks["last_loud"], 2)
        out["endpoint_wait_s"] = round(marks.get("request_sent", 0) - marks["last_loud"], 2)
        out["request_to_reply_s"] = round(marks.get("reply", 0) - marks.get("request_sent", 0), 2)
        out["reply_to_voice_s"] = round(started["reply"] - marks.get("reply", 0), 2)
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
