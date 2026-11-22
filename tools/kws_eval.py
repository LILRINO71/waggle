"""Measure the PC voice's wake path offline: today's "transcribe every utterance" (stt) against the keyword spotter
(kws), with no microphone and no speaker.

    python tools/kws_eval.py synth --work DIR [--kokoro-port 8899]
    python tools/kws_eval.py sweep --work DIR            spotter only, dev voices: threshold and spelling sweep
    python tools/kws_eval.py run   --work DIR [--json out.json] [--threshold 0.25]

synth  makes the clips with Waggle's own speech server (Kokoro): positives ("Waggle, what time is it?", "Hey Waggle,
       ...", "Waggle." alone), hard negatives (bagel, wiggle, waffle, haggle ... and "the waggle dance of bees" said
       mid-sentence), and TV-like talk. Two voice sets: "dev" (used to pick the threshold) and "test" (reported).
run    mixes every clip with the room: the owner's real level (speech RMS ~250 and a floor of ~2, from the PC voice
       log: rms 180-330, floor 0.5-4), a quiet voice, a loud one, a fan, music and a second talker (a TV) at 10 dB
       under. Each clip goes through the PC voice's own wake path, frame by frame (PcVoice.wake_frame: Silero gate,
       spotter, heard()), in dry-run mode, with the real recogniser (faster-whisper turbo through waggle.stt, the
       same call the core's /voice/transcribe makes). It reports per mode (stt, kws strict, kws trusting the
       spotter, both): hit rate, requests that came through whole, false triggers on single hard negatives and on
       10 minutes of room (TV talk, music, keys, thumps), recogniser calls, and CPU of the gate plus spotter.
Only numpy, onnxruntime, sherpa-onnx and faster-whisper are needed; Kokoro is only used by `synth`."""
import argparse
import hashlib
import json
import os
import random
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import endturn_eval as ee  # noqa: E402  (noise, trim, resample, say, read_wav, write_wav16)

RATE = 16000
FRAME = 480
DEV_VOICES = ["bm_george", "af_heart"]
TEST_VOICES = ["am_adam", "bf_emma", "af_bella", "bm_lewis"]
SPEEDS = [0.9, 1.1]
KOKORO = "http://127.0.0.1:{port}/"

