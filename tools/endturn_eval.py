"""Measure the PC voice's speech gate and end-of-turn detection offline, without a microphone or a speaker.

    python tools/endturn_eval.py synth --work DIR [--kokoro-url http://127.0.0.1:8899]
    python tools/endturn_eval.py run   --work DIR --models MODELS_DIR [--min-p 0.5] [--json out.json]
    python tools/endturn_eval.py tune  --work DIR --models MODELS_DIR

synth  builds the test set with Waggle's own speech server (Kokoro): complete requests, requests with a thinking
       pause in the middle ("remind me to ... um ... call grandma tomorrow", pauses of 0.5-1.5 s), and a few
       stress cases with a 1.8-2.2 s pause. Two sets: "dev" (used to pick the end-of-turn threshold) and "test"
       (other sentences, other voices; the numbers that are reported). Each is written as a 16 kHz mono WAV with the
       true speech spans in manifest.json.
run    mixes every utterance with a room tone and with noise (fan, pink noise, synthetic music) at a few signal to
       noise ratios, runs four endpointers over it frame by frame as the PC voice does, and reports, per endpointer:
         cut-offs      the turn ended before the last words (the words after the pause are lost)
         start clipped the captured audio starts after the first word began
         missed        no turn was found at all
         stuck         the turn never ended within 4 s after the last word (noise kept the gate open)
         delay         seconds from the last word to the end of the turn, for turns that were not cut off
       and then the false starts of each endpointer on 10 minutes-equivalent of noise with nobody speaking.
tune   runs the dev set at several end-of-turn thresholds and pauses.
words  lost words, with the recogniser (needs faster-whisper and its cached "small" model): the captured audio of A
       and B is transcribed and compared with the whole clean sentence.
Endpointers: A energy+silence (today), B silero+silence, C energy+smart, D silero+smart (the new one).
Only numpy and onnxruntime are needed (Python 3.14 here); Kokoro is only used by `synth`."""
import argparse
import json
import os
import sys
import time
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RATE = 16000
FRAME = 480
LEAD_S, TRAIL_S = 1.5, 4.0
SPEECH_RMS = 700.0  # int16 units: a normal voice at the quiet webcam microphone's usual level
QUIET_RMS = 150.0  # a quiet voice, just above the energy gate's 120

# ---- the sentences -------------------------------------------------------------------------------------------

