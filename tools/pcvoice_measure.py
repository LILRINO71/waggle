"""Measure the PC voice's wake word before trusting it: CPU, GPU and false triggers.

    python tools/pcvoice_measure.py [--quiet-s 300] [--speech-s 300] [--names Waggle] [--sanity]

It runs everything in a throwaway Waggle home and never touches the live one:
  * a measuring core: the core's own HTTP server (POST /voice/transcribe with the real recogniser) in a child
    process, on a free port;
  * the PC voice: `python -m waggle.pcvoice run --wake --dry-wake --no-hotkey` in a child process: it listens to
    the real microphone, transcribes every utterance through that core and counts wake-name matches, but sends
    nothing and starts no conversation (dry run);
  * phase 1 (--quiet-s): the room as it is;
  * phase 2 (--speech-s): this script plays short synthesised sentences that do not contain any wake name through
    the default output device, quietly (peak at 20% of full scale), with pauses between them;
  * --sanity first plays one sentence that starts with the wake name, to show the pipeline can hear it at all.
CPU is read from each child's process times (GetProcessTimes); GPU from nvidia-smi every 10 s (if present).
Prints one JSON report. Every child is stopped at the end. Nothing heard is written to disk: the PC voice keeps
only counters, and the words of a (false) trigger, which a dry run reports so they can be judged."""
import argparse
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from ctypes import wintypes
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NO_WINDOW = 0x08000000

