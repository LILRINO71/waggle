"""Speech-to-text for voice notes the engine answers itself: the same local model the Hermes gateway uses
(faster-whisper, model "turbo"), run in Waggle's own process with the same settings (beam search, voice-activity
filter, no previous-text conditioning, the silence-hallucination gate).

Nothing is installed anywhere: faster-whisper is already importable in the interpreter Waggle runs under (Hermes
uses it from the same Python), and its model files are already in the shared Hugging Face cache, so the model is
opened with local_files_only (never downloaded here). The loaded model stays in memory for IDLE_UNLOAD_S after
the last note and is then released (about a gigabyte), so a chat that is quiet for a while costs nothing.

    transcribe(path) -> {"ok", "text", "error", "seconds" (total), "load_s", "audio_s", "source"}
"""
import os
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
    return np.frombuffer(r.stdout, dtype=np.float32)


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
    out = {"ok": False, "text": "", "error": "", "seconds": 0.0, "load_s": 0.0, "audio_s": 0.0, "source": SOURCE}
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
        parts = [s.text.strip() for s in list(segments) if _keep(s)]  # the decode runs while iterating
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
        root = Path(str(getattr(settings, "vault", "") or "")) / "Projects"
        names += sorted(p.stem for p in root.glob("*.md"))[:40]
    except Exception:  # a hint only: never fail a transcription over it
        pass
    seen = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    text = ("Names that may come up: " + ", ".join(seen) + ".")[:600]
    _vocab.update(t=now, text=text)
    return text
