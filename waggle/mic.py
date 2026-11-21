"""The microphone, read in-process: 16 kHz mono 16-bit frames of 30 ms from the default recording device through
winmm's waveIn API (ctypes; no extra process, so nothing can flash a window, and the first frame arrives within
tens of milliseconds of opening). The device's name is read at run time (waveInGetDevCaps), never written down.

    with WaveInSource() as mic:            # opens the default recording device (WAVE_MAPPER)
        frame = mic.read(timeout=1.0)      # 960 bytes, or None when no frame came in time
    FileSource("speech.wav")               # the same interface over a WAV file (tests, and the P7 check: a
                                           # synthesised request is fed through the recording path)

Levels (level(), Endpointer) work on the raw 16-bit samples. The webcam microphone on the owner's PC is quiet
(a still room peaks near 0.4% of full scale), so the gate follows the room's own noise floor instead of using
one fixed threshold."""
import array
import collections
import io
import math
import os
import threading
import time
import wave
from pathlib import Path

RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2
TURN_FRAMES = 8000 // FRAME_MS  # the end-of-turn model reads the last 8 s


# ---- levels -----------------------------------------------------------------------------------------------

def samples(frame):
    a = array.array("h")
    a.frombytes(frame[: len(frame) - len(frame) % 2])
    return a


def level(frame):
    """(rms, peak) of one frame of 16-bit samples."""
    s = samples(frame)
    if not s:
        return 0.0, 0
    return math.sqrt(sum(x * x for x in s) / len(s)), max(max(s), -min(s))


def write_wav(path, pcm, rate=RATE):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return p