# (text, a word the request must still contain, or "")
POSITIVE = [
    ("Waggle, what time is it?", "time"),
    ("Hey Waggle, what's on my calendar today?", "calendar"),
    ("Waggle.", ""),
    ("Hey Waggle.", ""),
    ("Waggle, remind me to stretch in twenty minutes.", "stretch"),
]
# single hard negatives: sound-alikes at the start, and the name said inside a sentence
HARD = [
    "Bagel.", "Wiggle the cable a bit and see if the screen comes back.", "Waffle fries are on sale today.",
    "Gaggle of geese crossed the road.", "Haggle with the seller a little.", "Maggie called me yesterday.",
    "Wagon wheels are squeaking again.", "Giggle, giggle, that was so funny.", "Struggle is part of learning.",
    "Wobble the table to check it.", "Bagel and cream cheese, please.", "Wiggle room is tight this week.",
    "Eagle has landed, repeat, the eagle has landed.", "Wagging tails everywhere in the park.",
    "I read that the waggle dance is how bees tell each other where flowers are.",
    "The waggle dance of bees is amazing.", "Did you know honey bees do a waggle dance?",
    "My friend said that waggle sounds like a funny word.", "We watched a video about the waggle dance yesterday.",
    "Then he said, waggle your fingers like this.",
]
# talk like a TV or an anime in the next room
TV = [
    "So I was thinking we could get pizza tonight, if everyone is fine with that.",
    "The game starts at seven, and I still need to finish my homework before then.",
    "Did you see how much it rained this morning? The whole street was flooded.",
    "My brother keeps borrowing my charger and never gives it back.",
    "Okay, the next step is to tighten the four bolts on the bottom plate.",
    "Honestly, the second movie was better than the first one.",
    "We should leave around three thirty so we are not late again.",
    "Can you pass me the remote? It is on the couch next to you.",
    "The teacher said the test will cover chapters four through six.",
    "I think the battery is almost dead, the light keeps blinking red.",
    "Welcome back to the show. Today we are talking about electric cars.",
    "Turn left at the next light, then keep going for about a mile.",
    "That is a great question, and the answer depends on the season.",
    "Hey, are you coming down for dinner or should I save you a plate?",
    "The robot drove straight into the wall, so we need to check the code.",
    "In other news, the city council approved the new park on Main Street.",
    "Hold on, let me find that file, it was somewhere in my downloads.",
    "If you add a little salt, the flavor comes out a lot better.",
    "Hey, what time does the store close on Sundays?",
    "Let us take a short break and come back in ten minutes.",
    "I never thought I would see this day, but here we are, standing at the edge of the world.",
    "You can't just run away from this, Kenji. The village needs you, and so do I.",
    "The captain ordered the crew to hold their positions while the storm passed overhead.",
    "Tonight's forecast calls for scattered showers with a high of sixty two degrees.",
    "And that is why the champion has never lost a match on home ground, ladies and gentlemen.",
    "Please remain seated until the aircraft has come to a complete stop at the gate.",
    "I told you a hundred times, the key is under the flowerpot, not in the mailbox.",
    "Mom, where did you put my blue jacket? I can't find it anywhere in the closet.",
    "Breaking news this evening, the mayor has announced a plan to repair every bridge in the county.",
    "Next on the menu we have a roasted chicken with garlic, rosemary, and a splash of lemon.",
    "I'm not afraid of the dark anymore, because I know you are there beside me.",
    "Class, open your books to page forty and read the first paragraph to yourself.",
    "The suspect was last seen heading north on foot, wearing a gray hooded sweatshirt.",
    "Everybody get down, the whole building is about to come apart!",
    "She smiled, picked up the little paper boat, and set it gently on the water.",
    "This week on the show, we are visiting a farm where they grow forty kinds of apples.",
    "No way, you did not just win the whole tournament on your very first try.",
    "Press the red button twice, wait for the beep, and then pull the lever slowly.",
    "The prince looked out from the tower and saw an army of lanterns crossing the valley.",
    "For just nineteen ninety nine, you can have two of these and a free carrying case.",
    "Hey, wait for me, I'm coming too, don't leave without me this time!",
    "The train to the airport leaves from platform six every twenty minutes all day long.",
    "Okay everyone, settle down, we have a lot to cover before the bell rings.",
    "He opened the old wooden door and stepped into a room full of dusty clocks.",
    "Honey, can you take the trash out before you sit down to play that game?",
    "Scientists announced today that they have found water on a moon of Saturn.",
    "That was the best pancake I have ever had in my whole entire life.",
    "Stop right there, you have no idea what you are getting yourself into.",
    "Two cups of flour, one cup of sugar, and a pinch of salt, then stir it all together.",
    "The moon is bright tonight, so we should be able to see the whole trail.",
    "Alright, who wants to go first and tell us about their summer vacation?",
    "I can't believe it's already December, this year went by faster than any other.",
    "We interrupt this program to bring you a special report from the capital.",
    "Remember, the only thing we have to fear is giving up before we even begin.",
    "My name is Hana, and I am the strongest swordswoman in the entire kingdom.",
    "Don't forget to like, subscribe, and hit the little bell so you never miss a video.",
    "The final score was three to two, and the crowd went absolutely wild.",
    "Okay, so first you want to preheat the oven to three hundred fifty degrees.",
    "Over the next hill there is a river, and past the river there is a town.",
    "I wonder what the weather will be like when we finally get to the coast.",
    "Please speak clearly after the tone and state your name and your reason for calling.",
    "There she goes again, running down the hallway with her shoes in her hands.",
    "The results of the election will be announced shortly after midnight.",
    "Look at that, the whole sky is turning orange and pink above the mountains.",
    "I used to think robots would take over, but now I just want one to fold my laundry.",
]

CONDITIONS = [  # name, speech RMS, background kind, background dB under the speech
    ("room", 250.0, None, None),  # the owner's measured level
    ("quiet", 150.0, None, None),
    ("loud", 700.0, None, None),
    ("fan10", 250.0, "fan", 10),
    ("music10", 250.0, "music", 10),
    ("tv10", 250.0, "tv", 10),
]
BED_RMS = 2.0  # the webcam microphone's floor in a still room (log: 0.5-4)


def np():
    import numpy
    return numpy


# ---- clips ---------------------------------------------------------------------------------------------------