COMPLETE = {
    "dev": ["What's on my calendar today?", "Remind me to call grandma tomorrow at five.",
            "Turn the volume down a little.", "How long until my next class?",
            "Add milk and eggs to the shopping list.", "Tell me a joke.",
            "Set a timer for ten minutes.", "Read me my unread emails.", "Yes.", "Good morning."],
    "test": ["What's the weather like this weekend?", "Open my notes from yesterday.",
             "Send a message to my mom that I'll be late tonight.", "Summarize the article I saved this morning.",
             "Please remind me to submit the physics lab report before midnight.",
             "Hey Waggle, what's the score of the game tonight?", "Switch to the robotics project.",
             "What time does the library close on Friday?", "No, thanks.", "Stop.",
             "Waggle, what time is it?", "Add a reminder to email Mister Jones about the homework.",
             "How many days are left until the competition?", "Play some quiet music.",
             "What did I ask you a minute ago?", "Cancel my alarm for tomorrow morning.",
             "Tell me what's on my to do list for the weekend.", "Okay, that sounds good.",
             "Where did I save the robot chassis drawings?", "Remind me to water the plants when I get home."],
}
# (first part, second part, pause in seconds): the speaker stops in the middle of a request
HESITATE = {
    "dev": [("Remind me to", "call grandma tomorrow.", 0.6), ("Remind me to, um,", "call grandma tomorrow.", 0.9),
            ("Add", "milk and eggs to the shopping list.", 1.1), ("Set a timer for", "ten minutes.", 1.3),
            ("What's the weather like", "this weekend?", 0.8), ("I want you to find, uh,", "the file about the robot chassis.", 1.45),
            ("Can you tell me", "when my next class starts?", 1.0), ("Open the notes from", "yesterday's meeting.", 0.7),
            ("Turn the volume", "down a little.", 1.2), ("What is", "the capital of Australia?", 1.4)],
    "test": [("Waggle, remind me to", "email Mister Jones about the homework.", 0.5),
             ("Okay so, let me think,", "add a reminder for Friday.", 1.0), ("Summarize the article, hmm,", "about the new battery research.", 1.2),
             ("I need to, you know,", "finish my essay by Sunday, remind me.", 1.4),
             ("Send a message to my mom that", "I'll be late tonight.", 0.8), ("Call", "my dad.", 0.6),
             ("Please add to my calendar, um,", "dentist appointment on Tuesday at four.", 1.5),
             ("What's on my schedule for", "the day after tomorrow?", 1.1), ("Hey Waggle, um,", "what's the score tonight?", 0.9),
             ("Tell me the", "weather for Saturday.", 1.3), ("Remind me to take out the trash and, uh,", "feed the dog.", 1.5),
             ("Open the file about", "the intake design.", 0.5), ("Let me see, I want to ask about", "my homework due Monday.", 1.2),
             ("Search for, hmm,", "pickleball paddles.", 1.0), ("I was wondering if you could", "set an alarm for six thirty.", 0.7),
             ("Remind me to buy a birthday present for", "my sister next week.", 1.4),
             ("Can you check", "if the library is open?", 0.9), ("Add to my list, um, let's see,", "new batteries for the controller.", 1.5),
             ("Waggle, what is", "the plan for tomorrow?", 0.6), ("Read me", "the last email from school.", 1.3),
             ("Add a reminder to", "pick up the package on Wednesday.", 0.8), ("Turn on the", "lights in the living room.", 1.1),
             ("I think I need to, um,", "reschedule my dentist appointment.", 1.4), ("Show me the", "latest scores from the match.", 0.6),
             ("Please look up the, uh,", "opening hours of the museum.", 1.5), ("Wake me up at", "seven tomorrow morning.", 1.0)],
}
STRESS = [("Remind me to", "call grandma tomorrow.", 1.8), ("Add, um,", "milk to the shopping list.", 2.2),
          ("What's the", "weather like tomorrow?", 1.9), ("Set a timer for, hmm,", "fifteen minutes.", 2.0)]
VOICES = {"dev": ["bm_george", "af_heart"], "test": ["am_adam", "bf_emma", "af_bella", "bm_lewis"]}
SPEEDS = [1.0, 1.15]


# ---- audio helpers --------------------------------------------------------------------------------------------

def np():
    import numpy
    return numpy


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        raw, rate, ch = w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
    a = np().frombuffer(raw, dtype="<i2").astype(np().float64) / 32768.0
    return a.reshape(-1, ch).mean(axis=1) if ch > 1 else a, rate