def wav_bytes(pcm, rate=RATE):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class Endpointer:
    """Finds one utterance in a stream of frames by energy. Speech starts when START_MS of frames are louder than
    the room's noise floor times START_RATIO (and above min_on); it ends after `end_silence_s` of frames below
    the lower bar (floor times END_RATIO, at least min_off), or at max_s. The noise floor is a low percentile of
    the recent quiet frames, so a louder room raises the bar by itself.

    The floor is the 20th percentile of every frame of the last FLOOR_WINDOW_S (speech included: the pauses
    between words keep it low, while a steady noise such as a fan becomes the floor and stops counting as
    speech). The first WARMUP_FRAMES only measure the room.

    feed(frame) -> None while listening or inside speech, "start" when speech begins, "end" when it is over.
    pcm() is the utterance with PREROLL_S of sound before the start."""
    START_RATIO, END_RATIO = 3.0, 2.0
    MIN_ON, MIN_OFF = 120.0, 80.0
    START_MS, PREROLL_S = 90, 0.3
    FLOOR_WINDOW_S, FLOOR_PERCENTILE = 5.0, 0.2
    WARMUP_FRAMES = 5

    ASK_EVERY_MS = 90  # while a pause lasts, the end-of-turn model is asked again this often

    def __init__(self, end_silence_s=1.2, max_s=30.0, floor=None, min_on=None, min_off=None, *, turn=None,
                 turn_max_silence_s=1.6, turn_pause_ms=200, turn_min_p=0.5):
        """`turn` (optional): an end-of-turn model with `complete(pcm) -> P(finished)`. With it, the utterance ends
        when a pause of at least `turn_pause_ms` is judged finished (P >= `turn_min_p`) or when the pause reaches
        `turn_max_silence_s`, whichever comes first; `end_silence_s` is then only the fallback if the model fails.
        Without it the utterance ends after `end_silence_s` of silence, as always."""
        self.end_silence_s, self.max_s = end_silence_s, max_s
        self.turn, self.turn_max_silence_s = turn, turn_max_silence_s
        self.turn_pause_ms, self.turn_min_p = turn_pause_ms, turn_min_p
        self.ended_by, self.voiced, self.turn_asks, self.turn_p = "", False, 0, None
        self.mode = "energy+silence"
        self._asked_at = -1
        self.min_on = self.MIN_ON if min_on is None else float(min_on)
        self.min_off = self.MIN_OFF if min_off is None else float(min_off)
        self._quiet = collections.deque(maxlen=int(self.FLOOR_WINDOW_S * 1000 / FRAME_MS))
        self._fixed_floor = floor
        self._pre = collections.deque(maxlen=max(1, int(self.PREROLL_S * 1000 / FRAME_MS)))
        self._loud_run = []
        self._frames, self.in_speech, self.done = [], False, False
        self._silent_ms, self.speech_ms, self.peak, self._sum_sq, self._n = 0, 0, 0, 0.0, 0
        self.levels = []  # (rms, peak) per frame, for reports

    def floor(self):
        if self._fixed_floor is not None:
            return self._fixed_floor
        if not self._quiet:
            return 0.0
        q = sorted(self._quiet)
        return q[int(len(q) * self.FLOOR_PERCENTILE)]

    def on_bar(self):
        return max(self.min_on, self.floor() * self.START_RATIO)

    def off_bar(self):
        return max(self.min_off, self.floor() * self.END_RATIO)

    def feed(self, frame):
        if self.done:
            return "end"
        rms, peak = level(frame)
        if len(self.levels) < 20000:
            self.levels.append((round(rms, 1), peak))
        self._quiet.append(rms)
        self._observe(frame, rms)
        if not self.in_speech:
            if self._hot(frame, rms):
                self._loud_run.append(frame)
                if len(self._loud_run) * FRAME_MS >= self.START_MS:
                    self.in_speech = True
                    self._frames = list(self._pre) + self._loud_run
                    self.speech_ms = len(self._loud_run) * FRAME_MS
                    self._loud_run = []
                    return "start"
                return None
            for f in self._loud_run:  # a short click: back into the pre-roll, not speech
                self._pre.append(f)
            self._loud_run = []
            self._pre.append(frame)
            return None
        self._frames.append(frame)
        self.peak = max(self.peak, peak)
        self._sum_sq += rms * rms
        self._n += 1
        if self._silent(frame, rms):
            self._silent_ms += FRAME_MS
            self.voiced = False
        else:
            self._silent_ms = 0
            self._asked_at = -1
            self.voiced = True
            self.speech_ms += FRAME_MS
        if self._finished():
            self.done = True
            return "end"
        return None

    # -- what counts as speech: the energy gate here, a neural one in SileroGate
    def _observe(self, frame, rms):
        pass

    def _hot(self, frame, rms):
        """Can this frame start speech?"""
        return rms >= self.on_bar() and len(self._quiet) > self.WARMUP_FRAMES

    def _silent(self, frame, rms):
        """Is this frame (inside speech) a pause?"""
        return rms < self.off_bar()

    def _finished(self):
        """Is the utterance over? Sets `ended_by`: "max length", "silence" or "turn" (the model said finished)."""
        if len(self._frames) * FRAME_MS >= self.max_s * 1000:
            self.ended_by = "max length"
            return True
        if self.turn is None:
            if self._silent_ms >= self.end_silence_s * 1000:
                self.ended_by = "silence"
                return True
            return False
        if self._silent_ms < self.turn_pause_ms:
            return False
        if self._silent_ms >= self.turn_max_silence_s * 1000:
            self.ended_by = "silence"
            return True
        if self._asked_at < 0 or self._silent_ms - self._asked_at >= self.ASK_EVERY_MS:
            self._asked_at = self._silent_ms
            try:
                self.turn_p = self.turn.complete(b"".join(self._frames[-TURN_FRAMES:]))
                self.turn_asks += 1
            except Exception:  # noqa: BLE001 (the model failed: back to the fixed silence for this utterance)
                self.turn = None
                return self._finished()
            if self.turn_p >= self.turn_min_p:
                self.ended_by = "turn"
                return True
        return False

    @property
    def silence_s(self):
        """The silence at the end of what was heard so far: how long the endpointer waited to be sure."""
        return self._silent_ms / 1000.0

    def speech_rms(self):
        return math.sqrt(self._sum_sq / self._n) if self._n else 0.0

    def pcm(self, keep_tail_s=0.3):
        """The utterance: pre-roll, speech, and at most keep_tail_s of the closing silence."""
        drop = max(0, self._silent_ms - int(keep_tail_s * 1000)) // FRAME_MS
        frames = self._frames[: len(self._frames) - drop] if drop else self._frames
        return b"".join(frames)


class SileroGate(Endpointer):
    """The same endpointer with a neural speech detector deciding what is speech: a frame starts speech when Silero
    VAD gives it P(speech) >= START_P (held for START_MS) AND it is at least `min_on` loud (the same level floor as the
    energy gate, so a distant television in another room does not become a new source of utterances), and counts as a
    pause only when P < END_P (hysteresis in between, as the Silero authors recommend). Music, a steady fan, typing and
    thumps that the energy gate takes for speech do not start an utterance, and once speaking, a pause is a pause
    whatever the room sounds like (the energy gate never sees one while music plays). `stream` is a
    vad.SileroStream (one per microphone stream)."""
    START_P, END_P = 0.5, 0.35
    PREROLL_S = 0.5  # the model needs a moment of a soft first word before it is sure: keep more sound before the start
    START_MS = 60  # two frames: the model is the judge, and its first window of a word is often still below 0.5

    def __init__(self, stream, *args, **kw):
        super().__init__(*args, **kw)
        self.stream, self.prob = stream, 0.0

    def _observe(self, frame, rms):
        try:
            self.prob = self.stream.feed(frame)
        except Exception:  # noqa: BLE001 (a failing model must not stop the microphone: this frame is not speech)
            self.prob = 0.0

    def _hot(self, frame, rms):
        return self.prob >= self.START_P and rms >= self.min_on and len(self._quiet) > self.WARMUP_FRAMES

    def _silent(self, frame, rms):
        return self.prob < self.END_P


