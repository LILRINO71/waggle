"""Speech-to-text for voice notes the engine answers itself: the same local model the Hermes gateway uses
(faster-whisper, model "turbo"), run in Waggle's own process with the same settings (beam search, voice-activity
filter, no previous-text conditioning, the silence-hallucination gate).

Nothing is installed anywhere: faster-whisper is already importable in the interpreter Waggle runs under (Hermes
uses it from the same Python), and its model files are already in the shared Hugging Face cache, so the model is
opened with local_files_only (never downloaded here). The loaded model stays in memory for IDLE_UNLOAD_S after
the last note and is then released (about a gigabyte), so a chat that is quiet for a while costs nothing.

    transcribe(path) -> {"ok", "text", "error", "seconds" (total), "load_s", "audio_s", "source", "confidence"}
    (confidence: see confidence() below)
"""
import os
import re
import subprocess
import threading
import time
from pathlib import Path

MODEL = "turbo"
IDLE_UNLOAD_S = 180.0
MAX_BYTES = 25 * 1024 * 1024
AUDIO_SUFFIXES = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".aac", ".flac"}
NO_SPEECH_THRESHOLD, LOGPROB_THRESHOLD, VAD_MIN_SILENCE_MS = 0.6, -1.0, 500
SOURCE = "waggle faster-whisper"

_lock = threading.Lock()
_model, _model_name, _last_used, _watcher = None, None, 0.0, None


def model_name():
    return (os.environ.get("WAGGLE_STT_MODEL") or MODEL).strip() or MODEL


def _make(name, device, compute):
    from faster_whisper import WhisperModel
    return WhisperModel(name, device=device, compute_type=compute, local_files_only=True)


def _load(name):
    """The cached model; a CUDA library failure falls back to CPU int8, as Hermes does."""
    global _model, _model_name
    with _lock:
        if _model is not None and _model_name == name:
            return _model, 0.0
        t0 = time.time()
        try:
            _model = _make(name, "auto", "auto")
        except Exception as e:
            text = str(e).lower()
            if not any(k in text for k in ("cuda", "cublas", "cudnn", "cudart")):
                raise
            _model = _make(name, "cpu", "int8")
        _model_name = name
        return _model, time.time() - t0


def unload():
    global _model, _model_name
    with _lock:
        _model, _model_name = None, None


def _watch(idle_s):
    global _watcher
    while True:
        time.sleep(min(15.0, max(0.2, idle_s / 4)))
        with _lock:
            if _model is None:
                _watcher = None
                return
            if time.time() - _last_used >= idle_s:
                _watcher = None
                break
    unload()


def _start_watcher(idle_s):
    global _watcher
    with _lock:
        if _watcher is not None and _watcher.is_alive():
            return
        _watcher = threading.Thread(target=_watch, args=(idle_s,), daemon=True, name="waggle-stt-idle")
        _watcher.start()


def _keep(segment):
    """False for a segment that is probably the model inventing words over silence."""
    return not (getattr(segment, "no_speech_prob", 0.0) > NO_SPEECH_THRESHOLD
                and getattr(segment, "avg_logprob", 0.0) < LOGPROB_THRESHOLD)


# The bar for "heard well": faster-whisper's own defaults for judging one decoded segment (the values it uses to
# decide that a decode failed and must be retried): average log probability not below -1.0 (log_prob_threshold),
# no-speech probability not above 0.6 (no_speech_threshold), compression ratio not above 2.4
# (compression_ratio_threshold; a higher ratio means the text repeats itself, the sign of a looping hallucination). A note is confident only when
# every segment that was kept passes all three; one weak segment makes the whole note not confident, because the
# flag exists so that a destructive request can be read back before it is acted on.
CONF_AVG_LOGPROB, CONF_NO_SPEECH, CONF_COMPRESSION = -1.0, 0.6, 2.4


