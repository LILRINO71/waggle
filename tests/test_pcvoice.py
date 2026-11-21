"""The PC voice with fakes: a WAV file in place of the microphone, a fake player behind the real Speaker queue, a
fake core client, a fake speech server. Nothing here opens the microphone, plays a sound, registers a hotkey or
talks to a real core."""
import array
import http.server
import json
import logging
import math
import threading
import time

import pytest

from waggle import mic, pcvoice, speaker
from waggle.pcvoice import Reply


# ---- fakes -------------------------------------------------------------------------------------------------

class FakeBackend:
    def __init__(self):
        self.played, self.stops = [], 0

    def play(self, path):
        self.played.append(path.name.split("-")[0].removesuffix(".wav"))

    def stop(self):
        self.stops += 1


class FakeClient:
    def __init__(self, tmp_path, replies=None, transcripts=None, reply_ms=60):
        self.tmp, self.replies, self.transcripts = tmp_path, list(replies or []), list(transcripts or [])
        self.audio, self.texts, self.transcribed, self.reply_ms, self.n = [], [], 0, reply_ms, 0

    def _next(self):
        self.n += 1
        r = self.replies.pop(0) if self.replies else Reply("done")
        if r.status == "done" and r.wav is None and not r.line:
            r.wav = speaker.tone_wav(self.tmp / f"reply{self.n}.wav", ms=self.reply_ms)
        return r

    def voice_audio(self, pcm):
        self.audio.append(pcm)
        return self._next()

    def voice_text(self, text):
        self.texts.append(text)
        return self._next()

    def transcribe(self, pcm):
        self.transcribed += 1
        return {"text": self.transcripts.pop(0) if self.transcripts else "", "ok": True}


def fake_synth(made):
    def synth(text, out):
        made.append(text)
        speaker.tone_wav(out, ms=40)
        return True
    return synth


def speech_file(path, parts):
    """A 16 kHz WAV of (seconds, amplitude) parts: amplitude 0 is silence, anything else a 200 Hz voice-like tone."""
    s, i = array.array("h"), 0
    for seconds, amp in parts:
        for _ in range(int(seconds * mic.RATE)):
            s.append(int(amp * math.sin(2 * math.pi * 200 * i / mic.RATE)))
            i += 1
    return mic.write_wav(path, s.tobytes())


@pytest.fixture
def rig(tmp_path):
    class Rig:
        def __init__(self):
            self.backend = FakeBackend()
            self.spk = speaker.Speaker(self.backend, tail_s=0.0).start()
            self.made = []
            self.sounds = pcvoice.Sounds(synth=fake_synth(self.made))

        def voice(self, client, *, wake=False, names=None, dry_wake=False, source=None):
            self.client = client
            factory = (lambda: source) if source is not None else None
            self.pv = pcvoice.PcVoice(mic_factory=factory, spk=self.spk, client=client, sounds=self.sounds,
                                      wake=wake, names=names or ["Waggle"], dry_wake=dry_wake)
            return self.pv

        def played(self):
            self.spk.wait_idle(5)
            return self.backend.played
    r = Rig()
    yield r
    r.spk.close()


def source(tmp_path, parts, name="in.wav"):
    return mic.FileSource(speech_file(tmp_path / name, parts), trail_s=0.0).open()


# ---- the wake name and the hotkey --------------------------------------------------------------------------

@pytest.mark.parametrize("text, rest", [
    ("Waggle, what's the weather?", "what's the weather?"),
    ("Hey Waggle what time is it", "what time is it"),
    ("hey, waggle. Remind me at six.", "Remind me at six."),
    ("Waggle.", ""),
    ("Waggle's there?", "there?"),
    ("OK Waggle", ""),
    ("I told Waggle about it", None),
    ("The waggle dance of bees", None),
    ("Hey there", None),
    ("", None),
    ("Wiggle the mouse", None),
])
def test_the_wake_name_must_begin_the_words(text, rest):
    assert pcvoice.match_wake(text, ["Waggle"]) == rest


def test_the_wake_name_is_a_setting_and_can_be_two_words():
    assert pcvoice.match_wake("Jarvis, lights", ["Jarvis"]) == "lights"
    assert pcvoice.match_wake("Waggle, lights", ["Jarvis"]) is None
    assert pcvoice.match_wake("hey big bee open mail", ["Big Bee"]) == "open mail"


def test_hotkeys_parse():
    assert pcvoice.parse_hotkey("ctrl+alt+space") == (0x3, 0x20)
    assert pcvoice.parse_hotkey("Win+Shift+F9") == (0xC, 0x78)
    assert pcvoice.parse_hotkey("ctrl+alt+w") == (0x3, ord("W"))
    for bad in ("ctrl+alt", "ctrl+banana", ""):
        with pytest.raises(ValueError):
            pcvoice.parse_hotkey(bad)


