"""The hands-free line against a real core process in a temporary Waggle home: the core creates voice.token when it
starts, /voice needs that token (not the admin token), a text request comes back as audio, the written answer is
delivered to Telegram by the core's own sender (a fake transport), and a slow turn gets the holding line."""
import base64
import http.client
import json
import os
import threading
import time
import urllib.parse
from pathlib import Path

import pytest
from test_runtime_core import Harness, core, wait_for  # noqa: F401 - `core` is the shared fixture

import waggle.config as config
from waggle import voice_reply
from waggle.config import Settings
from waggle.core import health, outbox

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the core targets Windows")
PLUGIN = "test_voiceline_core_plugin"
CHAT = "telegram:4242"


def start(core, **env):
    s = Settings.load()
    s.engine_chats = ["4242"]
    s.save()
    return core.start(plugins=PLUGIN, extra_env=env or None)


def post(body, token, *, query="", content_type="application/json"):
    data = json.dumps(body).encode() if not isinstance(body, bytes) else body
    conn = http.client.HTTPConnection("127.0.0.1", health.settings.port(), timeout=120)
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        conn.request("POST", "/voice" + query, body=data, headers=headers)
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        conn.close()


def voice_token(core):
    return wait_for(lambda: (core.home / "voice.token").exists() and
                    (core.home / "voice.token").read_text(encoding="ascii").strip(), what="voice.token")


def test_the_core_creates_the_voice_token_at_start_and_it_is_not_the_admin_token(core):
    assert not (core.home / "voice.token").exists()
    start(core)
    tok = voice_token(core)
    assert len(base64.urlsafe_b64decode(tok + "=")) == 32
    assert tok != (core.home / "core.token").read_text(encoding="ascii").strip()
    assert (core.home / "voice.token").read_text(encoding="ascii").strip() == tok  # stable across reads
    assert core.cli("voice-setup").stdout.count(tok) == 1


def test_a_restart_keeps_the_same_token(core):
    start(core)
    first = voice_token(core)
    core.cli("restart")
    wait_for(lambda: health.fetch("/health", timeout=10)[0] == 200, what="the restarted core")
    assert (core.home / "voice.token").read_text(encoding="ascii").strip() == first


def test_401_without_the_voice_token_and_with_the_admin_token(core):
    start(core)
    tok = voice_token(core)
    admin = (core.home / "core.token").read_text(encoding="ascii").strip()
    assert post({"text": "hello"}, None)[0] == 401
    assert post({"text": "hello"}, admin)[0] == 401
    assert post({"text": "hello"}, tok[::-1])[0] == 401


def test_text_in_audio_out_and_the_written_answer_reaches_telegram_through_the_sender(core):
    start(core)
    tok = voice_token(core)
    code, headers, data = post({"text": "Um, remind me to call Sam in ten minutes."}, tok)
    assert code == 200 and urllib.parse.unquote(headers["x-waggle-spoken"]) == "The reminder is set, sir."
    assert voice_reply.sniff(data) == ("m4a" if voice_reply.find_ffmpeg() else "wav")
    sent = wait_for(lambda: core.lines("sent.log"), what="the Telegram copy")
    chat_id, body = sent[0].split("\t")[1:]
    assert chat_id == CHAT
    assert body == 'Heard: "Remind me to call Sam in ten minutes."||Reminder set for ten minutes from now.'
    row = wait_for(lambda: [r for r in outbox.list(limit=50) if r["origin"] == "voice" and r["state"] == "sent"],
                   what="the copy marked sent")
    assert row[0]["idem_key"] == "voice:" + headers["x-waggle-turn"]
    assert core.lines("speech.log") == ["The reminder is set, sir."]
    assert "Remind me to call Sam in ten minutes." in core.lines("prompts.log")[0]


def test_a_second_request_during_a_turn_is_429(core):
    start(core)
    tok = voice_token(core)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=post({"text": "SLOWTURN one"}, tok)))
    t.start()
    wait_for(lambda: core.lines("prompts.log"), what="the first turn to start")
    assert post({"text": "two"}, tok)[0] == 429
    t.join(60)
    assert out["r"][0] == 200


def test_a_slow_turn_gets_the_holding_line_and_its_answer_goes_to_telegram(core):
    start(core, WAGGLE_VOICE_HOLD_S="1")
    tok = voice_token(core)
    t0 = time.time()
    code, headers, data = post({"text": "SLOWTURN the long job"}, tok)
    assert code == 200 and time.time() - t0 < 2.4
    assert urllib.parse.unquote(headers["x-waggle-spoken"]) == "Working on it, sir. I will message you when it is done."
    assert headers["x-waggle-status"] == "working"
    sent = wait_for(lambda: core.lines("sent.log"), timeout=60, what="the answer in Telegram")
    assert "Reminder set for ten minutes from now." in sent[0] and sent[0].split("\t")[1] == CHAT


def test_the_token_is_nowhere_in_the_core_logs(core):
    start(core)
    tok = voice_token(core)
    assert post({"text": "hello"}, tok)[0] == 200
    assert post({"text": "hello"}, "wrong-" + tok)[0] == 401
    core.cli("status")
    time.sleep(0.5)
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in core.home.glob("*.log")]
    texts += [p.read_text(encoding="utf-8", errors="replace") for p in core.home.glob("*.out")]
    assert texts and not any(tok in t for t in texts)