def resample(a, src, dst):
    if src == dst:
        return a
    n = len(a)
    m = int(round(n * dst / src))
    spec = np().fft.rfft(a)
    out = np().zeros(m // 2 + 1, dtype=complex)
    k = min(len(spec), len(out))
    out[:k] = spec[:k]
    return np().fft.irfft(out, m) * (m / n)


def trim(a, floor=0.01):
    idx = np().nonzero(np().abs(a) > floor * max(1e-9, np().abs(a).max()))[0]
    return a[idx[0]: idx[-1] + 1] if len(idx) else a


def write_wav16(path, a):
    pcm = np().clip(np().round(a * 32767), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def say(url, text, voice, speed, out):
    req = urllib.request.Request(url, data=json.dumps({"text": text, "out": str(out), "voice": voice, "speed": speed}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read())
    if not body.get("ok"):
        raise RuntimeError(body)
    a, rate = read_wav(out)
    return trim(resample(a, rate, RATE))


FILLERS = {"um", "uh", "hmm", "er", "ur", "ah", "erm", "uhm", "hum", "mm"}
_asr = []


def asr():
    """faster-whisper (small, CPU, int8, from the local cache) used to find word boundaries and to check cuts."""
    if not _asr:
        from faster_whisper import WhisperModel
        _asr.append(WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True))
    return _asr[0]


def _norm(w):
    import re
    return re.sub(r"[^a-z0-9']", "", w.lower())


def _words(text):
    return [w for w in (_norm(x) for x in text.split()) if w]


def heard(audio):
    segs, _ = asr().transcribe(np().asarray(audio, dtype="float32"), language="en", beam_size=1,
                               condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segs).strip()


def split_point(full, first, second):
    """Where the first part of a sentence ends inside the whole sentence's audio: Whisper's word times give the gap
    between the last word of `first` and the first word of `second`, and the cut is the quietest 10 ms in it."""
    import difflib
    segs, _ = asr().transcribe(full.astype("float32"), language="en", beam_size=1, word_timestamps=True,
                               condition_on_previous_text=False)
    words = [(_norm(w.word), w.start, w.end) for s in segs for w in s.words if _norm(w.word)]
    exp = _words(first + " " + second)
    na = len(_words(first))
    sm = difflib.SequenceMatcher(a=exp, b=[w[0] for w in words], autojunk=False)
    mapping = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            mapping[blk.a + k] = blk.b + k
    before = [k for k in mapping if k <= na - 1]
    after = [k for k in mapping if k >= na]
    if not before or not after:
        return None
    ia, ib = mapping[max(before)], mapping[min(after)]
    if ib <= ia:
        return None
    lo, hi = words[ia][2] - 0.04, words[ib][1] + 0.04
    win = RATE // 100
    k0, k1 = max(1, int(lo * RATE) // win), max(2, int(hi * RATE) // win)
    n = len(full) // win
    rms = np().sqrt((full[: n * win].reshape(n, win) ** 2).mean(axis=1))
    k = k0 + int(np().argmin(rms[k0:min(k1 + 1, n)]))
    return k * win + win // 2


def split_ok(a_audio, b_audio, first, second):
    """Did the cut land where it should? The first clip must end with the last word of `first` and the second begin
    with the first word of `second` (as Whisper hears them, ignoring fillers)."""
    import difflib

    def close(x, y):
        return difflib.SequenceMatcher(a=x, b=y).ratio() >= 0.6
    ha = [w for w in _words(heard(a_audio)) if w not in FILLERS]
    hb = [w for w in _words(heard(b_audio)) if w not in FILLERS]
    ea = [w for w in _words(first) if w not in FILLERS]
    eb = [w for w in _words(second) if w not in FILLERS]
    return bool(ha and hb and ea and eb and close(ha[-1], ea[-1]) and close(hb[0], eb[0]))


def fade(a, ms=6):
    m = min(len(a) // 2, int(ms * RATE / 1000))
    a = a.copy()
    if m:
        a[:m] *= np().linspace(0, 1, m)
        a[-m:] *= np().linspace(1, 0, m)
    return a


def synth(work, url):
    """kind complete: one request. kind hesitate: the whole sentence is spoken in one go (so the first part keeps the
    intonation of a sentence that goes on) and cut at the word boundary, with the pause put in. kind fragment: the
    two parts are spoken as separate sentences (each ends like a finished one: the hard case for an end-of-turn
    model). kind stress: like hesitate with a pause above the 1.6 s limit."""
    work = Path(work)
    (work / "wav").mkdir(parents=True, exist_ok=True)
    tmp = work / "_kokoro.wav"
    manifest = []
    n = 0
    items = []
    for sset in ("dev", "test"):
        for i, t in enumerate(COMPLETE[sset]):
            items.append((sset, "complete", (t,), 0.0, VOICES[sset][i % len(VOICES[sset])], SPEEDS[(i // 2) % 2]))
        for i, (a, b, g) in enumerate(HESITATE[sset]):
            for kind in ("hesitate", "fragment"):
                items.append((sset, kind, (a, b), g, VOICES[sset][(i + 1) % len(VOICES[sset])], SPEEDS[i % 2]))
    for i, (a, b, g) in enumerate(STRESS):
        items.append(("test", "stress", (a, b), g, VOICES["test"][i % 4], 1.0))
    for sset, kind, parts, gap, voice, speed in items:
        if kind in ("hesitate", "stress"):
            full = say(url, " ".join(parts), voice, speed, tmp)
            cut = split_point(full, parts[0], parts[1])
            if cut is None:
                print("DROPPED (no word boundary found):", parts)
                continue
            clips = [trim(fade(full[:cut])), trim(fade(full[cut:]))]
            if not split_ok(clips[0], clips[1], parts[0], parts[1]):
                print("DROPPED (the cut is not at the intended word):", parts)
                continue
        else:
            clips = [say(url, p, voice, speed, tmp) for p in parts]
        audio, spans, pos = [], [], 0
        for j, c in enumerate(clips):
            if j:
                audio.append(np().zeros(int(gap * RATE)))
                pos += int(gap * RATE)
            audio.append(c)
            spans.append([pos / RATE, (pos + len(c)) / RATE])
            pos += len(c)
        a = np().concatenate(audio)
        name = f"{sset}-{kind}-{n:03d}"
        write_wav16(work / "wav" / f"{name}.wav", a / max(1e-9, np().abs(a).max()) * 0.5)
        manifest.append({"id": name, "set": sset, "kind": kind, "text": " ... ".join(parts), "voice": voice,
                         "speed": speed, "gap": gap, "spans": spans, "seconds": round(len(a) / RATE, 2)})
        n += 1
        print(name, round(len(a) / RATE, 1), "s", flush=True)
    tmp.unlink(missing_ok=True)
    (work / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"{len(manifest)} utterances in {work}")


# ---- noise ----------------------------------------------------------------------------------------------------

def _shaped(n, seed, shape):
    rng = np().random.default_rng(seed)
    spec = np().fft.rfft(rng.standard_normal(n))
    f = np().fft.rfftfreq(n, 1 / RATE)
    spec *= shape(np().maximum(f, 20.0))
    x = np().fft.irfft(spec, n)
    return x / np().sqrt((x ** 2).mean())


def noise(kind, seconds, seed):
    """Unit-RMS noise of the given kind."""
    n = int(seconds * RATE)
    t = np().arange(n) / RATE
    rng = np().random.default_rng(seed)
    if kind == "white":
        return rng.standard_normal(n)
    if kind == "pink":
        return _shaped(n, seed, lambda f: 1 / np().sqrt(f))
    if kind == "fan":  # low rumble with a hum and a slow wobble
        x = _shaped(n, seed, lambda f: 1 / f) + 0.6 * np().sin(2 * np().pi * 118 * t) + 0.3 * np().sin(2 * np().pi * 236 * t)
        x *= 1 + 0.15 * np().sin(2 * np().pi * 0.35 * t)
        return x / np().sqrt((x ** 2).mean())
    if kind == "music":  # a plucked melody over chords, a kick on the beat and a hi-hat (no voice)
        x = np().zeros(n)
        scale = [0, 2, 4, 7, 9, 12, 14, 16]
        chords = [[0, 4, 7], [5, 9, 12], [7, 11, 14], [2, 5, 9]]
        base = 220.0 * 2 ** (rng.integers(0, 5) / 12)
        for k in range(int(seconds / 0.5) + 1):
            s, e = int(k * 0.5 * RATE), min(n, int((k + 1) * 0.5 * RATE))
            tt = np().arange(e - s) / RATE
            f0 = base * 2 ** (scale[rng.integers(0, len(scale))] / 12)
            env = np().exp(-4 * tt)
            for h, amp in ((1, 1.0), (2, 0.5), (3, 0.3), (4, 0.15)):
                x[s:e] += amp * env * np().sin(2 * np().pi * f0 * h * tt)
            if k % 4 == 0:
                ch = chords[(k // 4) % 4]
                tt4 = np().arange(min(n, s + 2 * RATE) - s) / RATE
                for semi in ch:
                    x[s: s + len(tt4)] += 0.5 * np().sin(2 * np().pi * base / 2 * 2 ** (semi / 12) * tt4) * np().minimum(1, tt4 * 8) * np().exp(-0.8 * tt4)
            kick = np().arange(min(n - s, int(0.2 * RATE))) / RATE
            x[s: s + len(kick)] += 1.4 * np().sin(2 * np().pi * (55 + 90 * np().exp(-30 * kick)) * kick) * np().exp(-14 * kick)
            hat = rng.standard_normal(min(n - s - int(0.25 * RATE), int(0.04 * RATE)) if n - s > int(0.3 * RATE) else 0)
            if len(hat):
                x[s + int(0.25 * RATE): s + int(0.25 * RATE) + len(hat)] += 0.3 * hat * np().exp(-np().arange(len(hat)) / (0.01 * RATE))
        return x / np().sqrt((x ** 2).mean())
    if kind == "keys":  # typing: bursts of short clicks
        x = rng.standard_normal(n) * 0.01
        pos = 0.5
        while pos < seconds - 0.1:
            for _ in range(int(rng.integers(3, 12))):
                s = int(pos * RATE)
                m = min(int(0.02 * RATE), n - s)
                if m > 0:
                    x[s: s + m] += rng.standard_normal(m) * np().exp(-np().arange(m) / (0.004 * RATE)) * rng.uniform(0.5, 1.5)
                pos += rng.uniform(0.08, 0.22)
            pos += rng.uniform(1.0, 4.0)
        return x / np().sqrt((x ** 2).mean())
    if kind == "thumps":  # a door, a dropped book: a low ring every few seconds
        x = rng.standard_normal(n) * 0.005
        pos = 1.0
        while pos < seconds - 0.5:
            s = int(pos * RATE)
            m = min(int(0.4 * RATE), n - s)
            tt = np().arange(m) / RATE
            x[s: s + m] += np().sin(2 * np().pi * rng.uniform(90, 200) * tt) * np().exp(-9 * tt) + 0.3 * rng.standard_normal(m) * np().exp(-30 * tt)
            pos += rng.uniform(5, 12)
        return x / np().sqrt((x ** 2).mean())
    raise ValueError(kind)


def to_int16(a):
    return np().clip(np().round(a), -32768, 32767).astype("<i2")


def mix(item, wav, cond, seed):
    """The utterance placed in LEAD_S + TRAIL_S of background, per condition (kind, snr_db or None)."""
    kind, snr = cond
    speech_rms_target = QUIET_RMS if kind == "quiet" else SPEECH_RMS
    total = LEAD_S + item["seconds"] + TRAIL_S
    n = int(total * RATE)
    sig = np().zeros(n)
    a = wav.astype(np().float64)
    start = int(LEAD_S * RATE)
    sig[start: start + len(a)] = a
    mask = np().zeros(n, dtype=bool)
    for s, e in item["spans"]:
        mask[start + int(s * RATE): start + int(e * RATE)] = True
    active = np().sqrt((sig[mask] ** 2).mean())
    sig *= speech_rms_target / max(active, 1e-9)
    bed = noise("white", total, seed) * 4.0  # the room: webcam microphone floor
    if kind not in ("clean", "quiet"):
        bed = bed + noise(kind, total, seed + 1) * speech_rms_target / (10 ** (snr / 20))
    return to_int16(sig + bed), start / RATE


# ---- running an endpointer over audio --------------------------------------------------------------------------

CONFIGS = {
    "A energy+silence (today)": ("energy", "silence", {}),
    "B silero+silence": ("silero", "silence", {}),
    "C energy+smart": ("energy", "smart", {}),
    "D silero+smart (new)": ("silero", "smart", {}),
    # the model only asked once the old 1.2 s of silence has passed, and only able to wait longer (never to end early)
    "E silero+smart, extend only": ("silero", "smart", {"pause_ms": 1200, "max_silence": 2.0}),
}


def make(cfg, min_p=0.5, max_silence=1.6, pause_ms=200):
    from waggle import mic
    vad, endturn, over = CONFIGS[cfg]
    max_silence, pause_ms = over.get("max_silence", max_silence), over.get("pause_ms", pause_ms)
    ep = mic.make_endpointer(1.2, 30.0, min_on=120.0, min_off=80.0, vad=vad, endturn=endturn,
                             turn_max_silence_s=max_silence, turn_pause_ms=pause_ms, turn_min_p=min_p)
    if ep.mode != f"{vad}+{endturn}":
        raise SystemExit(f"{cfg}: models not available (got {ep.mode}); pass --models with both files fetched")
    return ep


def run_one(audio, cfg, **kw):
    """Feed `audio` (int16 array) to a fresh endpointer. Returns dict(start, end, cap_start, cap_end, ended_by) in
    seconds of audio time (None when it never started / ended)."""
    ep = make(cfg, **kw)
    out = {"start": None, "end": None, "cap_start": None, "cap_end": None, "ended_by": "", "asks": 0}
    for i in range(len(audio) // FRAME):
        r = ep.feed(audio[i * FRAME:(i + 1) * FRAME].tobytes())
        if r == "start":
            out["start"] = (i + 1) * FRAME / RATE
        if r == "end":
            end_t = (i + 1) * FRAME / RATE
            pcm = ep.pcm(keep_tail_s=0.3)
            cap_start = end_t - len(ep._frames) * FRAME / RATE
            out.update(end=end_t, cap_start=cap_start, cap_end=cap_start + len(pcm) / 2 / RATE, ended_by=ep.ended_by,
                       asks=ep.turn_asks, pcm=pcm)
            break
    return out


def judge(res, lead, spans_end_first, true_end, first_start):
    """Classify one run: 'missed', 'stuck', 'cut', and flags."""
    if res["start"] is None:
        return {"missed": True}
    if res["end"] is None:
        return {"stuck": True, "start_clipped": res["start"] > first_start + 0.35}
    cut = res["end"] < true_end - 0.1 or res["cap_end"] < true_end - 0.05
    return {"cut": cut, "delay": res["end"] - true_end,
            "start_clipped": res["cap_start"] > first_start + 0.05, "ended_by": res["ended_by"]}


CONDITIONS = [("clean", None), ("quiet", None), ("fan", 20), ("fan", 10), ("pink", 15), ("music", 10), ("music", 5),
              ("keys", 10)]


def load(work, sset):
    work = Path(work)
    man = [m for m in json.loads((work / "manifest.json").read_text(encoding="utf-8")) if m["set"] == sset]
    wavs = {m["id"]: read_wav(work / "wav" / f"{m['id']}.wav")[0] for m in man}
    return man, wavs


def evaluate(work, sset, kinds, cfgs, conditions=CONDITIONS, **kw):
    man, wavs = load(work, sset)
    rows = {c: [] for c in cfgs}
    for m in man:
        if m["kind"] not in kinds:
            continue
        for ci, cond in enumerate(conditions):
            audio, lead = mix(m, wavs[m["id"]], cond, seed=1000 + ci * 37 + int(m["id"][-3:]))
            true_end = lead + m["spans"][-1][1]
            first_start = lead + m["spans"][0][0]
            for c in cfgs:
                res = run_one(audio, c, **kw)
                j = judge(res, lead, None, true_end, first_start)
                j.update(id=m["id"], kind=m["kind"], cond=f"{cond[0]}{'' if cond[1] is None else str(cond[1]) + 'dB'}",
                         gap=m["gap"])
                rows[c].append(j)
    return rows


def summarize(rows):
    out = {}
    for c, rs in rows.items():
        n = len(rs)
        delays = sorted(r["delay"] for r in rs if "delay" in r and not r.get("cut"))
        out[c] = {
            "n": n,
            "cutoffs": sum(1 for r in rs if r.get("cut")),
            "cut_short_pause": sum(1 for r in rs if r.get("cut") and r["gap"] < 1.15),  # shorter than the fixed 1.2 s
            "start_clipped": sum(1 for r in rs if r.get("start_clipped")),
            "missed": sum(1 for r in rs if r.get("missed")),
            "stuck": sum(1 for r in rs if r.get("stuck")),
            "mean_delay": round(sum(delays) / len(delays), 3) if delays else None,
            "median_delay": round(delays[len(delays) // 2], 3) if delays else None,
            "p95_delay": round(delays[int(len(delays) * 0.95)], 3) if delays else None,
        }
    return out


def false_starts(cfgs, minutes=10.0):
    """Utterances an endpointer would send on for `minutes` of each background with nobody speaking. Levels are RMS in
    int16 units (a quiet room is about 4; the voice in the set is 700)."""
    backgrounds = [("room", "white", 4), ("fan 300", "fan", 300), ("fan 900", "fan", 900), ("pink 300", "pink", 300),
                   ("white 300", "white", 300), ("music 400", "music", 400), ("music 1500", "music", 1500),
                   ("typing 500", "keys", 500), ("door thumps 1500", "thumps", 1500)]
    seconds = 120
    res = {}
    for label, kind, level in backgrounds:
        a = to_int16(noise(kind, seconds, 99) * level + (0 if kind == "white" and level == 4 else noise("white", seconds, 98) * 4))
        for c in cfgs:
            ep = make(c)
            starts = ends = 0
            speech_s = 0.0
            for i in range(len(a) // FRAME):
                r = ep.feed(a[i * FRAME:(i + 1) * FRAME].tobytes())
                if r == "start":
                    starts += 1
                if r == "end":
                    ends += 1
                    speech_s += ep.speech_ms / 1000
                    ep = make(c)
            res.setdefault(label, {})[c] = {"starts_per_10min": round(starts * minutes * 60 / seconds, 1),
                                            "speech_s_per_10min": round(speech_s * minutes * 60 / seconds, 1)}
    return res


def table(summary):
    head = (f"{'endpointer':28} {'n':>4} {'cut':>4} {'cut<1.2':>7} {'clip':>5} {'miss':>5} {'stuck':>6} {'mean':>7} "
            f"{'median':>7} {'p95':>6}")
    lines = [head]
    for c, s in summary.items():
        lines.append(f"{c:28} {s['n']:>4} {s['cutoffs']:>4} {s['cut_short_pause']:>7} {s['start_clipped']:>5} "
                     f"{s['missed']:>5} {s['stuck']:>6} "
                     f"{(s['mean_delay'] if s['mean_delay'] is not None else float('nan')):>7.2f} "
                     f"{(s['median_delay'] if s['median_delay'] is not None else float('nan')):>7.2f} "
                     f"{(s['p95_delay'] if s['p95_delay'] is not None else float('nan')):>6.2f}")
    return "\n".join(lines)


def cmd_run(a):
    os.environ["WAGGLE_MODELS_DIR"] = str(a.models)
    cfgs = list(CONFIGS)
    report = {}
    t0 = time.time()
    for label, kinds in (("complete requests", ("complete",)), ("requests with a thinking pause (0.5-1.5 s)", ("hesitate",)),
                         ("adverse: the two halves spoken as separate sentences (pause 0.5-1.5 s)", ("fragment",)),
                         ("stress: pause 1.8-2.2 s (longer than the 1.6 s limit)", ("stress",))):
        rows = evaluate(a.work, "test", kinds, cfgs, min_p=a.min_p)
        report[label] = {"summary": summarize(rows), "rows": rows}
        print(f"\n== {label}\n{table(report[label]['summary'])}")
    rows = report["requests with a thinking pause (0.5-1.5 s)"]["rows"]
    print("\n== thinking pauses, by background: cut-offs / never-ended (stuck) of "
          f"{len(set(r['id'] for r in rows[cfgs[0]]))} requests")
    conds = sorted({r["cond"] for r in rows[cfgs[0]]})
    print(f"{'background':12}" + "".join(f"{c.split()[0]:>8}" for c in cfgs))
    for cond in conds:
        cells = []
        for c in cfgs:
            cut = sum(1 for r in rows[c] if r["cond"] == cond and r.get("cut"))
            stuck = sum(1 for r in rows[c] if r["cond"] == cond and r.get("stuck"))
            cells.append(f"{cut}/{stuck}".rjust(8))
        print(f"{cond:12}" + "".join(cells))
    fs = false_starts(cfgs)
    report["false_starts"] = fs
    print("\n== false starts per 10 min of noise, nobody speaking (utterances sent on)")
    print(f"{'background':20}" + "".join(f"{c.split()[0]:>6}" for c in cfgs))
    for label, d in fs.items():
        print(f"{label:20}" + "".join(f"{d[c]['starts_per_10min']:>6}" for c in cfgs))
    print(f"\n({time.time() - t0:.0f} s)")
    if a.json:
        slim = {k: ({"summary": v["summary"]} if isinstance(v, dict) and "summary" in v else v) for k, v in report.items()}
        Path(a.json).write_text(json.dumps(slim, indent=1), encoding="utf-8")


def cmd_words(a):
    """Lost words, measured with the recogniser: what the endpointers captured is transcribed (Whisper small, CPU) and
    compared, word by word, with the transcript of the same sentence taken whole from the clean recording."""
    import difflib
    os.environ["WAGGLE_MODELS_DIR"] = str(a.models)
    man, wavs = load(a.work, "test")
    items = [m for m in man if m["kind"] == "complete" or (m["kind"] == "hesitate" and m["gap"] < 1.15)]
    conds = [("clean", None), ("quiet", None), ("fan", 10), ("music", 10)]
    cfgs = ["A energy+silence (today)", "B silero+silence"]
    ref = {m["id"]: [w for w in _words(heard(wavs[m["id"]])) if w not in FILLERS] for m in items}
    stats = {c: {"n": 0, "no_turn": 0, "recall": 0.0, "first_lost": 0, "last_lost": 0, "perfect": 0} for c in cfgs}
    for ci, cond in enumerate(conds):
        for m in items:
            audio, lead = mix(m, wavs[m["id"]], cond, seed=1000 + ci * 37 + int(m["id"][-3:]))
            for c in cfgs:
                s = stats[c]
                s["n"] += 1
                res = run_one(audio, c)
                if res.get("pcm") is None:
                    s["no_turn"] += 1
                    continue
                pcm = np().frombuffer(res["pcm"], dtype="<i2").astype("float32") / 32768.0
                got = [w for w in _words(heard(pcm)) if w not in FILLERS]
                want = ref[m["id"]]
                matched = sum(b.size for b in difflib.SequenceMatcher(a=want, b=got, autojunk=False).get_matching_blocks())
                s["recall"] += matched / max(1, len(want))
                s["first_lost"] += bool(want and (not got or got[0] != want[0]))
                s["last_lost"] += bool(want and (not got or got[-1] != want[-1]))
                s["perfect"] += got == want
    print(f"{len(items)} sentences x {len(conds)} backgrounds, the capture transcribed and compared with the clean whole sentence")
    print(f"{'endpointer':28} {'n':>4} {'no turn':>8} {'word recall':>12} {'first word lost':>16} {'last word lost':>15} {'identical':>10}")
    for c, s in stats.items():
        done = max(1, s["n"] - s["no_turn"])
        print(f"{c:28} {s['n']:>4} {s['no_turn']:>8} {s['recall'] / done:>12.3f} {s['first_lost']:>16} {s['last_lost']:>15} {s['perfect']:>10}")


def cmd_tune(a):
    os.environ["WAGGLE_MODELS_DIR"] = str(a.models)
    d = "D silero+smart (new)"
    conds = [("clean", None), ("fan", 10), ("music", 10)]
    print("dev set, 3 conditions: end-of-turn threshold x pause before the model is asked")
    for pause in (200, 400, 600):
        for p in (0.5, 0.8, 0.9, 0.97):
            out = []
            for kinds in (("hesitate",), ("fragment",), ("complete",)):
                s = summarize(evaluate(a.work, "dev", kinds, [d], conditions=conds, min_p=p, pause_ms=pause))[d]
                out.append(f"{kinds[0]} cut {s['cutoffs']}/{s['n']} (delay {s['mean_delay']})")
            print(f"pause {pause} ms, min_p {p}: " + "; ".join(out))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth")
    s.add_argument("--work", required=True)
    s.add_argument("--kokoro-url", default="http://127.0.0.1:8899")
    for name in ("run", "tune", "words"):
        r = sub.add_parser(name)
        r.add_argument("--work", required=True)
        r.add_argument("--models", required=True)
        r.add_argument("--min-p", type=float, default=0.5)
        r.add_argument("--json", default="")
    a = ap.parse_args(argv)
    if a.cmd == "synth":
        synth(a.work, a.kokoro_url)
    elif a.cmd == "run":
        cmd_run(a)
    elif a.cmd == "words":
        cmd_words(a)
    else:
        cmd_tune(a)


if __name__ == "__main__":
    main()