def test_settings_have_the_documented_defaults_and_read_config(settings):
    assert pcvoice.setting("pc_voice_hotkey") == "ctrl+alt+backquote"
    assert pcvoice.setting("pc_voice_wake") is True and pcvoice.setting("pc_voice_wake_names") == ["Waggle"]
    assert pcvoice.setting("pc_voice_end_silence_s") == 1.2 and pcvoice.setting("pc_voice_max_s") == 30.0
    from waggle import config
    data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    data.update(pc_voice_wake_names="Jarvis", pc_voice_wake="off", pc_voice_followup_s="x")
    config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")
    assert pcvoice.setting("pc_voice_wake_names") == ["Jarvis"] and pcvoice.setting("pc_voice_wake") is False
    assert pcvoice.setting("pc_voice_followup_s") == 6.0


# ---- a conversation ----------------------------------------------------------------------------------------

def test_hotkey_tone_record_until_silence_send_play_then_follow_up_ends_on_silence(rig, tmp_path):
    client = FakeClient(tmp_path)
    pv = rig.voice(client)
    src = source(tmp_path, [(0.5, 0), (1.5, 3000), (1.5, 0), (8.0, 0)])
    pv.conversation(src, "hotkey")
    assert rig.played() == ["start", "stop", "reply1"]
    assert len(client.audio) == 1 and abs(len(client.audio[0]) / 2 / mic.RATE - 2.1) < 0.15
    assert pv.stats["turns"] == 1 and not pv.in_conversation


def test_a_follow_up_needs_no_hotkey(rig, tmp_path):
    client = FakeClient(tmp_path)
    pv = rig.voice(client)
    src = source(tmp_path, [(0.3, 0), (1.0, 3000), (1.4, 0), (1.0, 2500), (1.4, 0), (7.0, 0)])
    pv.conversation(src, "hotkey")
    assert rig.played() == ["start", "stop", "reply1", "stop", "reply2"]  # no start tone before the follow-up
    assert len(client.audio) == 2


def test_nobody_speaking_after_the_hotkey_just_ends_with_a_tone(rig, tmp_path):
    client = FakeClient(tmp_path)
    pv = rig.voice(client)
    pv.conversation(source(tmp_path, [(2.0, 0)]), "hotkey")
    assert rig.played() == ["start", "stop"] and client.audio == []


@pytest.mark.parametrize("reply, line", [(Reply("core_down", line="core_down"), "core_down"),
                                         (Reply("busy", line="busy"), "busy"),
                                         (Reply("failed", http=401, line="failed"), "failed")])
def test_a_failure_is_said_aloud_with_a_pre_made_line(rig, tmp_path, reply, line):
    pv = rig.voice(FakeClient(tmp_path, replies=[reply]))
    pv.conversation(source(tmp_path, [(0.3, 0), (1.0, 3000), (3.0, 0)]), "hotkey")
    assert rig.played() == ["start", "stop", line]
    assert rig.made == [pcvoice.LINES[line]]


def test_without_any_speech_a_failure_is_two_low_tones_never_silence(rig, tmp_path):
    rig.sounds.synth = lambda text, out: False
    pv = rig.voice(FakeClient(tmp_path, replies=[Reply("core_down", line="core_down")]))
    pv.conversation(source(tmp_path, [(0.3, 0), (1.0, 3000), (3.0, 0)]), "hotkey")
    assert rig.played() == ["start", "stop", "err1", "err2"]


def test_a_holding_line_ends_the_conversation_without_a_follow_up(rig, tmp_path):
    hold = Reply("working", wav=speaker.tone_wav(tmp_path / "hold.wav", ms=50))
    client = FakeClient(tmp_path, replies=[hold])
    pv = rig.voice(client)
    pv.conversation(source(tmp_path, [(0.3, 0), (1.0, 3000), (1.4, 0), (1.0, 3000), (3.0, 0)]), "hotkey")
    assert rig.played() == ["start", "stop", "hold"] and len(client.audio) == 1


def test_the_hotkey_while_waggle_speaks_cuts_it_off_and_listens(rig, tmp_path):
    client = FakeClient(tmp_path, reply_ms=3000)
    pv = rig.voice(client)
    src = source(tmp_path, [(0.3, 0), (1.0, 3000), (1.4, 0), (0.3, 0), (1.0, 3000), (1.4, 0), (7.0, 0)])
    client.replies = [Reply("done"), Reply("done", wav=speaker.tone_wav(tmp_path / "short.wav", ms=50))]
    timer = threading.Timer(0.4, pv.hotkey_pressed)
    t0 = time.monotonic()
    timer.start()
    pv.conversation(src, "hotkey")
    assert rig.played() == ["start", "stop", "reply1", "start", "stop", "short"]
    assert rig.backend.stops >= 1 and time.monotonic() - t0 < 3.0  # the 3 s reply did not play out
    assert len(client.audio) == 2


