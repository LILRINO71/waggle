"""Speaking through the PC: WAV files played on the default output device, one utterance at a time, queued,
with a way to cut it off at once. No window and no extra process: the standard library's winsound (PlaySound,
asynchronous), which plays a PCM WAV and stops when asked.

    sp = Speaker().start()
    item = sp.say("reply.wav", delete=True)      # queued; plays after what is already playing
    item.done.wait()                             # item.cut is True when it was cut off
    sp.stop()                                    # cut the current one off now and drop the queue
    sp.speaking                                  # True while something plays or waits

The backend is a seam: an object with play(path) (starts playing and returns at once) and stop(). The length of
each WAV comes from its header, so the speaker knows when an utterance has finished without polling the device.

Tones: tone_wav(path, freq, ms, amp) writes a short sine with soft edges (amp is a fraction of full scale; the
listening tones use 0.06, well under a normal speaking voice)."""
import logging
import math
import queue
import struct
import threading
import time
import wave
from pathlib import Path

log = logging.getLogger("waggle.speaker")

TAIL_S = 0.06  # the device's own latency: an utterance counts as finished this long after its last sample


def wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


def tone_wav(path, freq=660.0, ms=120, amp=0.06, rate=16000, fade_ms=15):
    """A short quiet sine tone as a 16-bit mono WAV at `path` (written atomically). Returns the path."""
    n = int(rate * ms / 1000)
    fade = max(1, int(rate * fade_ms / 1000))
    frames = bytearray()
    for i in range(n):
        edge = min(1.0, i / fade, (n - 1 - i) / fade)
        frames += struct.pack("<h", int(32767 * amp * edge * math.sin(2 * math.pi * freq * i / rate)))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    tmp.replace(p)
    return p


class WinsoundBackend:
    """PlaySound from the standard library: asynchronous, from a file, never the system's default beep."""

    def __init__(self):
        import winsound
        self._ws = winsound
        self._flags = winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT

    def play(self, path):
        self._ws.PlaySound(str(path), self._flags)

    def stop(self):
        self._ws.PlaySound(None, 0)


class Item:
    def __init__(self, path, delete=False, label=""):
        self.path, self.delete, self.label = Path(path), bool(delete), label
        self.done, self.cut, self.error, self.seconds = threading.Event(), False, "", 0.0
        self.started = None  # time.monotonic() when it began to play


class Speaker:
    """One utterance at a time from a queue; stop() cuts the current one off and drops the rest."""

    def __init__(self, backend=None, *, tail_s=TAIL_S, on_start=None):
        self.backend = backend
        self.tail_s = tail_s
        self.on_start = on_start  # called with the Item when it starts to play (for timing)
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._cut = threading.Event()
        self._current = None
        self._pending = 0
        self.idle = threading.Event()
        self.idle.set()
        self._thread = None

    def start(self):
        if self.backend is None:
            self.backend = WinsoundBackend()
        self._thread = threading.Thread(target=self._run, daemon=True, name="waggle-speaker")
        self._thread.start()
        return self

    @property
    def speaking(self):
        with self._lock:
            return self._current is not None or self._pending > 0

    def say(self, path, *, delete=False, label=""):
        item = Item(path, delete, label)
        with self._lock:
            self._pending += 1
            self.idle.clear()
        self._q.put(item)
        return item

    def play_now(self, path, *, wait=True, timeout=10.0, label=""):
        """Cut off whatever plays, drop the queue, play `path` (a tone), and by default wait for it to end."""
        self.stop()
        item = self.say(path, label=label)
        if wait:
            item.done.wait(timeout)
        return item

    def stop(self):
        """Cut the current utterance off at once and drop every queued one. Returns how many were affected."""
        dropped = []
        with self._lock:
            while True:
                try:
                    it = self._q.get_nowait()
                except queue.Empty:
                    break
                if it is None:
                    self._q.put(None)
                    break
                self._pending -= 1
                dropped.append(it)
            had = self._current is not None
            if had:
                self._cut.set()
            if self._current is None and self._pending == 0:
                self.idle.set()
        for it in dropped:
            it.cut = True
            self._finish(it)
        return len(dropped) + int(had)

    def wait_idle(self, timeout=None):
        return self.idle.wait(timeout)

    def close(self):
        self.stop()
        self._q.put(None)
        if self._thread:
            self._thread.join(5)

    def _finish(self, item):
        if item.delete:
            for _ in range(20):  # the device may hold the file for a moment after a stop
                try:
                    item.path.unlink(missing_ok=True)
                    break
                except OSError:
                    time.sleep(0.05)
        item.done.set()

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            with self._lock:
                self._pending -= 1
                self._current = item
                self._cut.clear()
            try:
                item.seconds = wav_seconds(item.path)
                item.started = time.monotonic()
                self.backend.play(item.path)
                if self.on_start:
                    try:
                        self.on_start(item)
                    except Exception:
                        pass
                if self._cut.wait(item.seconds + self.tail_s):
                    item.cut = True
                    self.backend.stop()
            except Exception as e:  # a bad file or a device error: the next utterance still plays
                item.error = f"{type(e).__name__}: {e}"[:200]
                log.warning("speaker: %s", item.error)
            finally:
                with self._lock:
                    self._current = None
                    if self._pending == 0:
                        self.idle.set()
                self._finish(item)
