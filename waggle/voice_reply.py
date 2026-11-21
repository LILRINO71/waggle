"""A voice note gets a short spoken answer plus the full text.

For a voice turn the model is asked, in the same turn, for the reply in a fixed format:

    SPOKEN: <one to three short sentences, plain words>
    ANSWER:
    <the full written answer>

split_reply() parses the two apart; with no SPOKEN part the spoken version is built by rule from the first
sentences of the answer (fallback_spoken). cap_spoken() then enforces, whatever the model wrote, at most
MAX_SENTENCES sentences and about MAX_WORDS words. The spoken part is turned into audio with the local speech
server (Kokoro on 127.0.0.1:8899), converted to Ogg/Opus with the ffmpeg that ships under ~/.hermes/tools, and
sent as a Telegram voice message before the text. VoiceReply starts the speech as soon as the model has finished
the SPOKEN part, while it is still writing the answer, so the audio is usually ready when the answer is.

Speech or conversion that fails or takes longer than BUDGET_S never fails the turn: the text goes out alone and
the reason is recorded.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path

MAX_SENTENCES = 3
MAX_WORDS = 45
MAX_AUDIO_S = 20.0
BUDGET_S = 15.0
KOKORO_PORT = int(os.environ.get("WAGGLE_KOKORO_PORT", "8899"))

FORMAT_NOTE = (
    "This message was spoken to you out loud (it reached you through speech recognition, so a name or term can be "
    "misheard: work out what they meant; \"ethics\" next to robots usually means FTC). Your reply goes out as a "
    "short voice message plus the written text, so write it in exactly this format, with nothing before it:\n"
    "Manner of the SPOKEN part: a composed British butler-assistant in the manner of JARVIS. Calm, precise, "
    "dry understatement, the result first, no filler, no enthusiasm, no exclamation marks. Address the user as "
    "\"sir\" once, never more.\n"
    "SPOKEN: <one to three short sentences, at most 40 words, plain words, no lists, no markdown, no code, no file "
    "paths or links, numbers written the way you say them aloud (\"ten minutes\", not \"10 min\")>\n"
    "ANSWER:\n"
    "<the full written answer, as you would give it to a typed message, with any detail, lists or code it needs>\n"
    "Everything you can do for a typed message you can do here: use your tools first when the request needs "
    "action (a reminder, a lookup, a change), then say in both parts what actually happened.")


# ---- the format ------------------------------------------------------------------------------------------

_MARK = r"[ \t*_#>]{0,6}"
_SPOKEN = re.compile(r"\A\s*" + _MARK + r"SPOKEN" + _MARK + r":[ \t*_]*", re.I)
_ANSWER = re.compile(r"(?im)^" + _MARK + r"ANSWER" + _MARK + r":[ \t*_]*\n?")


def split_reply(text):
    """(spoken, answer, had_spoken): the model's reply split by the fixed format. Without a SPOKEN part, spoken
    is "" and the answer is the whole text. Without an ANSWER part the spoken part is the first paragraph and
    the rest (or, if there is none, the spoken part itself) is the answer: the written text is never empty
    when the model said anything."""
    t = (text or "").strip()
    m = _SPOKEN.match(t)
    if not m:
        return "", t, False
    rest = t[m.end():]
    a = _ANSWER.search(rest)
    if a:
        spoken, answer = rest[:a.start()].strip(), rest[a.end():].strip()
    else:
        spoken, _, answer = rest.partition("\n\n")
        spoken, answer = spoken.strip(), answer.strip()
    return spoken, answer or spoken, True


def spoken_complete(text):
    """True once the SPOKEN part of a reply still being written is finished (its ANSWER marker has arrived)."""
    t = (text or "").lstrip()
    m = _SPOKEN.match(t)
    return bool(m and _ANSWER.search(t[m.end():]))


# ---- what can be said ------------------------------------------------------------------------------------

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|~[\\/]|\.{1,2}[\\/])\S+")
_LEAD = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]+|>+[ \t]*|[-*+•][ \t]+|\d+[.)][ \t]+)")
_SENT = re.compile(r"(?<=[.!?])[\"')\]]*\s+")


def speakable(text):
    """The words of `text` as they can be said: no code blocks, links, paths, list markers or markdown."""
    t = _FENCE.sub(" ", text or "")
    t = _LINK.sub(r"\1", t)
    t = _URL.sub(" ", t)
    t = _PATH.sub(" ", t)
    t = _LEAD.sub("", t)
    t = t.replace("`", "")
    t = re.sub(r"[*~]+", "", t)
    t = re.sub(r"(?<!\w)_+|_+(?!\w)", "", t)
    t = re.sub(r"\|", " ", t)
    return " ".join(t.split())


def sentences(text):
    return [s.strip() for s in _SENT.split(text or "") if s.strip()]


def cap_spoken(text, max_sentences=MAX_SENTENCES, max_words=MAX_WORDS):
    """At most `max_sentences` sentences and about `max_words` words of the speakable text: a cut that falls
    inside a sentence ends at the last whole sentence that fits, else at the word limit with a full stop."""
    out = []
    words = 0
    for s in sentences(speakable(text))[:max_sentences]:
        n = len(s.split())
        if words + n <= max_words:
            out.append(s)
            words += n
            continue
        if not out:  # the first sentence alone is too long: cut it at the word limit
            cut = " ".join(s.split()[:max_words]).rstrip(",;:- ")
            out.append(cut if cut[-1:] in ".!?" else cut + ".")
        break
    return " ".join(out)


def fallback_spoken(answer, **caps):
    """The spoken version built by rule: the first sentences of the written answer, capped."""
    return cap_spoken(answer, **caps)


# ---- speech and conversion -------------------------------------------------------------------------------

def synth_wav(text, out, voice, speed, timeout):
    """Kokoro through the warm local server: the WAV is written to `out`; returns its length in seconds."""
    body = json.dumps({"text": text, "out": str(out), "voice": voice, "speed": float(speed)}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{KOKORO_PORT}/", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as r:
        data = json.loads(r.read())
    if not data.get("ok") or not Path(out).is_file():
        raise RuntimeError(str(data.get("error") or "the speech server wrote no file")[:200])
    return float(data.get("seconds") or wav_seconds(out))


def wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


def find_ffmpeg():
    """The ffmpeg that ships under ~/.hermes/tools (newest version folder), else WAGGLE_FFMPEG, else None."""
    env = os.environ.get("WAGGLE_FFMPEG")
    if env and Path(env).is_file():
        return env
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    found = sorted((Path.home() / ".hermes" / "tools").glob(f"ffmpeg-*/bin/{name}"), reverse=True)
    return str(found[0]) if found else None


def to_ogg(wav, ogg, timeout, ffmpeg=None):
    """WAV -> Ogg/Opus (mono, 48 kHz, voice-tuned) with ffmpeg."""
    exe = ffmpeg or find_ffmpeg()
    if not exe:
        raise RuntimeError("ffmpeg was not found")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "libopus", "-b:a", "32k",
                        "-ar", "48000", "-ac", "1", "-application", "voip", str(ogg)],
                       capture_output=True, timeout=max(1.0, timeout), creationflags=flags)
    if r.returncode != 0 or not Path(ogg).is_file():
        raise RuntimeError(("ffmpeg: " + r.stderr.decode("utf-8", "replace").strip())[:200])


def to_m4a(wav, out, timeout, ffmpeg=None):
    """WAV -> M4A (AAC-LC, mono, 64 kbit/s), which an iPhone plays. Without ffmpeg, or when it fails, the WAV itself is
    written to `out` (the hands-free line sends whatever the file really is, by its first bytes: see sniff())."""
    exe = ffmpeg or find_ffmpeg()
    if exe:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "aac", "-b:a", "64k",
                                "-ac", "1", "-movflags", "+faststart", "-f", "ipod", str(out)],
                               capture_output=True, timeout=max(1.0, timeout), creationflags=flags)
            if r.returncode == 0 and Path(out).is_file() and Path(out).stat().st_size > 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    shutil.copyfile(wav, out)


def sniff(data):
    """What an audio file is, from its first bytes: "m4a", "wav" or "" (the file name cannot be trusted)."""
    head = bytes(data[:12])
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    return ""


def keep_wav(wav, out, timeout, ffmpeg=None):
    """No conversion: the speech server's WAV as it is (the PC voice plays WAV directly)."""
    shutil.copyfile(wav, out)