def synth(work, port):
    work = Path(work)
    (work / "clips").mkdir(parents=True, exist_ok=True)
    url = KOKORO.format(port=port)
    tmp = work / "_k.wav"
    manifest = []

    def make(kind, text, voice, speed, extra=None):
        cid = f"{kind}-{len(manifest):04d}"
        a = ee.say(url, text, voice, speed, tmp)
        a = a / max(1e-9, np().abs(a).max()) * 0.5
        ee.write_wav16(work / "clips" / f"{cid}.wav", a)
        manifest.append({"id": cid, "kind": kind, "text": text, "voice": voice, "speed": speed,
                         "seconds": round(len(a) / RATE, 2), **(extra or {})})

    for sset, voices in (("dev", DEV_VOICES), ("test", TEST_VOICES)):
        for pi, (text, word) in enumerate(POSITIVE):
            for vi, v in enumerate(voices):
                for sp in SPEEDS:
                    make("pos", text, v, sp, {"set": sset, "phrase": pi, "word": word})
        for hi, text in enumerate(HARD):
            make("hard", text, voices[hi % len(voices)], SPEEDS[(hi // 2) % 2], {"set": sset})
    rng = random.Random(7)
    allv = DEV_VOICES + TEST_VOICES
    for i, text in enumerate(TV):
        make("tv", text, allv[i % len(allv)], rng.choice([0.95, 1.0, 1.1, 1.2]), {"set": "stream"})
    tmp.unlink(missing_ok=True)
    (work / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"{len(manifest)} clips in {work / 'clips'}")


def load_manifest(work):
    m = json.loads((Path(work) / "manifest.json").read_text(encoding="utf-8"))
    for c in m:
        a, _ = ee.read_wav(Path(work) / "clips" / f"{c['id']}.wav")
        c["audio"] = a
    return m


# ---- mixing --------------------------------------------------------------------------------------------------

def _rms(a):
    return float(np().sqrt((a ** 2).mean())) if len(a) else 0.0


def background(kind, seconds, seed, speech_rms, db, tv_pool=None):
    if kind is None:
        return np().zeros(int(seconds * RATE))
    if kind == "tv":
        rng = random.Random(seed)
        out = []
        while sum(len(x) for x in out) < seconds * RATE:
            out.append(rng.choice(tv_pool)["audio"])
            out.append(np().zeros(int(rng.uniform(1.0, 2.5) * RATE)))  # a pause between lines: the gate can end
        x = np().concatenate(out)[: int(seconds * RATE)]
        x = x / max(_rms(x), 1e-9)
    else:
        x = ee.noise(kind, seconds, seed)
    return x * speech_rms / (10 ** (db / 20))


def place(clip, rms, bg_kind, db, seed, tv_pool=None, lead=1.5, trail=4.0):
    """The clip at `rms` inside `lead` + `trail` seconds of room (white bed of BED_RMS plus the background)."""
    a = clip / max(_rms(clip), 1e-9) * rms
    total = lead + len(a) / RATE + trail
    sig = np().zeros(int(total * RATE))
    s0 = int(lead * RATE)
    sig[s0:s0 + len(a)] = a
    sig = sig + ee.noise("white", total, seed) * BED_RMS + background(bg_kind, total, seed + 1, rms, db, tv_pool)[: len(sig)]
    return ee.to_int16(sig)


def room_stream(manifest, seconds, seed):
    """`seconds` of an ordinary evening: TV talk at varying levels, music, typing, door thumps and hard negatives,
    with gaps of room tone between them (no clip contains the name at the start; the mid-sentence waggle ones are in)."""
    rng = random.Random(seed)
    tv = [c for c in manifest if c["kind"] == "tv"]
    hard = [c for c in manifest if c["kind"] == "hard"]
    parts, total, n = [], 0.0, 0
    while total < seconds:
        n += 1
        roll = rng.random()
        if roll < 0.68:
            c = rng.choice(tv)
            a = c["audio"] / max(_rms(c["audio"]), 1e-9) * rng.choice([120, 180, 250, 350, 600])
        elif roll < 0.80:
            c = rng.choice(hard)
            a = c["audio"] / max(_rms(c["audio"]), 1e-9) * rng.choice([180, 250, 350])
        elif roll < 0.92:
            a = ee.noise("music", rng.uniform(6, 12), seed + n) * rng.choice([150, 300])
        elif roll < 0.96:
            a = ee.noise("keys", rng.uniform(4, 8), seed + n) * 120
        else:
            a = ee.noise("thumps", rng.uniform(6, 10), seed + n) * 250
        gap = np().zeros(int(rng.uniform(1.5, 6.0) * RATE))
        parts += [a, gap]
        total += (len(a) + len(gap)) / RATE
    x = np().concatenate(parts)[: int(seconds * RATE)]
    x = x + ee.noise("white", len(x) / RATE, seed + 999)[: len(x)] * BED_RMS
    return ee.to_int16(x)


# ---- the wake path under test ---------------------------------------------------------------------------------

class FakeSpeaker:
    speaking = False

    def stop(self):
        pass


class Client:
    """Stands in for CoreClient: the recogniser is waggle.stt (the same call /voice/transcribe makes); answers are
    kept by the audio's hash so the same audio is only recognised once. `calls` counts every request."""
    def __init__(self, null=False):
        self.null, self.cache = null, {}
        self.calls = self.audio_s = self.gpu_wall_s = self.cpu_s = 0.0

    def transcribe(self, pcm, **kw):
        self.calls += 1
        self.audio_s += len(pcm) / 2 / RATE
        if self.null:
            return {"text": "", "ok": False, "error": ""}
        key = hashlib.sha1(pcm).hexdigest()
        if key not in self.cache:
            from waggle import mic, stt
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            f.close()
            try:
                mic.write_wav(f.name, pcm)
                t0, c0 = time.perf_counter(), time.process_time()
                r = stt.transcribe(f.name)
                self.gpu_wall_s += time.perf_counter() - t0
                self.cpu_s += time.process_time() - c0
            finally:
                os.unlink(f.name)
            self.cache[key] = {"text": r.get("text") or "", "error": "" if r.get("ok") else (r.get("error") or "")}
        return dict(self.cache[key])


class Rig:
    """A PcVoice in dry-run mode over one mode and setting set; feed() runs audio through its real wake path."""
    def __init__(self, mode, *, trust=True, threshold=0.25, head_s=1.0, variants=True, greet=True, client=None):
        from waggle import kws, pcvoice
        self.pcvoice, self.kws = pcvoice, kws
        self.overrides = {"pc_voice_kws_trust": trust, "pc_voice_kws_threshold": threshold, "pc_voice_kws_head_s": head_s,
                          "pc_voice_smart": False}
        self._orig = pcvoice.setting
        pcvoice.setting = lambda k: self.overrides[k] if k in self.overrides else self._orig(k)
        self.client = client or Client()
        spotter = None
        if mode != "stt":
            spotter = kws.load(["Waggle"], threshold, 1.0, variants=variants, greet=greet)
            assert spotter is not None, "the spotter did not load"
        self.spotter = spotter
        self.pv = pcvoice.PcVoice(spk=FakeSpeaker(), client=self.client, wake=True, dry_wake=True, wake_mode=mode,
                                  smart=False, spotter=spotter)
        self.cpu_s = self.audio_s = 0.0

    def close(self):
        self.pcvoice.setting = self._orig

    def feed(self, pcm_int16):
        """Run one audio array through the wake path. Returns (triggers, recogniser calls) it caused."""
        pv, c = self.pv, self.client
        t0, calls0, cpu_c0 = pv.stats["wake_triggers"], c.calls, c.cpu_s
        ep = None
        cpu0 = time.process_time()
        for i in range(len(pcm_int16) // FRAME):
            ep = pv.wake_frame(None, ep, pcm_int16[i * FRAME:(i + 1) * FRAME].tobytes())
            if ep is None:
                pv._ring.clear()
        self.cpu_s += time.process_time() - cpu0 - (c.cpu_s - cpu_c0)  # the recogniser's own CPU is not the gate's
        self.audio_s += len(pcm_int16) / RATE
        return pv.stats["wake_triggers"] - t0, c.calls - calls0

    def kws_cpu_s(self):
        return self.spotter.busy_s if self.spotter else 0.0


MODES = [  # label, mode, trust
    ("stt (today)", "stt", False),
    ("kws strict", "kws", False),
    ("kws trusting", "kws", True),
    ("both trusting", "both", True),
]


def positives(manifest, sset):
    return [c for c in manifest if c["kind"] == "pos" and c["set"] == sset]


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def run_modes(work, args, sset="test", stream_s=600.0, modes=MODES, log=print):
    manifest = load_manifest(work)
    pos = positives(manifest, sset)
    hard = [c for c in manifest if c["kind"] == "hard" and c["set"] == sset]
    tv_pool = [c for c in manifest if c["kind"] == "tv"]
    stream = room_stream(manifest, stream_s, 11)
    out = {}
    for label, mode, trust in modes:
        rig = Rig(mode, trust=trust, threshold=args.threshold, head_s=args.head_s, variants=not args.no_variants,
                  greet=not args.no_greet)
        res = {"mode": label, "pos": 0, "pos_n": 0, "pos_whole": 0, "by_cond": {}, "by_phrase": {}, "hard_false": 0,
               "hard_n": 0, "hard_calls": 0, "pos_calls": 0}
        try:
            c0 = rig.client.calls
            for ci, cond in enumerate(CONDITIONS):
                name, rms, bg, db = cond
                hit = n = 0
                for k, c in enumerate(pos):
                    audio = place(c["audio"], rms, bg, db, 1000 + 7 * k + ci, tv_pool)
                    r0 = len(rig.pv.trigger_rests)
                    trig, _ = rig.feed(audio)
                    n += 1
                    ok = trig > 0
                    hit += ok
                    rest = " ".join(rig.pv.trigger_rests[r0:]).lower()
                    whole = ok and (not c["word"] or c["word"] in rest)
                    if name != "tv10":  # the headline: the owner speaks into a room; tv10 has a second talker over them
                        res["pos"] += ok
                        res["pos_n"] += 1
                        res["pos_whole"] += whole
                    p = res["by_phrase"].setdefault(c["phrase"], [0, 0])
                    p[0] += ok
                    p[1] += 1
                res["by_cond"][name] = [hit, n]
            res["pos_calls"] = rig.client.calls - c0
            c1 = rig.client.calls
            for k, c in enumerate(hard):
                for ci, cond in enumerate(CONDITIONS[:1] + CONDITIONS[3:]):
                    name, rms, bg, db = cond
                    audio = place(c["audio"], rms, bg, db, 5000 + 11 * k + ci, tv_pool)
                    trig, _ = rig.feed(audio)
                    res["hard_false"] += trig > 0
                    res["hard_n"] += 1
            res["hard_calls"] = rig.client.calls - c1
            # 10 minutes of room
            c2, t2, s0, a0 = rig.client.calls, rig.pv.stats["wake_triggers"], rig.cpu_s, rig.audio_s
            rig.pv.stats["utterances"] = 0
            rig.feed(stream)
            res["stream_s"] = round(len(stream) / RATE)
            res["stream_utterances"] = rig.pv.stats["utterances"]
            res["stream_calls"] = rig.client.calls - c2
            res["stream_false"] = rig.pv.stats["wake_triggers"] - t2
            res["stream_cpu_pct"] = round(100.0 * (rig.cpu_s - s0) / max(1e-9, rig.audio_s - a0), 2)
            res["stream_kws_cpu_pct"] = round(100.0 * rig.kws_cpu_s() / max(1e-9, rig.audio_s), 2)
            res["stream_audio_s_sent"] = round(rig.client.audio_s, 1)
            res["false_per_hour"] = round(res["stream_false"] * 3600.0 / res["stream_s"], 1)
            res["kws_hits"], res["kws_skipped"] = rig.pv.stats["kws_hits"], rig.pv.stats["kws_skipped"]
            res["rescued"] = rig.pv.stats["kws_rescued"]
        finally:
            rig.close()
        out[label] = res
        log(json.dumps({k: v for k, v in res.items() if k not in ("by_phrase",)}))
    return out


def table(out):
    rows = []
    h = (f"{'mode':16} {'hit%':>6} {'whole%':>7} {'tv10 hit%':>10} {'hard FP':>8} {'10min FP':>9} {'FP/h':>6} "
         f"{'calls/utts':>11} {'CPU%/core':>10}")
    rows.append(h)
    for label, r in out.items():
        tv = r["by_cond"].get("tv10", [0, 0])
        rows.append(f"{label:16} {pct(r['pos'], r['pos_n']):6} {pct(r['pos_whole'], r['pos_n']):7} {pct(*tv):10} "
                    f"{r['hard_false']}/{r['hard_n']:<5} {r['stream_false']:9} {r['false_per_hour']:6} "
                    f"{r['stream_calls']:>5}/{r['stream_utterances']:<5} {r['stream_cpu_pct']:10}")
    return "\n".join(rows)


def sweep(work, args):
    """Spotter only (the recogniser is a stub that says nothing): per threshold and spelling set, how many positive
    clips the spotter hears (kws_hits) and how many utterances of hard negatives and the room it lets through."""
    manifest = load_manifest(work)
    tv_pool = [c for c in manifest if c["kind"] == "tv"]
    pos = positives(manifest, "dev")
    hard = [c for c in manifest if c["kind"] == "hard" and c["set"] == "dev"]
    stream = room_stream(manifest, 300.0, 5)
    results = []
    for thr in (0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5):
        for variants, greet in ((True, True), (False, True), (False, False)):
            rig = Rig("kws", trust=False, threshold=thr, variants=variants, greet=greet, client=Client(null=True))
            try:
                hit = n = 0
                for ci, (name, rms, bg, db) in enumerate(CONDITIONS):
                    for k, c in enumerate(pos):
                        before = rig.pv.stats["kws_hits"]
                        rig.feed(place(c["audio"], rms, bg, db, 1000 + 7 * k + ci, tv_pool))
                        hit += rig.pv.stats["kws_hits"] > before
                        n += 1
                fp = fpn = 0
                for k, c in enumerate(hard):
                    for ci, (name, rms, bg, db) in enumerate(CONDITIONS[:1] + CONDITIONS[3:]):
                        before = rig.pv.stats["kws_hits"]
                        rig.feed(place(c["audio"], rms, bg, db, 5000 + 11 * k + ci, tv_pool))
                        fp += rig.pv.stats["kws_hits"] > before
                        fpn += 1
                before = rig.pv.stats["kws_hits"]
                rig.feed(stream)
                room = rig.pv.stats["kws_hits"] - before
                row = {"threshold": thr, "variants": variants, "greet": greet, "hit%": pct(hit, n),
                       "hard_fp": f"{fp}/{fpn}", "room5min_hits": room}
                results.append(row)
                print(json.dumps(row), flush=True)
            finally:
                rig.close()
    return results


def soak(work, args):
    """Hours of room with no recogniser at all (a stub that says nothing): how often the spotter lets an utterance
    through, and what the gate plus spotter cost. Different seeds give different evenings."""
    manifest = load_manifest(work)
    rig = Rig("kws", trust=False, threshold=args.threshold, head_s=args.head_s, client=Client(null=True))
    try:
        total = 0.0
        for seed in range(args.seeds):
            minutes = args.stream_s / 60.0
            rig.feed(room_stream(manifest, args.stream_s, 100 + seed))
            total += minutes
        st = rig.pv.stats
        out = {"minutes": round(total), "utterances": st["utterances"], "spotter_hits": st["kws_hits"],
               "hits_per_hour": round(st["kws_hits"] * 60.0 / total, 2), "cpu_pct_of_one_core_gate_and_spotter":
               round(100.0 * rig.cpu_s / rig.audio_s, 2), "spotter_ms_per_scan": round(1000 * rig.spotter.busy_s /
               max(1, rig.spotter.scans), 1)}
        print(json.dumps(out))
        return out
    finally:
        rig.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python tools/kws_eval.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth")
    s.add_argument("--work", required=True)
    s.add_argument("--kokoro-port", type=int, default=8899)
    for name in ("sweep", "run", "soak"):
        p = sub.add_parser(name)
        p.add_argument("--work", required=True)
        p.add_argument("--threshold", type=float, default=0.25)
        p.add_argument("--head-s", type=float, default=1.0)
        p.add_argument("--no-variants", action="store_true")
        p.add_argument("--no-greet", action="store_true")
        p.add_argument("--json", default="")
        p.add_argument("--stream-s", type=float, default=600.0)
        p.add_argument("--seeds", type=int, default=6, help="soak: how many streams of --stream-s")
        p.add_argument("--set", default="test")
        p.add_argument("--only", default="", help="comma list: run only the modes whose label contains one of these")
    args = ap.parse_args(argv)
    if args.cmd == "synth":
        synth(args.work, args.kokoro_port)
    elif args.cmd == "soak":
        soak(args.work, args)
    elif args.cmd == "sweep":
        r = sweep(args.work, args)
        if args.json:
            Path(args.json).write_text(json.dumps(r, indent=1), encoding="utf-8")
    else:
        keep = [x.strip() for x in args.only.split(",") if x.strip()]
        modes = [m for m in MODES if not keep or any(k in m[0] for k in keep)]
        out = run_modes(args.work, args, sset=args.set, stream_s=args.stream_s, modes=modes)
        print(table(out))
        if args.json:
            Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
