"""Neural speech detection and end-of-turn detection for the PC voice, run in-process with onnxruntime.

Two small ONNX models, both official files, pinned by size and sha256, kept in <home>/models:

    silero_vad   snakers4/silero-vad v6.2.3, MIT, 2.3 MB. Says, for every 32 ms of 16 kHz sound, how likely it is
                 speech (0..1). Music, a fan, keys and a closing door do not count the way they do for an energy gate.
    smart_turn   pipecat-ai/smart-turn v3.2 (CPU, int8), BSD-2-Clause, 8.7 MB. Listens to the last 8 s of what was
                 said, pause included, and says how likely the speaker has FINISHED (1) or only stopped to think (0).

Nothing here is needed to run: `mic.make_endpointer` falls back to the energy gate and the fixed silence when a model
file or onnxruntime is missing, and a model that raises while running is dropped for the rest of that utterance.

    python -m waggle.vad status        which files are present and verified
    python -m waggle.vad fetch         download the missing ones (checked against the sha256 below; a file that does
                                       not match is deleted and refused)
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("waggle.vad")

RATE = 16000

MODELS = {
    "silero_vad": {
        "file": "silero_vad.onnx",
        "url": "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.3/src/silero_vad/data/silero_vad.onnx",
        "size": 2327524,
        "sha256": "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
        "licence": "MIT",
        "source": "https://github.com/snakers4/silero-vad (tag v6.2.3)",
    },
    "smart_turn": {
        "file": "smart-turn-v3.2-cpu.onnx",
        "url": "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/"
               "f766f81d3cfdf7737ac64aad813d91bbfd56bf93/smart-turn-v3.2-cpu.onnx",
        "size": 8679182,
        "sha256": "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f",
        "licence": "BSD-2-Clause",
        "source": "https://huggingface.co/pipecat-ai/smart-turn-v3 (commit f766f81)",
    },
}


class ModelError(Exception):
    pass


# ---- where the files are, and whether they are the right ones ---------------------------------------------

def models_dir():
    """<home>/models (the data home's models folder); WAGGLE_MODELS_DIR overrides it (tests, a shared copy)."""
    env = os.environ.get("WAGGLE_MODELS_DIR")
    if env:
        return Path(env)
    from waggle import config
    return Path(config.HOME) / "models"


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verified_path(name, folder=None):
    """The path of model `name` when it is present with the pinned size and sha256, else None. The file is hashed on
    every call (10 ms): this runs when a model is loaded (once per process) and in `status`/`fetch`, never per frame."""
    spec = MODELS[name]
    p = Path(folder or models_dir()) / spec["file"]
    try:
        if p.stat().st_size != spec["size"]:
            return None
        return p if sha256_of(p) == spec["sha256"] else None
    except OSError:
        return None


def _download(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "waggle-model-fetch"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch(name, folder=None, download=None):
    """Download model `name` into the models folder unless a verified copy is there. The bytes are written to a
    temporary file first and moved into place only when size and sha256 match the pinned values; otherwise nothing
    is left behind and ModelError is raised. Returns the path."""
    spec = MODELS[name]
    folder = Path(folder or models_dir())
    have = verified_path(name, folder)
    if have:
        return have
    data = (download or _download)(spec["url"])
    if len(data) != spec["size"] or hashlib.sha256(data).hexdigest() != spec["sha256"]:
        raise ModelError(f"{name}: the downloaded file is not the pinned one (size {len(data)}, sha256 "
                         f"{hashlib.sha256(data).hexdigest()[:16]}...); refused")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / spec["file"]
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return dest


# ---- loading (cached; a missing file is retried after a while, so a later `fetch` is picked up) --------------

_RETRY_S = 300.0
_cache = {}
_lock = threading.Lock()


def _session(path):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.inter_op_num_threads = 1
    so.intra_op_num_threads = 1
    so.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])


def _load(name, build):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and (hit[0] is not None or now - hit[1] < _RETRY_S):
            return hit[0]
        obj = None
        try:
            p = verified_path(name)
            if p is not None:
                obj = build(p)
            else:
                log.info("%s: no verified model file in %s", name, models_dir())
        except Exception as e:  # noqa: BLE001 (onnxruntime missing, a damaged file ...: use the fallback)
            log.warning("%s: cannot load (%s: %s)", name, type(e).__name__, e)
            obj = None
        _cache[name] = (obj, now)
        return obj


def reset():
    """Forget loaded models (tests, and after a fetch)."""
    with _lock:
        _cache.clear()


def silero():
    """The shared SileroVAD, or None when its file or onnxruntime is missing."""
    return _load("silero_vad", SileroVAD)


def smart_turn():
    """The shared SmartTurn, or None when its file or onnxruntime is missing."""
    return _load("smart_turn", SmartTurn)


# ---- Silero VAD -------------------------------------------------------------------------------------------

class SileroVAD:
    """The model. `stream()` gives one SileroStream (own recurrent state) per microphone stream."""
    WINDOW, CONTEXT = 512, 64  # samples at 16 kHz: 32 ms, and the 4 ms of the last window the model also sees

    def __init__(self, path):
        self.session = _session(path)

    def stream(self):
        return SileroStream(self)


