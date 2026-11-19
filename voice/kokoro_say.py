"""Kokoro text-to-speech for Waggle (local, free). Hermes calls this as a TTS "command provider".

    kokoro_say.py --in text.txt --out reply.wav [--voice bm_george] [--speed 1.15]
    kokoro_say.py --serve        keep the model loaded on 127.0.0.1:8899 (started at login)

Each call first asks the warm server (no model load, ~1.3 s faster); if it isn't running, the model
is loaded in-process instead. Runs in its own venv (~/.waggle/kokoro/venv) so Hermes's locked
environment is never touched. Model files live in ~/.waggle/kokoro/models."""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

MODELS = Path(os.environ.get("WAGGLE_KOKORO_MODELS", Path.home() / ".waggle" / "kokoro" / "models"))
PORT = int(os.environ.get("WAGGLE_KOKORO_PORT", "8899"))


def _fix_speed_dtype(k):
    """kokoro-onnx 0.4.7 sends `speed` as int32 for the v1.0 export, which wants float32 (and int32
    would also truncate 1.15 to 1). Re-cast it on the way into the ONNX session."""
    import numpy as np
    run = k.sess.run

    def patched(outputs, inputs, *rest, **kw):
        if "speed" in inputs:
            inputs = {**inputs, "speed": np.asarray(k._waggle_speed, dtype=np.float32).reshape(1)}
        return run(outputs, inputs, *rest, **kw)
    k.sess.run = patched


def load():
    from kokoro_onnx import Kokoro
    k = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
    k._waggle_speed = 1.0
    _fix_speed_dtype(k)
    return k


def synth(k, text, out, voice, speed):
    import soundfile as sf
    k._waggle_speed = speed
    samples, rate = k.create(text, voice=voice, speed=speed, lang="en-gb" if voice.startswith("b") else "en-us")
    sf.write(out, samples, rate)
    return len(samples) / rate


def serve():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    k = load()
    lock = threading.Lock()
    import tempfile
    synth(k, "Ready.", str(Path(tempfile.gettempdir()) / "waggle-kokoro-warm.wav"), "bm_george", 1.0)  # warm-up

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                with lock:
                    secs = synth(k, req["text"], req["out"], req.get("voice", "bm_george"), float(req.get("speed", 1.15)))
                body, code = json.dumps({"ok": True, "seconds": secs}).encode(), 200
            except Exception as e:  # noqa: BLE001
                body, code = json.dumps({"ok": False, "error": str(e)}).encode(), 500
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    print(f"kokoro warm on 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


def via_server(text, out, voice, speed):
    data = json.dumps({"text": text, "out": str(Path(out).resolve()), "voice": voice, "speed": speed}).encode()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/", data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:  # noqa: BLE001  (server down or busy: fall back to in-process)
        return False


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--serve", action="store_true")
    p.add_argument("--in", dest="inp", help="text file (default: read stdin)")
    p.add_argument("--text")
    p.add_argument("--out")
    p.add_argument("--voice", default="bm_george")
    p.add_argument("--speed", type=float, default=1.15)
    a = p.parse_args(argv)
    if a.serve:
        serve()
        return 0
    if not a.out:
        p.error("--out is required")
    text = a.text or (Path(a.inp).read_text(encoding="utf-8") if a.inp else sys.stdin.read())
    text = " ".join(text.split())
    if not text:
        return 1
    t0 = time.time()
    if via_server(text, a.out, a.voice, a.speed):
        print(f"warm server: {time.time() - t0:.1f}s", file=sys.stderr)
        return 0
    secs = synth(load(), text, a.out, a.voice, a.speed)
    print(f"{secs:.1f}s of audio in {time.time() - t0:.1f}s (cold)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