def confidence(segments, dropped=0):
    """How well the recogniser says it heard, from its own numbers per segment. Returns
    {"confident": bool, "avg_logprob": the lowest, "no_speech_prob": the highest, "compression_ratio": the highest,
    "segments": n, "dropped": n, "why": [reasons it is not confident]}. No segments is never confident."""
    segs = list(segments)
    why = []
    if not segs:
        return {"confident": False, "avg_logprob": None, "no_speech_prob": None, "compression_ratio": None,
                "segments": 0, "dropped": dropped, "why": ["no segments"]}
    logp = min(float(getattr(s, "avg_logprob", 0.0)) for s in segs)
    nospeech = max(float(getattr(s, "no_speech_prob", 0.0)) for s in segs)
    ratio = max(float(getattr(s, "compression_ratio", 0.0)) for s in segs)
    if logp < CONF_AVG_LOGPROB:
        why.append(f"average log probability {logp:.2f} is below {CONF_AVG_LOGPROB}")
    if nospeech > CONF_NO_SPEECH:
        why.append(f"no-speech probability {nospeech:.2f} is above {CONF_NO_SPEECH}")
    if ratio > CONF_COMPRESSION:
        why.append(f"compression ratio {ratio:.2f} is above {CONF_COMPRESSION}")
    return {"confident": not why, "avg_logprob": round(logp, 3), "no_speech_prob": round(nospeech, 3),
            "compression_ratio": round(ratio, 3), "segments": len(segs), "dropped": dropped, "why": why}


