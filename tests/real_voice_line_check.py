"""Manual check (not collected by pytest): the hands-free line end to end WITHOUT a phone and WITHOUT Telegram, with
the real core HTTP server (POST /voice on 127.0.0.1), the real speech server (127.0.0.1:8899), the real ffmpeg and the
real `claude` CLI, in a throwaway Waggle home. The sender loop is not started, so the written copy only waits in the
outbox: nothing is sent anywhere.

usage: python tests/real_voice_line_check.py <repo> <empty_temp_home> <out_dir> "<text to say>" [more texts ...]
       (needs `python -m waggle.guard approve --yes` in the repo for this WAGGLE_HOME for a run with tools)
Each text is one real Claude call. The first is answered as audio (the file is saved to <out_dir>), the others as JSON.
Prints one JSON line per request: status, content type, size, kind, audio seconds, the spoken text, the total time.
"""
import http.client
import json
import os
import socket
import sys
import time
import urllib.parse
from pathlib import Path

repo, home, out_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
texts = sys.argv[4:] or ["Remind me to stand up in ten minutes."]
os.environ["WAGGLE_HOME"] = str(home)
os.environ["WAGGLE_ORIGIN"] = "test"
sys.path.insert(0, str(repo))

from waggle.config import Settings  # noqa: E402

work = home / "work"
work.mkdir(parents=True, exist_ok=True)
out_dir.mkdir(parents=True, exist_ok=True)
s = Settings(vault=str(home / "vault"), jobs_dir=str(home / "jobs"), agent_workdir=str(work), quiet_hours=[0, 0],
             chat_model="haiku", engine_chats=["4242"])
s.save()

from waggle import voice_reply  # noqa: E402
from waggle.core import health, outbox, voiceline  # noqa: E402

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
server = health.serve(port=port, accepting_fn=lambda: True)
token = voiceline.token()
print(json.dumps({"server": f"127.0.0.1:{port}", "ffmpeg": bool(voice_reply.find_ffmpeg())}), flush=True)

for n, text in enumerate(texts):
    as_json = n > 0
    t0 = time.time()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
    conn.request("POST", "/voice" + ("?reply=json" if as_json else ""), body=json.dumps({"text": text}).encode(),
                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    r = conn.getresponse()
    data = r.read()
    total = round(time.time() - t0, 2)
    h = {k.lower(): v for k, v in r.getheaders()}
    info = {"request": n + 1, "text_in": text, "status": r.status, "content_type": h.get("content-type"),
            "bytes": len(data), "total_s": total, "x_waggle_status": h.get("x-waggle-status"),
            "spoken_header": urllib.parse.unquote(h.get("x-waggle-spoken", ""))}
    if as_json:
        info.update(json_body=json.loads(data))
    else:
        kind = voice_reply.sniff(data)
        path = out_dir / f"line_reply.{'m4a' if kind == 'm4a' else 'wav'}"
        path.write_bytes(data)
        info.update(kind=kind, saved=str(path), ftyp_box=data[4:12].decode("latin-1"),
                    audio_s=voice_reply.ogg_seconds(path))
    print(json.dumps(info, ensure_ascii=False), flush=True)

rows = [r for r in outbox.list(limit=50) if r["origin"] == "voice"]
print(json.dumps({"queued_telegram_copies": [{"chat": r["chat"], "state": r["state"], "body": r["body"]} for r in rows]},
                 ensure_ascii=False), flush=True)
server.shutdown()
server.server_close()