class SileroStream:
    """Probabilities of speech for a stream of 16-bit mono 16 kHz frames of any length (480-sample frames of 30 ms
    are buffered into the model's 512-sample windows)."""

    def __init__(self, vad):
        import numpy as np
        self._np, self.vad = np, vad
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros(vad.CONTEXT, dtype=np.float32)
        self.buf = np.zeros(0, dtype=np.float32)
        self.sr = np.array(RATE, dtype=np.int64)
        self.prob = 0.0
        self.windows = 0

    def feed(self, frame):
        """Add a frame (bytes of 16-bit samples); returns the latest probability of speech (0..1)."""
        np = self._np
        x = np.frombuffer(frame[: len(frame) - len(frame) % 2], dtype="<i2").astype(np.float32) / 32768.0
        self.buf = np.concatenate([self.buf, x])
        w = self.vad.WINDOW
        while len(self.buf) >= w:
            chunk, self.buf = self.buf[:w], self.buf[w:]
            inp = np.concatenate([self.context, chunk])[None, :]
            out, self.state = self.vad.session.run(None, {"input": inp, "state": self.state, "sr": self.sr})
            self.context = inp[0, -self.vad.CONTEXT:]
            self.prob = float(out[0, 0])
            self.windows += 1
        return self.prob


# ---- Smart Turn v3: Whisper log-mel features in numpy, then the ONNX classifier ----------------------------

N_FFT, HOP, N_MELS = 400, 160, 80
TURN_SECONDS = 8
_FILTERS = None


def _hz_to_mel(f):
    import numpy as np
    f = np.asarray(f, dtype=np.float64)
    f_sp, min_log_hz = 200.0 / 3.0, 1000.0
    min_log_mel, logstep = min_log_hz / f_sp, np.log(6.4) / 27.0
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-9) / min_log_hz) / logstep, f / f_sp)


def _mel_to_hz(m):
    import numpy as np
    m = np.asarray(m, dtype=np.float64)
    f_sp, min_log_hz = 200.0 / 3.0, 1000.0
    min_log_mel, logstep = min_log_hz / f_sp, np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), f_sp * m)


def mel_filters():
    """The (201, 80) Slaney mel filter bank Whisper uses (0-8000 Hz, area-normalised)."""
    global _FILTERS
    if _FILTERS is None:
        import numpy as np
        freqs = np.linspace(0, RATE // 2, 1 + N_FFT // 2)
        pts = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(RATE / 2), N_MELS + 2))
        diff = np.diff(pts)
        slopes = pts[None, :] - freqs[:, None]
        down = -slopes[:, :-2] / diff[:-1]
        up = slopes[:, 2:] / diff[1:]
        fb = np.maximum(0.0, np.minimum(down, up))
        fb *= (2.0 / (pts[2:N_MELS + 2] - pts[:N_MELS]))[None, :]
        _FILTERS = fb.astype(np.float64)
    return _FILTERS


def turn_features(audio):
    """Whisper-style log-mel features (1, 80, 800) of the last 8 s of `audio` (float32 samples, 16 kHz), zero-padded
    in front when shorter, the way pipecat's reference code (smart-turn inference.py) prepares them."""
    import numpy as np
    n = TURN_SECONDS * RATE
    a = np.asarray(audio, dtype=np.float32)
    a = a[-n:] if len(a) > n else np.pad(a, (n - len(a), 0))
    a = (a - a.mean()) / np.sqrt(a.var() + 1e-7)  # do_normalize: zero mean, unit variance over the whole 8 s
    pad = N_FFT // 2
    x = np.pad(a.astype(np.float64), (pad, pad), mode="reflect")
    frames = 1 + (len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
    window = np.hanning(N_FFT + 1)[:-1]  # periodic Hann
    power = np.abs(np.fft.rfft(x[idx] * window, axis=1)) ** 2  # (frames, 201)
    mel = np.maximum(power @ mel_filters(), 1e-10).T  # (80, frames)
    log = np.log10(mel)[:, :-1]
    log = np.maximum(log, log.max() - 8.0)
    return ((log + 4.0) / 4.0).astype(np.float32)[None, :, :]


class SmartTurn:
    """P(the speaker has finished) for the last 8 s of an utterance."""

    def __init__(self, path):
        self.session = _session(path)
        self.last_ms = 0.0

    def complete(self, pcm):
        """`pcm`: bytes of 16-bit mono 16 kHz audio (the utterance so far, closing pause included). Returns the
        probability (0..1) that the turn is over."""
        import numpy as np
        t0 = time.perf_counter()
        a = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float32) / 32768.0
        out = self.session.run(None, {"input_features": turn_features(a)})[0]
        p = float(np.asarray(out).reshape(-1)[0])
        if not 0.0 <= p <= 1.0:  # a logit rather than a probability
            p = 1.0 / (1.0 + float(np.exp(-p)))
        self.last_ms = (time.perf_counter() - t0) * 1000
        return p


# ---- command line -----------------------------------------------------------------------------------------

def status():
    out = {"models_dir": str(models_dir()), "models": {}}
    for name, spec in MODELS.items():
        p = verified_path(name)
        out["models"][name] = {"file": spec["file"], "present": (models_dir() / spec["file"]).exists(),
                               "verified": p is not None, "licence": spec["licence"], "size": spec["size"],
                               "sha256": spec["sha256"]}
    try:
        import onnxruntime
        out["onnxruntime"] = onnxruntime.__version__
    except Exception as e:  # noqa: BLE001
        out["onnxruntime"] = f"missing ({type(e).__name__})"
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m waggle.vad", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    f = sub.add_parser("fetch")
    f.add_argument("names", nargs="*", metavar="name", help="silero_vad and/or smart_turn (default: both)")
    args = ap.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(status(), indent=2))
        return 0
    rc = 0
    for name in args.names or list(MODELS):
        if name not in MODELS:
            print(f"{name}: unknown model (known: {', '.join(MODELS)})", file=sys.stderr)
            rc = 2
            continue
        try:
            print(f"{name}: {fetch(name)}")
        except (ModelError, OSError) as e:
            print(f"{name}: FAILED: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
