"""Keyword spotting for the PC voice's wake word: a small streaming model listens for "Waggle" in the 16 kHz
microphone frames, so the core's speech recogniser is only asked about utterances that may have the name in them.

Model: k2-fsa sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01 (Apache-2.0, int8, 5.3 MB on disk, English,
open vocabulary: any word can be a keyword, no training), run by the sherpa-onnx Python package (Apache-2.0,
2.0.1, a cp314 Windows wheel of 2.5 MB + a 17 MB runtime; pip install sherpa-onnx==2.0.1). The files are pinned by
size and sha256, kept in <home>/models/kws-zipformer-gigaspeech-3.3M, and fetched from the release archive:

    python -m waggle.kws status          what is present and verified, and the sherpa-onnx version
    python -m waggle.kws fetch           download the archive (17.6 MB), check it and every file, keep the four needed
    python -m waggle.kws test FILE.wav   run the spotter over a WAV file and print the hits

The model is run on a finished utterance (its first seconds), not live: the audio is raised to a usable level and
scanned in two passes (see PASS_SHIFTS_S). The "mobile" variant of this model does not run under sherpa-onnx 1.13.8 / 2.0.1 (a Reshape error on the first
chunk, also on the model's own test wav), so the full-size archive is used.

Nothing here is needed to run: without the files or the package `load()` returns None and the PC voice keeps
transcribing every utterance (pc_voice_wake_mode "stt"). The keyword is given to the model as BPE pieces; a few
spellings of how "Waggle" sounds are listed for the model (WAGGLE_VARIANTS), other names are split into pieces
greedily from the model's own token list."""
import argparse
import hashlib
import io
import logging
import os
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from waggle import vad

log = logging.getLogger("waggle.kws")

RATE = 16000
FOLDER = "kws-zipformer-gigaspeech-3.3M"
ARCHIVE = {
    "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
           "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2",
    "size": 17626723,
    "sha256": "f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a",
    "root": "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01",
    "licence": "Apache-2.0",
    "source": "https://github.com/k2-fsa/sherpa-onnx/releases/tag/kws-models",
}
FILES = {
    "tokens": ("tokens.txt", 5006, "fd2ded4050a55d2b1578870ba8697d02371980217806b7558bd0a5cc60f3ba53"),
    "encoder": ("encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx", 4807159,
                "1e721676515bcd42a186979733981213c66c80db680e1cc582dfedf3be76e678"),
    "decoder": ("decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx", 277985,
                "e40ff43297abe815e8898494c17e71bba2152d9d40fa3eb803f75d0f7533329a"),
    "joiner": ("joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx", 163380,
               "eae9da0c7e1e6c6a3f4cc42d167899c388f6c6701b94cb96320e4f55df79624c"),
}

# "Waggle" as the model's BPE pieces (made once with the model's bpe.model; "▁" marks a word start). The first is the
# plain spelling, the rest are how a quick or soft "Waggle" is often heard: a split first piece, a dropped g, other
# endings. A hit on any of them means the name.
WAGGLE_VARIANTS = [
    "▁WA G G LE",
    "▁W A G G LE",
    "▁WA G LE",
    "▁WA G G EL",
    "▁WA G G AL",
]
GREET = "▁HE Y"  # "hey Waggle" is also given whole: it is spoken as one run and the plain name alone may then be missed
SOUND_TAIL_S = 0.5  # silence fed after the audio so a keyword at the very end is decided

DEFAULT_THRESHOLD, DEFAULT_SCORE = 0.25, 1.0


class KwsError(Exception):
    pass


# ---- files ------------------------------------------------------------------------------------------------

def model_dir(folder=None):
    return Path(folder or vad.models_dir()) / FOLDER


def verified(folder=None):
    """{"tokens", "encoder", "decoder", "joiner"} -> Path when all four files are present with the pinned size and
    sha256, else None. Hashes ~5 MB: called when the model is loaded and by `status`, never per frame."""
    d = model_dir(folder)
    out = {}
    for key, (name, size, sha) in FILES.items():
        p = d / name
        try:
            if p.stat().st_size != size or vad.sha256_of(p) != sha:
                return None
        except OSError:
            return None
        out[key] = p
    return out