def test_the_reply_file_is_deleted_after_it_played(rig, tmp_path):
    client = FakeClient(tmp_path)
    pv = rig.voice(client)
    pv.conversation(source(tmp_path, [(0.3, 0), (1.0, 3000), (3.0, 0)]), "hotkey")
    rig.spk.wait_idle(5)
    time.sleep(0.05)
    assert not (tmp_path / "reply1.wav").exists()


# ---- the wake word ------------------------------------------------------------------------------------------

def wake_run(rig, pv, until, timeout=10):
    th = threading.Thread(target=pv.run, daemon=True)
    th.start()
    deadline = time.time() + timeout
    while not until() and time.time() < deadline:
        time.sleep(0.02)
    while pv.in_conversation and time.time() < deadline:
        time.sleep(0.02)
    pv.stopping.set()
    th.join(5)
    return not th.is_alive()


def test_the_wake_word_with_a_request_sends_only_the_request(rig, tmp_path):
    client = FakeClient(tmp_path, transcripts=["Hey Waggle, what's the time?"])
    src = source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (7.0, 0)])
    pv = rig.voice(client, wake=True, source=src)
    assert wake_run(rig, pv, lambda: client.texts)
    assert client.texts == ["what's the time?"] and client.audio == []
    assert rig.played()[0] == "reply1"
    assert pv.stats["wake_triggers"] == 1 and pv.stats["transcribed"] == 1


def test_only_the_name_gets_sir_and_listens(rig, tmp_path):
    client = FakeClient(tmp_path, transcripts=["Waggle."])
    src = source(tmp_path, [(0.5, 0), (0.6, 3000), (1.0, 0), (0.5, 0), (1.2, 3000), (1.4, 0), (7.0, 0)])
    pv = rig.voice(client, wake=True, source=src)
    assert wake_run(rig, pv, lambda: client.audio)
    assert rig.played()[:3] == ["sir", "stop", "reply1"] and len(client.audio) == 1


def test_speech_without_the_name_is_dropped_and_nothing_is_sent_or_kept(rig, tmp_path):
    client = FakeClient(tmp_path, transcripts=["so I was telling him about the game"])
    src = source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (1.0, 0)])
    pv = rig.voice(client, wake=True, source=src)
    assert wake_run(rig, pv, lambda: client.transcribed)
    assert client.texts == [] and client.audio == [] and rig.played() == []
    assert pv.stats["utterances"] == 1 and pv.stats["wake_triggers"] == 0
    kept = [p for p in (pcvoice.home() / "pcvoice").rglob("*") if p.is_file()]
    assert kept and all(p.parent.name == "tones" for p in kept)  # only the listening tones: no audio, no words


def test_a_very_short_sound_is_not_even_transcribed(rig, tmp_path):
    client = FakeClient(tmp_path)
    src = source(tmp_path, [(0.5, 0), (0.15, 3000), (1.5, 0)])
    pv = rig.voice(client, wake=True, source=src)
    assert wake_run(rig, pv, lambda: pv.stats["utterances"])
    assert client.transcribed == 0


def test_a_dry_run_counts_a_match_and_sends_nothing(rig, tmp_path):
    client = FakeClient(tmp_path, transcripts=["Waggle open the door"])
    src = source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (1.0, 0)])
    pv = rig.voice(client, wake=True, source=src, dry_wake=True)
    assert wake_run(rig, pv, lambda: pv.stats["wake_triggers"])
    assert client.texts == [] and client.audio == [] and pv.trigger_texts == ["Waggle open the door"]


def test_the_hotkey_works_while_the_wake_word_listens(rig, tmp_path):
    client = FakeClient(tmp_path)
    src = source(tmp_path, [(0.5, 0), (0.5, 0), (1.0, 3000), (1.4, 0), (7.0, 0)])
    pv = rig.voice(client, wake=True, source=src)
    pv.hotkey_pressed()
    assert wake_run(rig, pv, lambda: client.audio)
    assert rig.played()[:3] == ["start", "stop", "reply1"] and client.transcribed == 0


# ---- Waggle's own messages ----------------------------------------------------------------------------------

def spool(text, created=None, name="a"):
    p = pcvoice.folder(pcvoice.SPOOL) / f"{name}.json"
    p.write_text(json.dumps({"id": 1, "text": text, "created": created or time.time()}), encoding="utf-8")
    return p


def test_spooled_messages_are_said_and_removed(rig, tmp_path):
    pv = rig.voice(FakeClient(tmp_path))
    a, b = spool("Time to stand up, sir.", name="1"), spool("Old news.", created=time.time() - 3600, name="2")
    assert pv.speak_spooled() == 1
    assert rig.made == ["Time to stand up, sir."] and rig.played() == ["msg"]
    assert not a.exists() and not b.exists() and pv.stats["spoken_messages"] == 1