def pipeline(fmt):
    """(file extension, converter, makes audio) for a delivery's voice format: "ogg" (Telegram voice messages,
    the default), "m4a" (the hands-free line), "wav" (the PC voice: no conversion) or "none" (only the spoken
    text is wanted, no audio)."""
    if fmt == "m4a":
        return "m4a", to_m4a, True
    if fmt == "wav":
        return "out.wav", keep_wav, True  # not "wav": the speech server's own file is <stem>.wav
    if fmt == "none":
        return "m4a", to_m4a, False
    return "ogg", to_ogg, True


def ogg_seconds(path, ffmpeg=None):
    """Length of an audio file in seconds, read from ffmpeg's report (0.0 if it cannot be read)."""
    exe = ffmpeg or find_ffmpeg()
    if not exe:
        return 0.0
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    r = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True, timeout=20, creationflags=flags)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr.decode("utf-8", "replace"))
    return round(int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]), 2) if m else 0.0


def make_voice(text, folder, *, voice="af_heart", speed=1.1, budget_s=None, synth=None, convert=None,
               clock=time.monotonic, ext="ogg"):
    """Speak `text` into an Ogg/Opus file (or another format: `ext` and `convert` say which) in `folder`, within
    `budget_s` seconds in all. Never raises.
    Returns {"ok", "path", "audio_s", "synth_s", "convert_s", "words", "error"}. Audio over MAX_AUDIO_S is made
    again from a shorter text (two sentences, thirty words) when the budget allows."""
    out = {"ok": False, "path": "", "audio_s": 0.0, "synth_s": 0.0, "convert_s": 0.0, "words": 0, "error": ""}
    budget_s = BUDGET_S if budget_s is None else budget_s
    synth, convert = synth or synth_wav, convert or to_ogg
    start = clock()
    folder = Path(folder)
    stem = uuid.uuid4().hex[:10]
    wav, ogg = folder / f"{stem}.wav", folder / f"{stem}.{ext}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for attempt, say in enumerate((text, cap_spoken(text, 2, 30))):
            if not say:
                break
            out["words"] = len(say.split())
            t = clock()
            left = budget_s - (t - start)
            if left <= 0.5:
                out["error"] = f"speech took longer than {budget_s:g} s"
                break
            secs = synth(say, wav, voice, speed, left)
            out["synth_s"] = round(out["synth_s"] + (clock() - t), 2)
            if secs > MAX_AUDIO_S and attempt == 0 and say != cap_spoken(text, 2, 30):
                continue  # too long to listen to: once more, shorter
            t = clock()
            left = budget_s - (t - start)
            if left <= 0.5:
                out["error"] = f"speech took longer than {budget_s:g} s"
                break
            convert(wav, ogg, left)
            out["convert_s"] = round(clock() - t, 2)
            out["audio_s"], out["path"], out["ok"], out["error"] = round(float(secs), 2), str(ogg), True, ""
            break
        else:
            out["error"] = out["error"] or "nothing to say"
        if not out["ok"] and not out["error"]:
            out["error"] = "nothing to say"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        try:
            wav.unlink(missing_ok=True)
            if not out["ok"]:
                ogg.unlink(missing_ok=True)
        except OSError:
            pass
    return out


