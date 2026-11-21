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
        segments, info = model.transcribe(
            str(path), beam_size=5, condition_on_previous_text=False, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": VAD_MIN_SILENCE_MS},
            no_speech_threshold=NO_SPEECH_THRESHOLD, log_prob_threshold=LOGPROB_THRESHOLD)
        parts = [s.text.strip() for s in list(segments) if _keep(s)]  # the decode runs while iterating
        out["text"] = " ".join(p for p in parts if p).strip()
        out["audio_s"] = round(float(getattr(info, "duration", 0.0) or 0.0), 2)
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