def make_endpointer(end_silence_s=1.2, max_s=30.0, min_on=None, min_off=None, *, vad="energy", endturn="silence",
                    turn_max_silence_s=1.6, turn_pause_ms=200, turn_min_p=0.5, note=None):
    """An endpointer for the settings pc_voice_vad ("silero" | "energy") and pc_voice_endturn ("smart" | "silence").
    A model that is not available (file missing or not the pinned one, onnxruntime missing) means the energy gate
    and the fixed silence, with `note(text)` told once per call. The returned object has `.mode`, for example
    "silero+smart" or "energy+silence"."""
    from waggle import vad as vadmod
    stream = turn = None
    if str(vad).lower() == "silero":
        model = vadmod.silero()
        if model is not None:
            try:
                stream = model.stream()
            except Exception as e:  # noqa: BLE001
                model = None
                if note:
                    note(f"silero stream: {type(e).__name__}: {e}")
        if model is None and note:
            note("silero VAD unavailable: using the energy gate")
    if str(endturn).lower() == "smart":
        turn = vadmod.smart_turn()
        if turn is None and note:
            note("smart turn unavailable: using the fixed silence")
    kw = dict(end_silence_s=end_silence_s, max_s=max_s, min_on=min_on, min_off=min_off, turn=turn,
              turn_max_silence_s=turn_max_silence_s, turn_pause_ms=turn_pause_ms, turn_min_p=turn_min_p)
    ep = SileroGate(stream, **kw) if stream is not None else Endpointer(**kw)
    ep.mode = ("silero" if stream is not None else "energy") + "+" + ("smart" if turn is not None else "silence")
    return ep


# ---- sources ----------------------------------------------------------------------------------------------

class FileSource:
    """Frames from a WAV file (converted to 16 kHz mono when it is not), with `lead_s` of silence before and
    `trail_s` after, then None. realtime=True paces the frames as a microphone would."""

    def __init__(self, path, *, realtime=False, trail_s=3.0, lead_s=0.0):
        self.path, self.realtime, self.trail_s, self.lead_s = Path(path), realtime, trail_s, lead_s
        self._data, self._pos, self._t0 = b"", 0, None
        self.name = f"file:{self.path.name}"

    def open(self):
        with wave.open(str(self.path), "rb") as w:
            ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
        if width != 2:
            raise ValueError("only 16-bit WAV files")
        s = array.array("h")
        s.frombytes(raw)
        if ch > 1:
            s = array.array("h", (int(sum(s[i:i + ch]) / ch) for i in range(0, len(s), ch)))
        if rate != RATE:
            n = int(len(s) * RATE / rate)
            s = array.array("h", (s[min(len(s) - 1, int(i * rate / RATE))] for i in range(n)))
        self._data = b"\x00\x00" * int(RATE * self.lead_s) + s.tobytes() + b"\x00\x00" * int(RATE * self.trail_s)
        self._pos, self._t0 = 0, time.monotonic()
        return self

    def read(self, timeout=1.0):
        if self._pos >= len(self._data):
            time.sleep(min(timeout, 0.02))  # like a microphone that has nothing: the caller does not spin
            return None
        if self.realtime:
            wait = self._t0 + (self._pos / 2 + FRAME_SAMPLES) / RATE - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        frame = self._data[self._pos:self._pos + FRAME_BYTES]
        self._pos += FRAME_BYTES
        return frame.ljust(FRAME_BYTES, b"\x00")

    def flush(self):
        return 0

    def close(self):
        pass

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class WAVEFORMATEX(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                    ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                    ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]

    class WAVEHDR(ctypes.Structure):
        _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                    ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t), ("dwFlags", wintypes.DWORD),
                    ("dwLoops", wintypes.DWORD), ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]

    class WAVEINCAPSW(ctypes.Structure):
        _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                    ("szPname", wintypes.WCHAR * 32), ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                    ("wReserved1", wintypes.WORD)]

    _winmm = ctypes.WinDLL("winmm")
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _HWAVEIN = wintypes.HANDLE
    _winmm.waveInOpen.argtypes = (ctypes.POINTER(_HWAVEIN), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX),
                                  ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD)
    for _name in ("waveInPrepareHeader", "waveInUnprepareHeader", "waveInAddBuffer"):
        getattr(_winmm, _name).argtypes = (_HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT)
    for _name in ("waveInStart", "waveInStop", "waveInReset", "waveInClose"):
        getattr(_winmm, _name).argtypes = (_HWAVEIN,)
    _winmm.waveInGetNumDevs.restype = wintypes.UINT
    _winmm.waveInGetDevCapsW.argtypes = (ctypes.c_size_t, ctypes.POINTER(WAVEINCAPSW), wintypes.UINT)
    _k32.CreateEventW.restype = wintypes.HANDLE
    _k32.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
    _k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    WAVE_MAPPER = 0xFFFFFFFF
    CALLBACK_EVENT = 0x00050000
    WHDR_DONE = 0x1


