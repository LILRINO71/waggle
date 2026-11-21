"""A real end-to-end check of the PC voice without a person speaking (manual; not collected by pytest).

    python tools/pcvoice_check.py "What is seven times eight? One short sentence, please."  [--work DIR]

  1. A spoken request is synthesised with the local speech server (127.0.0.1:8899) and lowered to the level the
     webcam microphone records speech at (peak about 8% of full scale), so the recogniser's level normalisation
     is used as it would be with the real microphone.
  2. This process is the core: the core's own HTTP server (POST /voice) with the real engine and the real `claude`
     CLI on Haiku, in a throwaway Waggle home (one Claude call per request). The sender loop is not started, so
     the written copy only waits in that home's outbox: nothing is sent to Telegram.
  3. `pythonw -m waggle.pcvoice once --inject <file>` runs the real recording path with the file in place of the
     microphone (paced in real time, as a microphone would deliver it): start tone, endpointing, POST /voice,
     the reply played on the real speakers, the follow-up window ending on silence.
  4. The same once more with the core stopped: the pre-made "can't reach the core" line must be spoken.
Prints one JSON line per step with the timings; end of speech to the start of Waggle's voice is
`end_of_speech_to_voice_s`. Needs `python -m waggle.guard approve --yes` in this checkout for the WAGGLE_HOME it
uses (printed at start) when the turn may use tools.

Run it under tools/window_probe.py (--run) to measure windows, focus changes and the busy cursor of a full cycle."""
import argparse
import array
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NO_WINDOW = 0x08000000


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lowered(src, dst, peak=0.08):
    with wave.open(str(src), "rb") as w:
        params, raw = w.getparams(), w.readframes(w.getnframes())
    s = array.array("h")
    s.frombytes(raw)
    g = peak * 32767 / max(1, max(abs(v) for v in s))
    with wave.open(str(dst), "wb") as w:
        w.setparams(params)
        w.writeframes(array.array("h", (int(v * g) for v in s)).tobytes())
    return dst


def once(env, inject):
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    r = subprocess.run([str(pyw), "-m", "waggle.pcvoice", "once", "--inject", str(inject)], cwd=str(REPO), env=env,
                       capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    return json.loads(lines[-1]) if lines else {"error": (r.stderr or r.stdout)[-400:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="+")
    ap.add_argument("--work", default=str(REPO.parent / ".pcvoice-tmp"))
    ap.add_argument("--home", help="use this (empty) home, e.g. one already approved with waggle.guard")
    args = ap.parse_args()
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    home = Path(args.home) if args.home else work / f"check-home-{os.getpid()}"
    home.mkdir(parents=True, exist_ok=True)
    port = free_port()
    os.environ.update(WAGGLE_HOME=str(home), WAGGLE_CORE_PORT=str(port), WAGGLE_ORIGIN="test")
    sys.path.insert(0, str(REPO))
    from waggle.config import Settings
    from waggle.core import procs
    procs.install_quiet_children()  # as the core does: every child it starts (claude, ffmpeg) has no window
    s = Settings.load()  # keeps what is already in the home's config (an approved home must not change)
    s.vault, s.jobs_dir, s.agent_workdir = str(home / "vault"), str(home / "jobs"), str(home / "work")
    s.quiet_hours, s.chat_model, s.engine_chats = [0, 0], "haiku", ["4242"]
    (home / "work").mkdir(exist_ok=True)
    s.save()
    print(json.dumps({"home": str(home), "port": port}), flush=True)
    from waggle import voice_reply
    from waggle.core import health, inbound, outbox, voiceline
    inbound.register(lambda m: {})
    voiceline.token()
    env = {**os.environ}
    for n, text in enumerate(args.text):
        raw = work / f"request{n}-raw.wav"
        voice_reply.synth_wav(text, raw, "af_heart", 1.0, 30)
        inject = lowered(raw, work / f"request{n}.wav")
        raw.unlink(missing_ok=True)
        server = health.serve(port=port, accepting_fn=lambda: True)
        t0 = time.time()
        result = once(env, inject)
        server.shutdown()
        server.server_close()
        copies = [{"state": r["state"], "silent": (r.get("meta") or {}).get("silent"),
                   "via": (r.get("meta") or {}).get("via"), "chars": len(r["body"] or "")}
                  for r in outbox.list(limit=20) if r["origin"] == "voice"]
        print(json.dumps({"step": f"request {n + 1}", "text": text, "wall_s": round(time.time() - t0, 1),
                          **result, "telegram_copy_rows": copies}), flush=True)
        inject.unlink(missing_ok=True)
    # the core down: the pre-made line must be heard
    raw = _synth(voice_reply, work, "Are you there?")
    inject = lowered(raw, work / "down.wav")
    raw.unlink(missing_ok=True)
    t0 = time.time()
    result = once(env, inject)
    print(json.dumps({"step": "core down", "wall_s": round(time.time() - t0, 1), **result}), flush=True)
    inject.unlink(missing_ok=True)
    if not args.home:
        shutil.rmtree(home, ignore_errors=True)


def _synth(voice_reply, work, text):
    p = work / "down-raw.wav"
    voice_reply.synth_wav(text, p, "af_heart", 1.0, 30)
    return p


if __name__ == "__main__":
    main()