def fetch(folder=None, download=None):
    """Download the release archive unless the four files are already verified. The archive must match its pinned size
    and sha256, then each wanted member must match its own; only then are the files moved into place (a temporary
    name first). Anything else raises KwsError and leaves nothing behind. Returns the file paths."""
    have = verified(folder)
    if have:
        return have
    data = (download or (lambda u: vad._download(u, 300)))(ARCHIVE["url"])
    if len(data) != ARCHIVE["size"] or hashlib.sha256(data).hexdigest() != ARCHIVE["sha256"]:
        raise KwsError(f"the downloaded archive is not the pinned one (size {len(data)}, sha256 "
                       f"{hashlib.sha256(data).hexdigest()[:16]}...); refused")
    d = model_dir(folder)
    d.mkdir(parents=True, exist_ok=True)
    got = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as tf:
            for key, (name, size, sha) in FILES.items():
                f = tf.extractfile(f"{ARCHIVE['root']}/{name}")  # read by exact name: nothing else is unpacked
                blob = f.read() if f else b""
                if len(blob) != size or hashlib.sha256(blob).hexdigest() != sha:
                    raise KwsError(f"{name}: not the pinned file; refused")
                got[key] = (d / name, blob)
    except (tarfile.TarError, KeyError, OSError) as e:
        raise KwsError(f"the archive could not be read ({type(e).__name__}: {e})") from e
    out = {}
    for key, (dest, blob) in got.items():
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(blob)
        os.replace(tmp, dest)
        out[key] = dest
    return out


# ---- keywords ---------------------------------------------------------------------------------------------

def pieces(word, tokens):
    """One word as BPE pieces: the longest known piece first, the word-start mark on the first. None when the model's
    token list cannot spell it."""
    w = "".join(c for c in word.upper() if c.isalpha() or c == "'")
    out, i = [], 0
    while i < len(w):
        for j in range(len(w), i, -1):
            piece = ("▁" if i == 0 else "") + w[i:j]
            if piece in tokens:
                out.append(piece)
                i = j
                break
        else:
            return None
    return out or None


def read_tokens(path):
    with open(path, encoding="utf-8") as f:
        return {line.split()[0] for line in f if line.strip()}


def keyword_lines(names, tokens, threshold=DEFAULT_THRESHOLD, score=DEFAULT_SCORE, variants=True, greet=True):
    """The keywords file text: one line per spelling, "PIECES :boost #threshold @LABEL". "Waggle" gets the variants
    above (and "hey Waggle"); any other name is split from the token list. Names that cannot be spelled are skipped."""
    lines = []
    for name in names:
        words = [w for w in name.replace(",", " ").split() if w]
        if not words:
            continue
        label = "_".join(w.upper() for w in words)
        if [w.lower() for w in words] == ["waggle"]:
            spell = WAGGLE_VARIANTS if variants else WAGGLE_VARIANTS[:1]
            forms = list(spell) + ([f"{GREET} {s}" for s in spell[:2]] if greet else [])
        else:
            parts = [pieces(w, tokens) for w in words]
            if any(p is None for p in parts):
                log.warning("kws: cannot spell the wake name %r from the model's tokens; skipped", name)
                continue
            forms = [" ".join(" ".join(p) for p in parts)]
        for form in forms:
            lines.append(f"{form} :{score:g} #{threshold:g} @{label}")
    return "\n".join(lines) + ("\n" if lines else "")


# ---- the spotter ------------------------------------------------------------------------------------------

class Hit:
    """One detection. start_s / end_s are seconds from the start of the audio given to scan() (the model's token
    times, which run about 0.2 s late: a token is placed when the model is sure of it)."""
    def __init__(self, label, start_s, end_s, tokens=()):
        self.label, self.start_s, self.end_s, self.tokens = label, start_s, end_s, list(tokens)

    def __repr__(self):
        return f"Hit({self.label!r}, {self.start_s:.2f}-{self.end_s:.2f}s)"


# The model has dead zones: a keyword that starts about 1.3-1.6 s after the start of the audio fed to it is missed
# (and again 1.5 s later, 2.8-3.1 s), whatever the level or the speaker; the plain recogniser reads the same audio
# correctly. Measured on 4 synthesised voices, the start of the keyword swept in 50 ms steps over 4 s. So the audio is
# scanned twice, the second time with this much silence put in front: a keyword is in a dead zone in at most one pass.
PASS_SHIFTS_S = (0.0, 0.7)
# Quiet recordings are raised before the model sees them, as the recogniser's own front end does (waggle/stt.py): the
# webcam microphone gives speech at about 1% of full scale. At most MAX_GAIN, never lowered below the level given.
QUIET_BELOW, TARGET, MAX_GAIN = 0.1, 0.3, 30.0


def leveled(a):
    """Samples (float32, -1..1) raised to a level the model reads well when they are quiet; louder ones unchanged."""
    import numpy as np
    if not len(a):
        return a
    loud = float(np.percentile(np.abs(a), 99.0))
    if loud <= 0.0 or loud >= QUIET_BELOW:
        return a
    return np.clip(a * min(TARGET / loud, MAX_GAIN), -1.0, 1.0).astype(np.float32)