def device_names():
    """The names of the recording devices Windows offers (waveIn, at most 31 characters each)."""
    if os.name != "nt":
        return []
    out = []
    for i in range(_winmm.waveInGetNumDevs()):
        caps = WAVEINCAPSW()
        if _winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            out.append(caps.szPname)
    return out


class WaveInSource:
    """The default recording device through waveIn, with BUFFERS frames queued (3 s), so a pause while the caller
    does something else (a transcription) loses nothing shorter than that."""
    BUFFERS = 100

    def __init__(self, device=None):
        if os.name != "nt":
            raise OSError("the microphone is read through the Windows waveIn API")
        self.device = WAVE_MAPPER if device is None else int(device)
        self._h, self._event, self._hdrs, self._bufs, self._next = None, None, [], [], 0
        names = device_names()
        if device is None:
            self.name = names[0] if names else ""
        else:
            self.name = names[int(device)] if int(device) < len(names) else ""

    def open(self):
        fmt = WAVEFORMATEX(1, 1, RATE, RATE * 2, 2, 16, 0)
        self._event = _k32.CreateEventW(None, False, False, None)
        h = _HWAVEIN()
        rc = _winmm.waveInOpen(ctypes.byref(h), self.device, ctypes.byref(fmt), self._event, 0, CALLBACK_EVENT)
        if rc != 0:
            _k32.CloseHandle(self._event)
            self._event = None
            raise OSError(f"waveInOpen failed (MMRESULT {rc}): no usable microphone")
        self._h = h
        self._bufs = [ctypes.create_string_buffer(FRAME_BYTES) for _ in range(self.BUFFERS)]
        self._hdrs = []
        for b in self._bufs:
            hdr = WAVEHDR(ctypes.cast(b, ctypes.c_void_p), FRAME_BYTES, 0, 0, 0, 0, None, 0)
            _winmm.waveInPrepareHeader(self._h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            _winmm.waveInAddBuffer(self._h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self._hdrs.append(hdr)
        self._next = 0
        if _winmm.waveInStart(self._h) != 0:
            self.close()
            raise OSError("waveInStart failed")
        return self

    def read(self, timeout=1.0):
        """The next 30 ms frame in order, or None when none arrived within `timeout` seconds."""
        if self._h is None:
            return None
        deadline = time.monotonic() + timeout
        hdr = self._hdrs[self._next]
        while not (hdr.dwFlags & WHDR_DONE):
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            _k32.WaitForSingleObject(self._event, max(1, int(min(left, 0.1) * 1000)))
        data = ctypes.string_at(hdr.lpData, hdr.dwBytesRecorded)
        hdr.dwFlags &= ~WHDR_DONE
        hdr.dwBytesRecorded = 0
        _winmm.waveInAddBuffer(self._h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        self._next = (self._next + 1) % len(self._hdrs)
        return data.ljust(FRAME_BYTES, b"\x00")

    def flush(self):
        """Drop every frame already recorded (for example Waggle's own voice, heard while it spoke)."""
        n = 0
        while self._h is not None and self._hdrs[self._next].dwFlags & WHDR_DONE:
            self.read(0)
            n += 1
        return n

    def close(self):
        if self._h is None:
            return
        _winmm.waveInReset(self._h)
        for hdr in self._hdrs:
            _winmm.waveInUnprepareHeader(self._h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        _winmm.waveInClose(self._h)
        _k32.CloseHandle(self._event)
        self._h, self._event, self._hdrs, self._bufs = None, None, [], []

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()


def capture(seconds, source=None):
    """`seconds` of sound from the microphone (or `source`) as raw 16-bit PCM, kept in memory only."""
    src = source or WaveInSource()
    out = bytearray()
    with src:
        while len(out) < seconds * RATE * 2:
            f = src.read(1.0)
            if f is None:
                break
            out += f
    return bytes(out)


def report(pcm):
    """Levels of a capture: frames, rms median / p90 / max and the peak, as fractions of full scale too."""
    frames = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
    lv = [level(f) for f in frames]
    if not lv:
        return {"frames": 0}
    r = sorted(x[0] for x in lv)
    peak = max(x[1] for x in lv)
    return {"frames": len(lv), "seconds": round(len(pcm) / 2 / RATE, 2), "rms_median": round(r[len(r) // 2], 1),
            "rms_p90": round(r[int(len(r) * 0.9)], 1), "rms_max": round(r[-1], 1), "peak": peak,
            "peak_fraction": round(peak / 32767, 4)}