def decode(path, timeout=60):
    """The note as 16 kHz mono float samples, decoded by the ffmpeg that ships under ~/.hermes/tools: the PyAV in
    this interpreter is newer than faster-whisper 1.2.1 accepts (its own file decoding fails with a TypeError),
    and faster-whisper takes samples directly. Returns the file's path itself when there is no ffmpeg."""
    from . import voice_reply
    exe = voice_reply.find_ffmpeg()
    if not exe:
        return str(path)
    import numpy as np
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    r = subprocess.run([exe, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                       capture_output=True, timeout=timeout, creationflags=flags)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(("ffmpeg could not decode the note: " + r.stderr.decode("utf-8", "replace").strip())[:200])
    return normalise(np.frombuffer(r.stdout, dtype=np.float32))


# Level normalisation: the PC's webcam microphone records speech far below full scale, and the recogniser's
# voice-activity filter and its features both do worse on very quiet audio. A recording whose loud parts (the
# 99.9th percentile of the magnitude, so a single click does not count) are under QUIET_BELOW of full scale is
# raised to TARGET, at most MAX_GAIN times. Louder recordings (Telegram voice notes) are left exactly as they are.
QUIET_BELOW, TARGET, MAX_GAIN = 0.1, 0.5, 30.0


def normalise(samples):
    """The samples (float, -1..1) raised to a usable level when they are quiet; returned unchanged otherwise."""
    import numpy as np
    if samples is None or not len(samples):
        return samples
    loud = float(np.percentile(np.abs(samples), 99.9))
    if loud <= 0.0 or loud >= QUIET_BELOW:
        return samples
    gain = min(TARGET / loud, MAX_GAIN)
    return np.clip(samples * gain, -1.0, 1.0).astype(np.float32)


def audio_file_problem(path):
    """Why `path` cannot be transcribed, or "" when it can."""
    p = Path(str(path or ""))
    if p.suffix.lower() not in AUDIO_SUFFIXES:
        return "not an audio file"
    try:
        if not p.is_file():
            return "the audio file is missing"
        size = p.stat().st_size
    except OSError as e:
        return f"cannot read the audio file: {e}"[:200]
    if size == 0:
        return "the audio file is empty"
    if size > MAX_BYTES:
        return "the audio file is too large"
    return ""


def transcribe(path, *, idle_s=None, loader=None):
    """Transcribe one audio file. Never raises. `loader(name)` is for tests: it returns (model, load_s)."""
    global _last_used
    t0 = time.time()
    out = {"ok": False, "text": "", "error": "", "seconds": 0.0, "load_s": 0.0, "audio_s": 0.0, "source": SOURCE,
           "confidence": confidence([])}
    problem = audio_file_problem(path)
    if problem:
        out["error"] = problem
        return out
    try:
        model, out["load_s"] = (loader or _load)(model_name())
        samples = decode(path)
        segments, info = model.transcribe(
            samples, beam_size=5, condition_on_previous_text=False, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": VAD_MIN_SILENCE_MS},
            no_speech_threshold=NO_SPEECH_THRESHOLD, log_prob_threshold=LOGPROB_THRESHOLD,
            initial_prompt=vocabulary())
        segs = list(segments)  # the decode runs while iterating
        kept = [s for s in segs if _keep(s)]
        parts = [s.text.strip() for s in kept]
        out["confidence"] = confidence([s for s, p in zip(kept, parts) if p], dropped=len(segs) - len(kept))
        out["text"] = " ".join(p for p in parts if p).strip()
        dur = float(getattr(info, "duration", 0.0) or 0.0)
        out["audio_s"] = round(dur or (0.0 if isinstance(samples, str) else len(samples) / 16000.0), 2)
        out["ok"] = bool(out["text"])
        if not out["ok"]:
            out["error"] = "no speech was recognised"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    _last_used = time.time()
    if loader is None and _model is not None:
        _start_watcher(IDLE_UNLOAD_S if idle_s is None else idle_s)
    out["seconds"] = round(time.time() - t0, 2)
    return out

_vocab = {"t": 0.0, "text": ""}


def vocabulary(settings=None, now=None):
    """Names the user is likely to say (their project notes plus a few fixed words), as a hint for the recogniser.
    Without it project names come out as ordinary words ("Waggle repo" was heard as "bagel repo")."""
    import time as _time
    from pathlib import Path
    now = _time.time() if now is None else now
    if settings is None and _vocab["text"] and now - _vocab["t"] < 600:
        return _vocab["text"]
    names = ["Waggle", "Obsidian vault", "GitHub", "Telegram", "Claude"]
    try:
        if settings is None:
            from .config import Settings
            settings = Settings.load()
        from . import vault
        root = vault.own(settings) / "Projects"
        names += sorted(p.stem for p in root.glob("*.md"))[:40]
    except Exception:  # a hint only: never fail a transcription over it
        pass
    seen = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    text = ("Names that may come up: " + ", ".join(seen) + ".")[:600]
    _vocab.update(t=now, text=text)
    return text


# ---- clean transcripts --------------------------------------------------------------------------------------
#
# What a person does not mean to say is taken out of a transcript before the model reads it, the way good dictation
# does: filler sounds (um, uh, er, erm, ah, hmm), filler phrases at clause edges (you know, I mean, so yeah),
# immediate repeats (the the, I I) and an abandoned start followed by its restart (can you, could you send). The
# function only ever deletes words (and capitalises the word that now starts a sentence); a final check refuses any
# result that is not the original text with some words removed, or that changes a number, and returns the
# original instead. When a case is unclear the text is left alone: quoted text, capitalised sounds in the middle of a
# sentence (a name or a title), sounds right after a number ("10 um", "7 ah"), "uh-huh", "I mean it".

_SOUND = re.compile(r"(?:u+h*m+|u+h+|e+r+m*|a+h+|h+m+|m{3,})", re.I)
_TOKEN = re.compile(r"^(?P<lead>[^\w\x00]*)(?P<core>.*?)(?P<trail>[^\w\x00]*)$", re.S)
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|`[^`\n]*`')
_PLACE = re.compile(r"\x00([a-j]+)\x00")  # a quoted span; its index is written in letters (digits would look like numbers)
_NUMBER_WORDS = {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
                 "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
                 "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
                 "thousand", "million", "first", "second", "third", "half", "quarter"}
# Words that, said twice in a row, are a stumble and never meaning ("that that", "had had", "no no", "very very",
# "so so" are all legitimate and stay).
_STUMBLE = {"i", "the", "a", "an", "to", "and", "of", "in", "on", "at", "for", "with", "my", "your", "our", "we",
            "you", "he", "she", "they", "it", "its", "i'm", "i'll", "i'd", "i've", "it's", "we're", "you're",
            "he's", "she's", "they're", "let's"}
_MODALS = {"can", "could", "would", "will", "should", "shall", "do", "does", "did"}
_SUBJECTS = {"you", "i", "we"}
_WANTS = {"want", "need", "wanna", "gotta", "would", "have", "got"}
_DASHES = ("-", "—", "–")


class _Tok:
    __slots__ = ("lead", "core", "trail")

    def __init__(self, lead, core, trail):
        self.lead, self.core, self.trail = lead, core, trail

    @property
    def low(self):
        return self.core.lower()

    def text(self):
        return self.lead + self.core + self.trail


def _tokens(line):
    toks = []
    for raw in line.split():
        m = _TOKEN.match(raw)
        tok = _Tok(m["lead"], m["core"], m["trail"])
        if not tok.core and toks:  # a lone dash or ellipsis belongs to the word before it
            toks[-1].trail += tok.lead + tok.trail
            continue
        toks.append(tok)
    return toks


def _terminal(trail):
    for ch in trail:
        if ch in ".!?…":
            return ch
    return ""


def _delimiter(trail):
    """True when `trail` (the punctuation after a word) only pauses: nothing, a comma, a dash, an ellipsis."""
    return not re.search(r"[.!?:;]", trail.replace("...", "").replace("…", ""))


def _numberish(tok):
    return bool(re.search(r"\d", tok.core)) or tok.low in _NUMBER_WORDS


def _plain(tok):
    """A word with nothing around it that could be a name, a number or quoted text."""
    return bool(tok.core) and not tok.lead and "\x00" not in tok.core and not _numberish(tok)


def _starts_sentence(toks, i):
    return i == 0 or bool(_terminal(toks[i - 1].trail))


def _drop(toks, i, j):
    """Remove toks[i:j]. A full stop or question mark they ended with moves to the word before; the word after takes
    a capital when the first one removed began a sentence with one."""
    first, last = toks[i], toks[j - 1]
    if first.core[:1].isupper() and _starts_sentence(toks, i) and j < len(toks):
        nxt = toks[j]
        if nxt.core[:1].islower() and "\x00" not in nxt.core:
            nxt.core = nxt.core[0].upper() + nxt.core[1:]
    end = _terminal(last.trail)
    if end and i > 0 and not _terminal(toks[i - 1].trail):
        toks[i - 1].trail = toks[i - 1].trail.rstrip(",;:-—– ") + end
    del toks[i:j]


def _is_sound(toks, i):
    t = toks[i]
    if t.lead or not _SOUND.fullmatch(t.core):
        return False
    if not (t.core == t.core.lower() or (t.core == t.core.capitalize() and _starts_sentence(toks, i))):
        return False  # "ER", or a capitalised "Um" inside a sentence, may be a name or a title
    nxt = toks[i + 1] if i + 1 < len(toks) else None
    if t.core != t.core.lower() and not t.trail and nxt is not None and nxt.core[:1].isupper() and nxt.core != "I":
        return False  # "Um Kalthoum": a capitalised sound straight before another capitalised word is a name
    before = toks[i - 1] if i else None
    return not (before is not None and _numberish(before) and not before.trail)  # "10 um", "7 ah": a unit


def _fragment(toks, i):
    """A cut-off word start before a restart: "wh- what", "I- I want"."""
    t = toks[i]
    nxt = toks[i + 1] if i + 1 < len(toks) else None
    return (nxt is not None and t.trail.strip() in _DASHES and not t.lead and t.core.isalpha()
            and len(t.core) <= 4 and not nxt.lead and nxt.low.startswith(t.low) and len(nxt.core) >= len(t.core))


def _same_restart(toks, i):
    """k words, a pause, the same k words: the first copy is dropped. Returns k, or 0."""
    for k in (4, 3, 2):
        if i + 2 * k > len(toks):
            continue
        a, b = toks[i:i + k], toks[i + k:i + 2 * k]
        if (all(_plain(t) and not t.trail for t in a[:-1]) and _plain(a[-1]) and _delimiter(a[-1].trail)
                and all(_plain(t) for t in b) and all(not t.trail for t in b[:-1])
                and all(x.low == y.low for x, y in zip(a, b))):
            return k
    return 0


def _other_restart(toks, i):
    """A question or wish begun twice in different words: "can you, could you send" (drops "can you")."""
    if i + 4 <= len(toks):
        a, b, c, d = toks[i:i + 4]
        if (a.low in _MODALS and b.low in _SUBJECTS and not a.trail and b.trail and _delimiter(b.trail)
                and c.low in _MODALS and d.low in _SUBJECTS and all(_plain(t) for t in (a, b, c, d))):
            return 2
    if i + 5 <= len(toks):
        a, b, c, d, e = toks[i:i + 5]
        if (a.low in _SUBJECTS and b.low in ("want", "need", "wanna") and c.low == "to" and not a.trail
                and not b.trail and c.trail and _delimiter(c.trail) and d.low in _SUBJECTS and e.low in _WANTS
                and all(_plain(t) for t in (a, b, c, d, e))):
            return 3
    return 0


def _edge_phrase(toks, i):
    """Length of a filler phrase at a clause edge starting at i ("you know,", "I mean,", "so yeah"), or 0."""
    n = len(toks)
    if i + 1 >= n or toks[i].lead:
        return 0
    before = toks[i - 1] if i else None
    if before is not None and before.trail[-1:] not in (",", ".", "!", "?", ";", "…"):
        return 0
    a, b = toks[i], toks[i + 1]
    pair = (a.low, b.low)
    after = toks[i + 2] if i + 2 < n else None
    if pair not in (("you", "know"), ("i", "mean"), ("so", "yeah")) or a.trail or b.lead:
        return 0
    if any(t is not None and _numberish(t) for t in (before, after)):
        return 0
    if pair == ("i", "mean"):  # a correction ("at three, I mean four") when anything came before it: only at the very start
        return 2 if i == 0 and b.trail.startswith(",") else 0
    right = after is None or b.trail[:1] in (",", ".", "!", "…")
    return 2 if right else 0


def _clean_line(toks):
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(toks):
            n = 0
            if _fragment(toks, i) or _is_sound(toks, i):
                n = 1
            elif _edge_phrase(toks, i):
                n = 2
            elif _same_restart(toks, i):
                n = _same_restart(toks, i)
            elif _other_restart(toks, i):
                n = _other_restart(toks, i)
            elif (i + 1 < len(toks) and toks[i].low in _STUMBLE and toks[i].low == toks[i + 1].low
                  and _plain(toks[i]) and _plain(toks[i + 1]) and toks[i].trail.strip() in ("", ",")):
                n = 1
            if n:
                _drop(toks, i, i + n)
                changed = True
                continue
            i += 1
    return toks


def _words(text):
    return re.findall(r"[\w'’]+", text.lower())


def clean_transcript(text):
    """`text` without what the speaker did not mean to say (see the section comment above). Returns the text
    unchanged when nothing applies, when the result would be empty, or when the safety check below fails."""
    original = text or ""
    if not original.strip():
        return original
    saved = []

    def hide(m):
        saved.append(m.group(0))
        return "\x00" + "".join(chr(97 + int(d)) for d in str(len(saved) - 1)) + "\x00"
    hidden = _QUOTED.sub(hide, original)
    lines = []
    for line in hidden.split("\n"):
        toks = _clean_line(_tokens(line))
        lines.append(" ".join(t.text() for t in toks))
    out = _PLACE.sub(lambda m: saved[int("".join(str(ord(c) - 97) for c in m[1]))], "\n".join(lines))
    out = re.sub(r"[ \t]+([,.;:!?])", r"\1", out).strip()
    if out == " ".join(original.split()) or out == original.strip():
        return original
    if not _words(out):
        return original
    # Only deletions: the kept words are the original's, in order, and every number is still there.
    pos = 0
    theirs = _words(original)
    for w in _words(out):
        try:
            pos = theirs.index(w, pos) + 1
        except ValueError:
            return original
    if re.findall(r"\d+", out) != re.findall(r"\d+", original):
        return original
    return out