class Spotter:
    """The loaded model with its keywords. One per process; scan() is called from one thread."""
    def __init__(self, files, keywords, *, num_threads=1):
        import sherpa_onnx
        self.keywords = keywords
        self.busy_s = 0.0  # time spent inside the model (CPU measurement)
        self.scans = 0
        with tempfile.TemporaryDirectory(prefix="waggle-kws-") as tmp:  # the constructor wants a file; read once
            kf = Path(tmp) / "keywords.txt"
            kf.write_text(keywords, encoding="utf-8")
            self._kws = sherpa_onnx.KeywordSpotter(
                tokens=str(files["tokens"]), encoder=str(files["encoder"]), decoder=str(files["decoder"]),
                joiner=str(files["joiner"]), keywords_file=str(kf), num_threads=num_threads, sample_rate=RATE,
                feature_dim=80, provider="cpu")

    def scan(self, pcm, *, shifts=PASS_SHIFTS_S, level=True):
        """Hits in `pcm` (16-bit little-endian mono, 16 kHz bytes), earliest first. Each pass feeds the audio plus a
        silent tail; a keyword heard in more than one pass is reported once."""
        import numpy as np
        t0 = time.perf_counter()
        a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if level:
            a = leveled(a)
        hits = []
        for shift in shifts:
            x = np.concatenate([np.zeros(int(shift * RATE), dtype=np.float32), a,
                                np.zeros(int(SOUND_TAIL_S * RATE), dtype=np.float32)])
            for h in self._one(x):
                h.start_s, h.end_s = h.start_s - shift, h.end_s - shift
                if not any(abs(h.start_s - o.start_s) < 0.4 for o in hits):
                    hits.append(h)
        self.scans += 1
        self.busy_s += time.perf_counter() - t0
        return sorted(hits, key=lambda h: h.start_s)

    def _one(self, x):
        sp, st, out = self._kws, self._kws.create_stream(), []
        st.accept_waveform(RATE, x)
        st.input_finished()
        while sp.is_ready(st):
            sp.decode_stream(st)
            r = sp.keyword_spotter.get_result(st)
            word = r.keyword.strip()
            if word:
                ts = [float(t) for t in r.timestamps]
                out.append(Hit(word, ts[0] if ts else 0.0, ts[-1] if ts else 0.0, r.tokens))
                sp.reset_stream(st)
        return out


# ---- loading (cached; a failure is remembered for a while so it is logged once, not per utterance) ---------

_RETRY_S = 300.0
_cache = {}
_lock = threading.Lock()


def load(names, threshold=DEFAULT_THRESHOLD, score=DEFAULT_SCORE, *, variants=True, greet=True, folder=None):
    """The shared Spotter for these wake names, or None when the model files, the sherpa-onnx package or a speakable
    name are missing (the reason is logged once per five minutes at most)."""
    key = (tuple(names), round(float(threshold), 4), round(float(score), 4), bool(variants), bool(greet), str(folder))
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and (hit[0] is not None or now - hit[1] < _RETRY_S):
            return hit[0]
        obj = None
        try:
            files = verified(folder)
            if files is None:
                log.warning("kws: no verified model in %s (python -m waggle.kws fetch)", model_dir(folder))
            else:
                keywords = keyword_lines(names, read_tokens(files["tokens"]), threshold, score, variants, greet)
                if not keywords:
                    log.warning("kws: no speakable wake name in %s", list(names))
                else:
                    obj = Spotter(files, keywords)
        except Exception as e:  # noqa: BLE001 (package missing, a damaged file ...: the caller falls back)
            log.warning("kws: cannot load (%s: %s)", type(e).__name__, e)
            obj = None
        _cache[key] = (obj, now)
        return obj


def reset():
    with _lock:
        _cache.clear()


# ---- command line -----------------------------------------------------------------------------------------

def status():
    out = {"dir": str(model_dir()), "verified": verified() is not None, "licence": ARCHIVE["licence"],
           "files": {k: {"name": n, "size": s} for k, (n, s, _) in FILES.items()},
           "present": {k: (model_dir() / n).exists() for k, (n, _, _) in FILES.items()}}
    try:
        import sherpa_onnx
        out["sherpa_onnx"] = sherpa_onnx.__version__
    except Exception as e:  # noqa: BLE001
        out["sherpa_onnx"] = f"missing ({type(e).__name__})"
    return out


def main(argv=None):
    import json
    ap = argparse.ArgumentParser(prog="python -m waggle.kws", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("fetch")
    t = sub.add_parser("test")
    t.add_argument("wav")
    t.add_argument("--names", nargs="*", default=["Waggle"])
    t.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = ap.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(status(), indent=2))
        return 0
    if args.cmd == "fetch":
        try:
            for k, p in fetch().items():
                print(f"{k}: {p}")
        except (KwsError, OSError) as e:
            print(f"FAILED: {e}", file=sys.stderr)
            return 1
        return 0
    from waggle import mic
    sp = load(args.names, args.threshold)
    if sp is None:
        print("the spotter could not be loaded (see the log line above)", file=sys.stderr)
        return 1
    src = mic.FileSource(args.wav)
    src.open()
    pcm = b""
    while (f := src.read(0.5)) is not None:
        pcm += f
    print(sp.scan(pcm) or "no hit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