def test_spoken_messages_can_be_switched_off(rig, tmp_path, settings):
    from waggle import config
    data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    data["pc_voice_speak_messages"] = False
    config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")
    pv = rig.voice(FakeClient(tmp_path))
    spool("Hello")
    assert pv.speak_spooled() == 0 and rig.made == []


# ---- the core client ----------------------------------------------------------------------------------------

class FakeCore(http.server.BaseHTTPRequestHandler):
    calls = []
    mode = "audio"

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeCore.calls.append({"path": self.path, "auth": self.headers.get("Authorization"),
                               "ctype": self.headers.get("Content-Type"), "body": body})
        if self.path.startswith("/voice/transcribe"):
            data, ctype, code = json.dumps({"text": "Waggle hi", "ok": True}).encode(), "application/json", 200
        elif FakeCore.mode == "audio":
            data, ctype, code = mic.wav_bytes(b"\x00\x00" * 1600), "audio/wav", 200
        elif FakeCore.mode == "json":
            data, ctype, code = json.dumps({"spoken": "x"}).encode(), "application/json", 200
        else:
            data, ctype, code = b'{"error": "x"}', "application/json", int(FakeCore.mode)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Waggle-Spoken", "It%20is%20six%2C%20sir.")
        self.send_header("X-Waggle-Status", "done")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def core(tmp_path):
    FakeCore.calls, FakeCore.mode = [], "audio"
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCore)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pcvoice.home().mkdir(parents=True, exist_ok=True)
    (pcvoice.home() / "voice.token").write_text("SECRET-TOKEN", encoding="ascii")
    yield pcvoice.CoreClient(port=srv.server_address[1], timeout=10, out_dir=tmp_path / "out")
    srv.shutdown()
    srv.server_close()


def test_the_client_posts_audio_as_wav_with_the_token_and_gets_a_wav(core, caplog):
    caplog.set_level(logging.DEBUG)
    r = core.voice_audio(b"\x01\x00" * 3200)
    call = FakeCore.calls[0]
    assert call["path"] == "/voice?format=wav&client=pc" and call["auth"] == "Bearer SECRET-TOKEN"
    assert call["ctype"] == "audio/wav" and call["body"][:4] == b"RIFF"
    assert r.status == "done" and r.wav.exists() and r.wav.read_bytes()[:4] == b"RIFF"
    assert r.spoken == "It is six, sir."
    assert "SECRET-TOKEN" not in caplog.text and "SECRET-TOKEN" not in repr(r)


def test_the_client_sends_wake_word_text_as_json(core):
    core.voice_text("what's the time")
    assert json.loads(FakeCore.calls[0]["body"]) == {"text": "what's the time"}
    assert FakeCore.calls[0]["ctype"] == "application/json"


@pytest.mark.parametrize("mode, status, line", [("503", "core_down", "core_down"), ("429", "busy", "busy"),
                                                ("401", "failed", "failed"), ("json", "done", "failed")])
def test_the_client_turns_refusals_into_a_line(core, mode, status, line):
    FakeCore.mode = mode
    r = core.voice_audio(b"\x00\x00" * 100)
    assert (r.status, r.line, r.wav) == (status, line, None)


def test_a_core_that_is_down_is_core_down(tmp_path):
    pcvoice.home().mkdir(parents=True, exist_ok=True)
    (pcvoice.home() / "voice.token").write_text("T", encoding="ascii")
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r = pcvoice.CoreClient(port=port, timeout=2).voice_audio(b"\x00\x00")
    assert r.status == "core_down" and r.line == "core_down"


def test_no_token_file_is_core_down_not_a_crash(tmp_path):
    r = pcvoice.CoreClient(port=1, timeout=1).voice_text("hi")
    assert r.status == "core_down" and "voice.token" in r.error


def test_transcribe_returns_the_words(core):
    assert core.transcribe(b"\x00\x00" * 100)["text"] == "Waggle hi"
    assert FakeCore.calls[0]["path"] == "/voice/transcribe"


# ---- the process ----------------------------------------------------------------------------------------------

def test_status_and_stop_without_a_running_process(capsys):
    assert pcvoice.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["running"] is False
    assert pcvoice.main(["stop"]) == 0 and "not running" in capsys.readouterr().out
    assert pcvoice.running() is False


def test_the_lock_says_whether_it_runs():
    from waggle.core import procs
    lock = procs.InstanceLock(pcvoice.lock_path())
    assert lock.acquire()
    try:
        assert pcvoice.running() is True
    finally:
        lock.release()
    assert pcvoice.running() is False