# ---- one voice turn --------------------------------------------------------------------------------------

class VoiceReply:
    """The spoken half of one voice turn. feed(text so far) while the model writes; finish(final text) once it is
    done: returns (answer text for the chat, spoken text); send(delivery) then sends the voice message."""

    def __init__(self, folder, *, voice="af_heart", speed=1.1, budget_s=None, synth=None, convert=None,
                 clock=time.monotonic, ext="ogg", audio=True):
        self.folder, self.voice, self.speed = folder, voice, speed
        self.ext, self.audio = ext, audio  # audio=False: the spoken text is worked out but never turned into sound
        self.budget_s = BUDGET_S if budget_s is None else budget_s
        self.synth, self.convert, self.clock = synth, convert, clock
        self.spoken, self.answer, self.had_spoken = "", "", False
        self.result = {}
        self._thread, self._started_text, self._lock = None, None, threading.Lock()

    def _start(self, spoken):
        if not self.audio:
            return
        with self._lock:
            if self._started_text == spoken:
                return
            self._started_text, self.result = spoken, {}
        box = {}
        self._box = box
        self._began = self.clock()

        def run():
            box["r"] = make_voice(spoken, self.folder, voice=self.voice, speed=self.speed, budget_s=self.budget_s,
                                  synth=self.synth, convert=self.convert, clock=self.clock, ext=self.ext)
        self._thread = threading.Thread(target=run, daemon=True, name="waggle-voice-reply")
        self._thread.start()

    def feed(self, text):
        """The reply so far: speech starts the moment the SPOKEN part is complete."""
        try:
            if self._started_text is None and spoken_complete(text):
                spoken = cap_spoken(split_reply(text)[0])
                if spoken:
                    self._start(spoken)
        except Exception:
            pass

    def finish(self, final_text):
        """Split the final reply; start the speech if it has not started (or the final SPOKEN part differs).
        Returns (answer, spoken)."""
        spoken_raw, self.answer, self.had_spoken = split_reply(final_text)
        self.spoken = cap_spoken(spoken_raw) or fallback_spoken(self.answer)
        if self.spoken:
            self._start(self.spoken)
        return self.answer, self.spoken

    def wait(self):
        """The result of the speech (waits for it, at most what is left of the budget)."""
        if self._thread is None:
            return {"ok": False, "error": "nothing speakable in the reply", "path": "", "audio_s": 0.0,
                    "synth_s": 0.0, "convert_s": 0.0, "words": 0}
        left = max(0.0, self.budget_s - (self.clock() - self._began)) + 1.0
        self._thread.join(timeout=left)
        r = self._box.get("r")
        if r is None:
            return {"ok": False, "error": f"speech took longer than {self.budget_s:g} s", "path": "", "audio_s": 0.0,
                    "synth_s": 0.0, "convert_s": 0.0, "words": 0}
        return r

    def send(self, delivery):
        """Wait for the audio and send it as a voice message. Returns the info for the turn record:
        voice_delivered, voice_message_id, voice_error, spoken, spoken_words, audio_s, synth_s, convert_s, from_model."""
        r = self.wait() if self.audio else {"ok": False, "error": ""}
        info = {"spoken": self.spoken, "spoken_words": len(self.spoken.split()), "spoken_from_model": self.had_spoken,
                "audio_s": r.get("audio_s"), "synth_s": r.get("synth_s"), "convert_s": r.get("convert_s"),
                "voice_delivered": False, "voice_message_id": None, "voice_error": r.get("error") or ""}
        path = r.get("path")
        if r.get("ok") and path:
            try:
                if not getattr(delivery, "can_voice", False):
                    raise RuntimeError("this chat cannot take voice messages")
                mid = delivery.send_voice(path, duration=int(round(r.get("audio_s") or 0)) or None)
                if mid is None or mid is False:
                    raise RuntimeError("the voice message was not accepted")
                info["voice_delivered"], info["voice_message_id"], info["voice_error"] = True, mid, ""
            except Exception as e:
                info["voice_error"] = f"send failed: {type(e).__name__}: {e}"[:200]
            finally:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
        elif not info["voice_error"] and self.audio:
            info["voice_error"] = "no audio was made"
        return info
