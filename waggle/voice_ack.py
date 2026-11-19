"""Spoken fillers for voice conversations: if an answer takes more than ~2 s, say "One sec." like a person would.

Phrases are synthesised once with the warm Kokoro server (same voice as replies) and cached as WAV files, so
playing one is instant (winsound, async, stdlib). The filler is cancelled the moment the real reply is ready,
and stops any filler still playing so it never talks over the answer.
"""
import json
import os
import random
import threading
import urllib.request

from .config import data_path

PHRASES = {
    "wait": ["One sec.", "Let me check.", "Give me a moment.", "Hmm, one moment."],
    "act": ["On it.", "Working on it.", "Doing that now."],
    "mail": ["Checking.", "Let me look."],
}
KOKORO = "http://127.0.0.1:8899/"


def _cache_dir():
    d = data_path("voice_cache")
    d.mkdir(exist_ok=True)
    return d


def _file(text):
    return _cache_dir() / ("".join(c for c in text.lower() if c.isalnum() or c == " ").strip().replace(" ", "-")
                           + ".wav")


def prepare(voice="bm_daniel", speed=1.25, post=None):
    """Synthesise any missing filler phrases (needs the Kokoro server). Returns how many were made."""
    made = 0
    for texts in PHRASES.values():
        for t in texts:
            f = _file(t)
            if f.exists() and f.stat().st_size > 1000:
                continue
            body = json.dumps({"text": t, "out": str(f), "voice": voice, "speed": speed}).encode()
            try:
                if post:
                    post(body)
                else:
                    req = urllib.request.Request(KOKORO, data=body, headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=20).read()
                made += 1
            except Exception:
                return made
    return made


def _play(path):
    if os.name != "nt" or not path.exists():
        return False
    import winsound
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    return True


def stop():
    if os.name == "nt":
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)


def say(kind="wait"):
    options = [p for p in PHRASES.get(kind, PHRASES["wait"]) if _file(p).exists()]
    return _play(_file(random.choice(options))) if options else False


class Filler:
    """Speak a filler if the turn is still running after `delay` seconds. .now(kind) speaks immediately (once)."""

    def __init__(self, delay=2.0, enabled=True, player=None):
        self.enabled, self.spoken, self.done = enabled, False, False
        self._lock = threading.Lock()
        self._say = player or say
        self._timer = threading.Timer(delay, self._fire, args=("wait",)) if enabled else None
        if self._timer:
            self._timer.daemon = True
            self._timer.start()

    def _fire(self, kind):
        with self._lock:
            if self.done or self.spoken or not self.enabled:
                return
            self.spoken = True
        self._say(kind)

    def now(self, kind):
        self._fire(kind)

    def cancel(self):
        with self._lock:
            self.done = True
        if self._timer:
            self._timer.cancel()
