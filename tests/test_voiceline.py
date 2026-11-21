"""The hands-free line (POST /voice on the core's HTTP server): token, one request at a time, text and audio in,
M4A out, the holding line after the hold time, the Telegram copy through the outbox, and the token's secrecy.
The engine is the real one with a fake `claude` process and a fake speech server; ffmpeg is the real one when it is
installed (the tests that need it skip otherwise)."""
import base64
import http.client
import json
import logging
import math
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import wave
from io import BytesIO
from pathlib import Path

import pytest
from engine_fakes import FakeClaude

import waggle.config as config
from waggle import chat_engine, stt, traces, voice_reply
from waggle.chat_session import ChatSession, SessionPool
from waggle.core import health, inbound, outbox, store, voiceline

REPO = Path(__file__).resolve().parents[1]
CHAT = "telegram:4242"
REPLY = "SPOKEN: The reminder is set, sir.\nANSWER:\nReminder set for ten minutes from now."
HAS_FFMPEG = voice_reply.find_ffmpeg() is not None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tone_wav(out, seconds=0.6):
    rate = 24000
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(int(rate * seconds))))


class Line:
    """A core's health server in this process, a fake `claude`, a fake speech server."""

    def __init__(self, settings, monkeypatch):
        self.settings, self.prompts, self.slow_s, self.synth_calls = settings, [], 0.0, []
        self.reply = REPLY
        settings.engine_chats = ["4242"]
        settings.edit_interval_s = 0.05
        settings.save()
        monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")
        monkeypatch.setattr(chat_engine, "claude_available", lambda s: (True, ""))
        monkeypatch.setattr(voice_reply, "synth_wav", self._synth)
        fake = FakeClaude(self._script)
        chat_engine.set_pool(SessionPool(factory=lambda chat: ChatSession(chat, settings=settings, popen=fake,
                                                                          claude="claude"), settings=settings))
        inbound.register(lambda m: {})  # the core "accepts turns" once an inbound handler exists
        self.token = voiceline.token()
        self.port = _free_port()
        self.server = health.serve(port=self.port)

    def _script(self, proc, text):
        self.prompts.append(text)
        if "SLOWTURN" in text:
            time.sleep(self.slow_s)
        proc.answer(self.reply)

    def _synth(self, text, out, voice, speed, timeout):
        self.synth_calls.append(text)
        tone_wav(out)
        return 0.6

    def post(self, body, *, content_type="application/json", token="own", query="", headers=None, raw=None):
        """(status, headers dict, body bytes)."""
        data = raw if raw is not None else (json.dumps(body).encode() if not isinstance(body, bytes) else body)
        h = {"Content-Type": content_type, **(headers or {})}
        if token == "own":
            h["Authorization"] = f"Bearer {self.token}"
        elif token:
            h["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            conn.request("POST", "/voice" + query, body=data, headers=h)
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        inbound.register(None)
        chat_engine.set_pool(None)
        store.close_all()


@pytest.fixture
def line(settings, monkeypatch):
    ln = Line(settings, monkeypatch)
    yield ln
    ln.close()


def spoken_of(headers):
    return urllib.parse.unquote(headers["x-waggle-spoken"])


def kind(data):
    return voice_reply.sniff(data)


# ---- the token -----------------------------------------------------------------------------------------------

def test_the_voice_token_is_32_random_bytes_url_safe_in_its_own_file(line):
    path = Path(config.HOME) / "voice.token"
    tok = path.read_text(encoding="ascii")
    assert tok == line.token and len(base64.urlsafe_b64decode(tok + "=")) == 32
    assert voiceline.token() == tok  # asking again never makes a second one
    assert tok != (Path(config.HOME) / "core.token").read_text(encoding="ascii")


def test_without_the_token_it_is_401(line):
    assert line.post({"text": "hello"}, token=None)[0] == 401
    assert line.post({"text": "hello"}, token="wrong")[0] == 401
    assert line.prompts == []  # nothing ran


def test_the_core_admin_token_does_not_open_the_line_and_the_voice_token_opens_nothing_else(line):
    admin = (Path(config.HOME) / "core.token").read_text(encoding="ascii")
    assert line.post({"text": "hello"}, token=admin)[0] == 401
    conn = http.client.HTTPConnection("127.0.0.1", line.port, timeout=10)
    conn.request("GET", "/health", headers={"Authorization": f"Bearer {line.token}"})
    assert conn.getresponse().status == 401
    conn.close()
    conn = http.client.HTTPConnection("127.0.0.1", line.port, timeout=10)
    conn.request("POST", "/turn", body=b'{"chat":"x","text":"y"}',
                 headers={"Authorization": f"Bearer {line.token}", "Content-Type": "application/json"})
    assert conn.getresponse().status == 401
    conn.close()


def test_a_missing_token_file_means_the_line_is_closed(line):
    (Path(config.HOME) / "voice.token").unlink()
    assert line.post({"text": "hello"})[0] == 401


def test_the_token_is_never_logged_or_returned(line, caplog):
    caplog.set_level(logging.DEBUG)
    code, headers, data = line.post({"text": "Remind me to call Sam."})
    wrong = line.post({"text": "x"}, token="not-it")
    everything = repr((headers, data, wrong)) + caplog.text
    assert code == 200 and line.token not in everything


def test_voice_setup_prints_the_path_and_the_token(tmp_path):
    home = tmp_path / "setuphome"
    env = dict(os.environ, WAGGLE_HOME=str(home), PYTHONPATH=str(REPO))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    r = subprocess.run([sys.executable, "-m", "waggle.core", "voice-setup"], cwd=str(REPO), env=env,
                       capture_output=True, text=True, timeout=60, creationflags=flags)
    tok = (home / "voice.token").read_text(encoding="ascii")
    assert r.returncode == 0 and "/voice" in r.stdout and tok in r.stdout and r.stderr.strip() == ""
    again = subprocess.run([sys.executable, "-m", "waggle.core", "voice-setup"], cwd=str(REPO), env=env,
                           capture_output=True, text=True, timeout=60, creationflags=flags)
    assert tok in again.stdout  # the same token every time


# ---- one at a time, size and shape -------------------------------------------------------------------------

def test_a_second_request_while_one_runs_is_429(line):
    line.slow_s = 1.5
    first = {}
    t = threading.Thread(target=lambda: first.update(r=line.post({"text": "SLOWTURN please"})))
    t.start()
    deadline = time.time() + 20
    while not line.prompts and time.time() < deadline:
        time.sleep(0.02)
    code, headers, data = line.post({"text": "and another"})
    t.join(30)
    assert code == 429 and json.loads(data)["error"] and headers.get("retry-after")
    assert first["r"][0] == 200
    assert line.post({"text": "after it ended"})[0] == 200  # free again


def test_a_body_over_two_megabytes_is_413_and_never_reaches_the_engine(line):
    code, _, data = line.post(b"x" * (2 * 1024 * 1024 + 1), content_type="audio/mp4")
    assert code == 413 and line.prompts == []


def test_a_body_of_exactly_two_megabytes_is_accepted_as_audio(line, monkeypatch):
    seen = {}
    monkeypatch.setattr(chat_engine.stt, "transcribe", lambda p, **kw: seen.update(size=Path(p).stat().st_size) or {
        "ok": True, "text": "Remind me to call Sam.", "error": "", "seconds": 0.1, "load_s": 0.0, "audio_s": 1.0,
        "source": "fake", "confidence": stt.confidence([])})
    code, _, _ = line.post(b"\0" * (2 * 1024 * 1024), content_type="audio/mp4")
    assert code == 200 and seen["size"] == 2 * 1024 * 1024


@pytest.mark.parametrize("ctype,body,code", [("text/plain", b"hello", 415), ("application/json", b"not json", 400),
                                             ("application/json", b'{"text": ""}', 400),
                                             ("application/json", b'{"text": 5}', 400),
                                             ("application/json", b'[1]', 400),
                                             ("application/json", b'{"text": "' + b"a" * 4001 + b'"}', 400)])
def test_bad_requests_are_refused(line, ctype, body, code):
    assert line.post(body, content_type=ctype)[0] == code and line.prompts == []


def test_no_content_length_is_411():
    class Rfile:
        def read(self, n):
            raise AssertionError("must not read")
    code, _, _ = voiceline.respond({"Authorization": f"Bearer {voiceline.token()}", "Content-Type": "application/json"},
                                   Rfile())
    assert code == 411


def test_it_stays_bound_to_localhost(line):
    assert line.server.server_address[0] == "127.0.0.1"


# ---- text in, audio out ------------------------------------------------------------------------------------

def test_text_in_gives_the_spoken_answer_as_audio(line):
    code, headers, data = line.post({"text": "Um, remind me to call Sam in, uh, ten minutes."})
    assert code == 200
    assert spoken_of(headers) == "The reminder is set, sir." and headers["x-waggle-status"] == "done"
    assert kind(data) == ("m4a" if HAS_FFMPEG else "wav")
    assert headers["content-type"] == ("audio/mp4" if HAS_FFMPEG else "audio/wav")
    assert headers["content-disposition"].endswith('.m4a"' if HAS_FFMPEG else '.wav"')
    assert line.synth_calls == ["The reminder is set, sir."]  # the Heard line is never spoken
    assert line.prompts[-1].rstrip().endswith("Remind me to call Sam in, ten minutes.")  # the cleaned words


def test_the_turn_is_a_user_voice_turn_in_the_owners_chat(line):
    code, headers, data = line.post({"text": "Remind me to call Sam in ten minutes."})
    row = traces.by_turn_id(headers["x-waggle-turn"])
    assert row["origin"] == "user" and row["chat"] == CHAT and row["session"] == CHAT
    v = json.loads(row["extra"])["voice_turn"]
    assert v["transcript_source"] == "phone dictation" and v["voice"] is True
    assert v["transcript_raw"] == "Remind me to call Sam in ten minutes."
    assert v["stt_confident"] is False  # dictation brings no recogniser numbers
    assert "<voice_reply_format>" in line.prompts[-1]  # the same spoken-and-written format as a Telegram note


def test_the_line_talks_in_the_chat_set_in_the_settings(line, tmp_path):
    cfg = json.loads(Path(config.CONFIG_FILE).read_text(encoding="utf-8"))
    cfg["voice_line_chat"] = "9001"
    Path(config.CONFIG_FILE).write_text(json.dumps(cfg), encoding="utf-8")
    code, headers, _ = line.post({"text": "hello there"})
    assert traces.by_turn_id(headers["x-waggle-turn"])["chat"] == "telegram:9001"


def test_no_engine_chat_is_503(line):
    cfg = json.loads(Path(config.CONFIG_FILE).read_text(encoding="utf-8"))
    cfg["engine_chats"] = []
    Path(config.CONFIG_FILE).write_text(json.dumps(cfg), encoding="utf-8")
    code, _, data = line.post({"text": "hello"})
    assert code == 503 and "engine" in json.loads(data)["error"]


def test_reply_json_gives_spoken_and_text_and_no_audio(line):
    code, headers, data = line.post({"text": "Remind me to call Sam."}, query="?reply=json")
    body = json.loads(data)
    assert code == 200 and headers["content-type"] == "application/json" and set(body) == {"spoken", "text"}
    assert body["spoken"] == "The reminder is set, sir."
    assert body["text"] == 'Heard: "Remind me to call Sam."\n\nReminder set for ten minutes from now.'
    assert line.synth_calls == []  # no speech is made for JSON


def test_a_reply_without_a_spoken_part_is_still_spoken(line):
    line.reply = "The build passed. All forty tests ran. Nothing failed. Deploy at five."
    code, headers, data = line.post({"text": "How did the build go?"})
    assert code == 200 and spoken_of(headers) == "The build passed. All forty tests ran. Nothing failed."
    assert kind(data) in ("m4a", "wav")


def test_a_direct_reply_is_spoken_too(line):
    code, headers, data = line.post({"text": "Approve zzzz."})  # answered without the model
    assert code == 200 and "approval" in spoken_of(headers) and kind(data) in ("m4a", "wav")
    assert line.prompts == []


def test_with_no_speech_server_the_spoken_text_still_arrives_as_json(line, monkeypatch):
    def down(*a, **k):
        raise ConnectionError("the speech server is off")
    monkeypatch.setattr(voice_reply, "synth_wav", down)
    code, headers, data = line.post({"text": "Remind me to call Sam."})
    assert code == 200 and headers["x-waggle-audio"] == "unavailable" and json.loads(data)["spoken"]


# ---- audio in ----------------------------------------------------------------------------------------------

def test_audio_in_is_transcribed_by_waggle_and_the_file_is_removed(line, monkeypatch):
    seen = {}

    def fake(path, **kw):
        seen.update(path=path, bytes=Path(path).read_bytes(), suffix=Path(path).suffix)
        return {"ok": True, "text": "Um, remind me to call Sam in ten minutes.", "error": "", "seconds": 0.3,
                "load_s": 0.0, "audio_s": 2.0, "source": "fake whisper", "confidence": {
                    "confident": True, "avg_logprob": -0.2, "no_speech_prob": 0.01, "compression_ratio": 1.2,
                    "segments": 1, "dropped": 0, "why": []}}
    monkeypatch.setattr(chat_engine.stt, "transcribe", fake)
    code, headers, data = line.post(b"FAKEAUDIO", content_type="audio/mp4")
    assert code == 200 and seen["bytes"] == b"FAKEAUDIO" and seen["suffix"] == ".m4a"
    assert not Path(seen["path"]).exists()
    assert line.prompts[-1].rstrip().endswith("Remind me to call Sam in ten minutes.")
    v = json.loads(traces.by_turn_id(headers["x-waggle-turn"])["extra"])["voice_turn"]
    assert v["transcript_source"] == "fake whisper" and v["stt_confident"] is True
    assert v["transcript_raw"].startswith("Um,") and v["transcript_cleaned"].startswith("Remind me")


def test_audio_that_cannot_be_heard_is_answered_aloud_not_with_an_error(line, monkeypatch):
    monkeypatch.setattr(chat_engine.stt, "transcribe", lambda p, **kw: {
        "ok": False, "text": "", "error": "no speech was recognised", "seconds": 0.1, "load_s": 0.0, "audio_s": 0.0,
        "source": "fake", "confidence": stt.confidence([])})
    code, headers, data = line.post(b"silence", content_type="audio/wav")
    assert code == 200 and "make out" in spoken_of(headers) and kind(data) in ("m4a", "wav")


# ---- the Telegram copy ---------------------------------------------------------------------------------------

def sent_copies():
    return [r for r in outbox.list(limit=100) if r["origin"] == "voice"]


def test_the_full_answer_is_also_queued_to_telegram(line):
    code, headers, _ = line.post({"text": "Remind me to call Sam."})
    rows = sent_copies()
    assert len(rows) == 1 and rows[0]["kind"] == "text" and rows[0]["chat"] == CHAT
    assert rows[0]["body"] == 'Heard: "Remind me to call Sam."\n\nReminder set for ten minutes from now.'
    assert rows[0]["idem_key"] == "voice:" + headers["x-waggle-turn"]


def test_the_copy_can_be_switched_off(line):
    cfg = json.loads(Path(config.CONFIG_FILE).read_text(encoding="utf-8"))
    cfg["voice_line_telegram_copy"] = False
    Path(config.CONFIG_FILE).write_text(json.dumps(cfg), encoding="utf-8")
    assert line.post({"text": "Remind me to call Sam."})[0] == 200
    assert sent_copies() == []


# ---- a turn that takes long -----------------------------------------------------------------------------------

def test_a_turn_not_finished_in_time_gets_the_holding_line_and_then_its_answer_goes_to_telegram(line, monkeypatch):
    monkeypatch.setenv("WAGGLE_VOICE_HOLD_S", "0.4")
    line.slow_s = 1.5
    t0 = time.time()
    code, headers, data = line.post({"text": "SLOWTURN do the long job"})
    assert code == 200 and time.time() - t0 < 1.4
    assert spoken_of(headers) == voiceline.HOLD_LINE == "Working on it, sir. I will message you when it is done."
    assert headers["x-waggle-status"] == "working" and kind(data) in ("m4a", "wav")
    assert sent_copies() == []  # not yet: the turn is still running
    deadline = time.time() + 30
    while not sent_copies() and time.time() < deadline:
        time.sleep(0.05)
    rows = sent_copies()
    assert len(rows) == 1 and rows[0]["chat"] == CHAT and "Reminder set" in rows[0]["body"]
    assert rows[0]["body"].startswith('Heard: "SLOWTURN do the long job"')


def test_the_holding_line_is_also_given_as_json_and_the_answer_still_goes_to_telegram_with_the_copy_off(line, monkeypatch):
    monkeypatch.setenv("WAGGLE_VOICE_HOLD_S", "0.3")
    cfg = json.loads(Path(config.CONFIG_FILE).read_text(encoding="utf-8"))
    cfg["voice_line_telegram_copy"] = False
    Path(config.CONFIG_FILE).write_text(json.dumps(cfg), encoding="utf-8")
    line.slow_s = 1.0
    code, headers, data = line.post({"text": "SLOWTURN again"}, query="?reply=json")
    assert json.loads(data) == {"spoken": voiceline.HOLD_LINE, "text": voiceline.HOLD_LINE}
    deadline = time.time() + 30
    while not sent_copies() and time.time() < deadline:
        time.sleep(0.05)
    assert len(sent_copies()) == 1  # the answer had nowhere else to go


def test_the_request_lock_is_free_again_while_the_long_turn_carries_on(line, monkeypatch):
    monkeypatch.setenv("WAGGLE_VOICE_HOLD_S", "0.3")
    line.slow_s = 1.2
    assert line.post({"text": "SLOWTURN one"})[0] == 200
    assert line.post({"text": "two"})[0] != 429


# ---- a broken engine ------------------------------------------------------------------------------------------

def test_an_engine_that_raises_is_a_spoken_apology(line, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("engine bug")
    monkeypatch.setattr(chat_engine, "handle_inbound", boom)
    code, headers, data = line.post({"text": "hello"})
    assert code == 200 and headers["x-waggle-status"] == "error" and "could not answer" in spoken_of(headers)


# ---- the real ffmpeg -------------------------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_FFMPEG, reason="no ffmpeg under ~/.hermes/tools")
def test_the_audio_is_an_m4a_with_a_valid_ftyp_box_that_ffmpeg_can_read(line, tmp_path):
    code, headers, data = line.post({"text": "Remind me to call Sam."})
    size = struct.unpack(">I", data[:4])[0]
    assert code == 200 and data[4:8] == b"ftyp" and 16 <= size <= len(data)
    assert data[8:12] in (b"M4A ", b"isom", b"mp42", b"M4B ")
    f = tmp_path / "reply.m4a"
    f.write_bytes(data)
    assert 0.4 <= voice_reply.ogg_seconds(f) <= 1.2  # the reader works on any audio file; the tone is 0.6 s
    assert b"moov" in data and b"mdat" in data


def test_without_ffmpeg_the_audio_is_a_wav(line, monkeypatch):
    monkeypatch.setattr(voice_reply, "find_ffmpeg", lambda: None)
    code, headers, data = line.post({"text": "Remind me to call Sam."})
    assert code == 200 and kind(data) == "wav" and headers["content-type"] == "audio/wav"
    with wave.open(BytesIO(data)) as w:
        assert w.getnframes() > 0


def test_the_defaults_are_the_documented_ones(line):
    assert voiceline.HOLD_AFTER_S == 25.0 and voiceline.hold_after_s() == 25.0
    assert voiceline.MAX_BODY == 2 * 1024 * 1024 and voiceline.telegram_copy() is True
    assert voiceline.HOLD_LINE == "Working on it, sir. I will message you when it is done."


def test_with_several_engine_chats_the_first_one_is_the_owners(line):
    cfg = json.loads(Path(config.CONFIG_FILE).read_text(encoding="utf-8"))
    cfg["engine_chats"] = ["777", "123"]
    Path(config.CONFIG_FILE).write_text(json.dumps(cfg), encoding="utf-8")
    assert voiceline.owner_chat() == "telegram:123"


# ---- the PC voice's options -----------------------------------------------------------------------------------

def copy_meta():
    rows = sent_copies()
    assert len(rows) == 1
    return rows[0]["meta"]


def test_format_wav_gives_the_speech_servers_wav_without_conversion(line, monkeypatch):
    monkeypatch.setattr(voice_reply, "to_m4a", lambda *a, **k: pytest.fail("no AAC encoding for the PC"))
    code, headers, data = line.post({"text": "Remind me to call Sam."}, query="?format=wav&client=pc")
    assert code == 200 and headers["content-type"] == "audio/wav" and kind(data) == "wav"
    assert headers["content-disposition"].endswith('.wav"')
    with wave.open(BytesIO(data)) as w:
        assert w.getnframes() > 0
    assert spoken_of(headers) == "The reminder is set, sir."


def test_the_holding_line_also_comes_as_wav(line, monkeypatch):
    monkeypatch.setenv("WAGGLE_VOICE_HOLD_S", "0.3")
    line.slow_s = 1.0
    code, headers, data = line.post({"text": "SLOWTURN long"}, query="?format=wav&client=pc")
    assert code == 200 and headers["x-waggle-status"] == "working" and kind(data) == "wav"
    deadline = time.time() + 30  # let the turn finish here, so its copy cannot land in the next test's home
    while not sent_copies() and time.time() < deadline:
        time.sleep(0.05)


def test_pc_text_is_labelled_as_the_wake_word_and_its_copy_is_silent(line):
    code, headers, _ = line.post({"text": "What time is it?"}, query="?format=wav&client=pc")
    v = json.loads(traces.by_turn_id(headers["x-waggle-turn"])["extra"])["voice_turn"]
    assert v["transcript_source"] == "PC wake word" and v["stt_confident"] is False
    meta = copy_meta()
    assert meta["silent"] is True and meta["via"] == "pc voice" and "speak_pc" not in meta


def test_a_phone_request_is_unchanged_by_the_pc_options(line):
    code, headers, _ = line.post({"text": "What time is it?"}, query="?client=unknown")
    v = json.loads(traces.by_turn_id(headers["x-waggle-turn"])["extra"])["voice_turn"]
    assert v["transcript_source"] == "phone dictation"
    meta = copy_meta()
    assert "silent" not in meta and "speak_pc" not in meta and meta["via"] == "voice line"


def test_a_pc_answer_after_the_holding_line_is_marked_to_be_spoken_on_the_pc(line, monkeypatch):
    monkeypatch.setenv("WAGGLE_VOICE_HOLD_S", "0.3")
    line.slow_s = 1.0
    code, headers, _ = line.post({"text": "SLOWTURN long"}, query="?format=wav&client=pc")
    assert headers["x-waggle-status"] == "working"
    deadline = time.time() + 30
    while not sent_copies() and time.time() < deadline:
        time.sleep(0.05)
    meta = copy_meta()
    assert meta["speak_pc"] is True and "silent" not in meta


def transcribe_post(line, body, *, token="own", content_type="audio/wav"):
    h = {"Content-Type": content_type}
    if token:
        h["Authorization"] = f"Bearer {line.token if token == 'own' else token}"
    conn = http.client.HTTPConnection("127.0.0.1", line.port, timeout=60)
    try:
        conn.request("POST", "/voice/transcribe", body=body, headers=h)
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def test_transcribe_gives_the_words_runs_no_turn_and_keeps_nothing(line, monkeypatch, caplog):
    seen = {}

    def fake(path, **kw):
        seen.update(path=path, bytes=Path(path).read_bytes())
        return {"ok": True, "text": "Waggle what is the weather", "error": "", "seconds": 0.2, "load_s": 0.0,
                "audio_s": 1.5, "source": "fake", "confidence": {"confident": True}}
    monkeypatch.setattr(stt, "transcribe", fake)
    caplog.set_level(logging.DEBUG)
    code, data = transcribe_post(line, b"RIFFfakeaudio")
    body = json.loads(data)
    assert code == 200 and body["text"] == "Waggle what is the weather" and body["ok"] and body["confident"]
    assert seen["bytes"] == b"RIFFfakeaudio" and not Path(seen["path"]).exists()
    assert line.prompts == [] and outbox.list(limit=10) == []
    assert "weather" not in caplog.text


def test_transcribe_needs_the_voice_token_and_audio(line, monkeypatch):
    monkeypatch.setattr(stt, "transcribe", lambda p, **k: pytest.fail("not reached"))
    assert transcribe_post(line, b"x", token="")[0] == 401
    assert transcribe_post(line, b"x", token="wrong")[0] == 401
    assert transcribe_post(line, b'{"text": "hi"}', content_type="application/json")[0] == 415


def test_a_failed_transcription_still_deletes_the_audio(line, monkeypatch):
    seen = {}

    def boom(path, **kw):
        seen["path"] = path
        raise RuntimeError("model gone")
    monkeypatch.setattr(stt, "transcribe", boom)
    code, _ = transcribe_post(line, b"RIFFx")
    assert code == 500 and not Path(seen["path"]).exists()