SENTENCES = [
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
    "Wiggle the cable a bit and see whether the screen comes back.",
    "Hey, what time does the store close on Sundays?",
    "The waggle dance is how honey bees tell each other where flowers are.",
    "Let us take a short break and come back in ten minutes.",
]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cpu_seconds(handle):
    k32 = ctypes.WinDLL("kernel32")
    times = [wintypes.FILETIME() for _ in range(4)]
    if not k32.GetProcessTimes(wintypes.HANDLE(int(handle)), *(ctypes.byref(t) for t in times)):
        return None
    ft = lambda t: ((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7  # noqa: E731
    return ft(times[2]) + ft(times[3])


def gpu_sample():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        util, mem = r.stdout.strip().splitlines()[0].split(",")
        return float(util), float(mem)
    except Exception:
        return None


def serve(port):
    """The measuring core: the core's HTTP server alone, in this process, until serve.stop appears in the home.
    (Not "until stdin closes": a main thread blocked reading stdin stalls the recogniser's threads.)"""
    sys.path.insert(0, str(REPO))
    from waggle import config
    from waggle.core import health, voiceline
    voiceline.token()
    srv = health.serve(port=port, accepting_fn=lambda: True)
    print("ready", flush=True)
    stop = Path(config.HOME) / "serve.stop"
    while not stop.exists():
        time.sleep(0.3)
    srv.shutdown()


def synth_clips(folder, texts, peak=0.2, prefix="clip"):
    """The sentences as WAV files from the local speech server, scaled so their peak is `peak` of full scale."""
    sys.path.insert(0, str(REPO))
    import array
    from waggle import voice_reply
    out = []
    for n, text in enumerate(texts):
        p = folder / f"{prefix}{n:02d}.wav"
        voice_reply.synth_wav(text, p, "af_heart" if n % 2 else "bm_george", 1.0, 30)
        with wave.open(str(p), "rb") as w:
            params, raw = w.getparams(), w.readframes(w.getnframes())
        s = array.array("h")
        s.frombytes(raw)
        top = max(1, max(abs(v) for v in s))
        g = peak * 32767 / top
        s = array.array("h", (int(v * g) for v in s))
        with wave.open(str(p), "wb") as w:
            w.setparams(params)
            w.writeframes(s.tobytes())
        out.append(p)
    return out


def play(path):
    import winsound
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)  # blocks until it ends


def status(home):
    try:
        return json.loads((home / "pcvoice" / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet-s", type=float, default=300)
    ap.add_argument("--speech-s", type=float, default=300)
    ap.add_argument("--names", default="Waggle")
    ap.add_argument("--sanity", action="store_true")
    ap.add_argument("--work", default=str(REPO.parent / ".pcvoice-tmp"),
                    help="where the throwaway home lives (not under AppData: the speech server must see it)")
    ap.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.serve:
        return serve(args.serve)

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="measure-home-", dir=work))
    clips_dir = Path(tempfile.mkdtemp(prefix="clips-", dir=work))
    port = free_port()
    env = {**os.environ, "WAGGLE_HOME": str(home), "WAGGLE_CORE_PORT": str(port), "WAGGLE_ORIGIN": "test"}
    (home / "config.json").write_text(json.dumps({"pc_voice_wake_names": args.names.split(","),
                                                  "pc_voice_speak_messages": False, "quiet_hours": [0, 0]}),
                                      encoding="utf-8")
    report = {"home": "temp", "names": args.names.split(","), "cpu_count": os.cpu_count()}
    core = voice = None
    try:
        core = subprocess.Popen([sys.executable, __file__, "--serve", str(port)], cwd=str(REPO), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
        assert core.stdout.readline().strip() == "ready"
        clips = synth_clips(clips_dir, SENTENCES)
        # warm the recogniser so loading the model is not counted as listening
        sys.path.insert(0, str(REPO))
        os.environ.update(WAGGLE_HOME=str(home), WAGGLE_CORE_PORT=str(port))
        from waggle import mic, pcvoice
        t0 = time.time()
        warm = pcvoice.CoreClient(port=port, timeout=180).transcribe(b"\x00\x00" * 16000)
        report["model_load_s"] = round(time.time() - t0, 1)
        report["warm_error"] = warm.get("error", "")
        core_cpu0 = cpu_seconds(core._handle)
        total = args.quiet_s + args.speech_s + (40 if args.sanity else 0)
        voice = subprocess.Popen([sys.executable, "-m", "waggle.pcvoice", "run", "--wake", "--dry-wake", "--no-hotkey",
                                  "--seconds", str(total)], cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                                 text=True, creationflags=NO_WINDOW)
        gpu, stop_gpu = {"quiet": [], "speech": [], "sanity": []}, threading.Event()
        phase = {"name": "sanity" if args.sanity else "quiet"}

        def sample():
            while not stop_gpu.wait(10):
                g = gpu_sample()
                if g:
                    gpu[phase["name"]].append(g)
        threading.Thread(target=sample, daemon=True).start()
        start = time.time()
        if args.sanity:
            time.sleep(6)
            voice_text = f"Hey {report['names'][0]}, what time is it?"
            play(synth_clips(clips_dir, [voice_text], prefix="sanity")[0])
            time.sleep(25)
            report["sanity"] = {k: status(home).get(k) for k in ("utterances", "transcribed", "wake_triggers")}
            phase["name"] = "quiet"
            start = time.time()
        time.sleep(args.quiet_s)
        report["after_quiet"] = {k: status(home).get(k) for k in ("utterances", "transcribed", "wake_triggers",
                                                                     "transcribe_s", "microphone")}
        phase["name"] = "speech"
        t_speech = time.time()
        n = played = 0
        while time.time() - t_speech < args.speech_s - 8:
            play(clips[n % len(clips)])
            played += 1
            n += 1
            time.sleep(4 + (n % 4))
        report["clips_played"] = played
        voice.wait(timeout=total + 60)
        stop_gpu.set()
        out = voice.stdout.read().strip().splitlines()
        final = json.loads(out[-1]) if out else {}
        wall = time.time() - start
        report["final"] = {k: final.get(k) for k in ("utterances", "transcribed", "wake_triggers", "transcribe_s",
                                                     "errors", "last_error")}
        report["trigger_texts"] = final.get("trigger_texts", [])
        voice_cpu = cpu_seconds(voice._handle)
        core_cpu = cpu_seconds(core._handle) - core_cpu0
        n_cpu = os.cpu_count() or 1
        report["wall_s"] = round(wall, 1)
        report["pcvoice_cpu_s"] = round(voice_cpu, 1)
        report["core_transcribe_cpu_s"] = round(core_cpu, 1)
        report["avg_cpu_pct_of_one_core"] = {"pcvoice": round(100 * voice_cpu / wall, 2),
                                             "core_transcribing": round(100 * core_cpu / wall, 2)}
        report["avg_cpu_pct_of_machine"] = round(100 * (voice_cpu + core_cpu) / wall / n_cpu, 3)
        for k, v in gpu.items():
            if v:
                report[f"gpu_{k}"] = {"util_avg": round(sum(x[0] for x in v) / len(v), 1),
                                      "util_max": max(x[0] for x in v), "samples": len(v)}
    finally:
        for p in (voice, core):
            if p and p.poll() is None:
                try:
                    if p is core:
                        (home / "serve.stop").write_text("stop")
                        p.wait(10)
                except Exception:
                    pass
                if p.poll() is None:
                    p.kill()
        shutil.rmtree(clips_dir, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
