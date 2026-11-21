"""PC voice, second round: who is being addressed (judge, echo, stop, re-listen after a question), speaking sentence by
sentence from POST /voice/stream, and a reminder coming back in the channel it was asked in. Fakes only: a WAV file
in place of the microphone, a fake player behind the real Speaker queue, fake core clients and servers, a fake speech
server, a fake `claude`, a fake judge model. Nothing opens a real microphone, plays a sound, or reaches Ollama,
Telegram or the live core."""
import http.client
import http.server
import json
import threading
import time
import urllib.parse

import pytest
from engine_fakes import FakeClaude
from test_pcvoice import FakeClient, rig, source, spool, wake_run  # noqa: F401  (rig is a fixture)
from test_voiceline import Line

from waggle import followups, mic, pcspeak, pcvoice, speaker, tick, voice_intent, voice_reply
from waggle.chat_session import ChatSession, SessionPool
from waggle.core import jobs, outbox, timers, voiceline
from waggle.pcvoice import Reply
from waggle.voice_intent import Decision

@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    """The judge never reaches a real Ollama from a test: not to warm the model, not to ask it."""
    def refuse(*a, **k):
        raise ConnectionRefusedError("no model in tests")
    monkeypatch.setattr(voice_intent, "warm", lambda *a, **k: False)
    monkeypatch.setattr(voice_intent, "ollama_ask", refuse)


HOTKEY_TALK = [(0.3, 0), (1.0, 3000), (1.4, 0)]  # an utterance, then silence long enough to end it
FOLLOW_UP = [(1.0, 2500), (1.4, 0)]
SILENCE = [(7.0, 0)]


class StreamClient(FakeClient):
    """A FakeClient whose answers come as sentences (the stream), and whose transcriptions are scripted."""

    def __init__(self, tmp_path, sentences=("Done, sir.", "The reminder is set."), status="done", spoken=None,
                 transcripts=None, after_first=None, transcribe_error=""):
        super().__init__(tmp_path, transcripts=transcripts)
        self.sentences, self.status, self.spoken_text, self.after_first = list(sentences), status, spoken, after_first
        self.stream_calls, self.transcribe_error, self.per_turn = [], transcribe_error, []

    def voice_stream(self, kind, payload, on_sentence):
        self.stream_calls.append(kind)
        (self.audio if kind == "audio" else self.texts).append(payload)
        sentences = self.per_turn.pop(0) if self.per_turn else self.sentences
        for i, s in enumerate(sentences):
            on_sentence(s)
            if i == 0 and self.after_first:
                self.after_first()
        spoken = self.spoken_text if self.spoken_text is not None else " ".join(sentences)
        return Reply(self.status, spoken=spoken, http=200, streamed=len(sentences))

    def transcribe(self, pcm):
        self.transcribed += 1
        if self.transcribe_error:
            return {"error": self.transcribe_error, "text": ""}
        return {"text": self.transcripts.pop(0) if self.transcripts else "", "ok": True}


def smart(rig, client, decide=None, **kw):  # noqa: F811
    return rig.voice(client, smart=True, **kw) if decide is None else _with(rig.voice(client, smart=True, **kw), decide)


def _with(pv, decide):
    pv.decide = decide
    return pv


def config(**values):
    from waggle import config as cfg
    data = json.loads(cfg.CONFIG_FILE.read_text(encoding="utf-8"))
    data.update(values)
    cfg.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")


class Decider:
    """A fake judge: a decision per transcript (default: send), and a record of what it was asked."""

    def __init__(self, table=None, default="send", source="model"):
        self.table, self.default, self.source, self.asked = dict(table or {}), default, source, []

    def __call__(self, text, asked_question=False):
        self.asked.append((text, asked_question))
        return Decision(self.table.get(text, self.default), self.source, "fake")


# ---- settings and the off switch ------------------------------------------------------------------------------

def test_the_new_settings_have_defaults_and_the_master_switch_turns_it_all_off(settings):
    assert pcvoice.setting("pc_voice_smart") is True and pcvoice.setting("pc_voice_judge") is True
    assert pcvoice.setting("pc_voice_stream") is True and pcvoice.setting("pc_voice_question_s") == 10.0
    assert pcvoice.setting("pc_voice_judge_timeout_s") == voice_intent.JUDGE_TIMEOUT_S
    assert pcvoice.setting("pc_voice_judge_min") == 0.5 and pcvoice.setting("pc_voice_judge_model") == ""
    assert pcvoice.PcVoice(spk=None, wake=False).smart is True
    config(pc_voice_smart=False)
    pv = pcvoice.PcVoice(spk=None, wake=False, client=StreamClient(None))
    assert pv.smart is False and not pv.judging() and not pv.streaming()


def test_each_half_can_be_switched_off_on_its_own(settings):
    config(pc_voice_judge=False)
    pv = pcvoice.PcVoice(spk=None, wake=False, client=StreamClient(None))
    assert pv.smart and not pv.judging() and pv.streaming()
    config(pc_voice_judge=True, pc_voice_stream=False)
    pv = pcvoice.PcVoice(spk=None, wake=False, client=StreamClient(None))
    assert pv.judging() and not pv.streaming()


def test_a_client_without_a_stream_is_never_streamed():
    assert not pcvoice.PcVoice(spk=None, wake=False, client=FakeClient(None), smart=True).streaming()


@pytest.mark.parametrize("text, yes", [("Shall I add a note?", True), ("Shall I add a note? ", True), ('"Really?"', True),
                                       ("Done, sir.", False), ("", False), ("Is it? No.", False), ("Right?)", True)])
def test_a_question_is_a_reply_that_ends_in_one(text, yes):
    assert pcvoice.ends_in_question(text) is yes


# ---- speaking sentence by sentence -----------------------------------------------------------------------------

def test_the_first_sentence_plays_while_the_stream_is_still_open_and_sentences_keep_their_order(rig, tmp_path):
    seen = {}

    def after_first():
        deadline = time.time() + 3
        while "sent" not in rig.backend.played and time.time() < deadline:
            time.sleep(0.01)
        seen["mid"] = list(rig.backend.played)  # what had played when the stream was still going

    client = StreamClient(tmp_path, sentences=("Done, sir.", "The reminder is set.", "Anything else?"),
                          after_first=after_first)
    pv = smart(rig, client)
    pv.conversation(source(tmp_path, HOTKEY_TALK + SILENCE), "hotkey")
    assert seen["mid"] == ["start", "stop", "sent"]
    assert rig.made == ["Done, sir.", "The reminder is set.", "Anything else?"]
    assert rig.played() == ["start", "stop", "sent", "sent", "sent"]
    assert client.stream_calls == ["audio"] and client.audio and pv.stats["streamed_turns"] == 1


def test_a_streamed_reply_is_remembered_for_echo_rejection_and_files_are_removed(rig, tmp_path):
    client = StreamClient(tmp_path, sentences=("The reminder is set for ten minutes.", "Shall I add a note?"))
    pv = smart(rig, client)
    pv.conversation(source(tmp_path, HOTKEY_TALK + SILENCE), "hotkey")
    rig.spk.wait_idle(5)
    time.sleep(0.05)
    assert pv.spoken.last() == "Shall I add a note?"
    assert voice_intent.is_echo("the reminder is set for ten minutes", pv.spoken.recent())
    assert list((pcvoice.home() / "pcvoice" / "tmp").glob("sent-*.wav")) == []


def test_a_text_request_from_the_wake_word_is_streamed_too(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["Hey Waggle, what's the time?"])
    pv = smart(rig, client, wake=True, source=source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (7.0, 0)]))
    assert wake_run(rig, pv, lambda: client.texts)
    assert client.texts == ["what's the time?"] and client.stream_calls == ["text"] and client.audio == []
    assert rig.played()[:2] == ["sent", "sent"]


def test_a_core_without_the_stream_is_answered_through_the_old_path_once(rig, tmp_path):
    class Old(StreamClient):
        def voice_stream(self, kind, payload, on_sentence):
            self.stream_calls.append(kind)
            return Reply("stream_unavailable", http=401)
    client = Old(tmp_path)
    pv = smart(rig, client)
    pv.conversation(source(tmp_path, HOTKEY_TALK + SILENCE), "hotkey")
    assert client.stream_calls == ["audio"] and len(client.audio) == 1  # asked /voice once, with the same audio
    assert rig.played() == ["start", "stop", "reply1"]


def test_the_holding_line_streamed_ends_the_conversation_without_a_follow_up(rig, tmp_path):
    client = StreamClient(tmp_path, sentences=("Working on it, sir. I will message you when it is done.",),
                          status="working")
    pv = smart(rig, client, decide=Decider())
    pv.conversation(source(tmp_path, HOTKEY_TALK + FOLLOW_UP + SILENCE), "hotkey")
    assert rig.played() == ["start", "stop", "sent"] and len(client.stream_calls) == 1


def test_when_nothing_can_be_made_into_sound_a_failure_line_or_two_tones_never_silence(rig, tmp_path):
    rig.sounds.synth = lambda text, out: False
    client = StreamClient(tmp_path)
    pv = smart(rig, client)
    pv.conversation(source(tmp_path, HOTKEY_TALK + SILENCE), "hotkey")
    assert rig.played() == ["start", "stop", "err1", "err2"] and len(client.stream_calls) == 1


def test_a_failure_before_any_sentence_is_said_with_a_line(rig, tmp_path):
    class Down(StreamClient):
        def voice_stream(self, kind, payload, on_sentence):
            return Reply("core_down", error="refused", line="core_down")
    pv = smart(rig, Down(tmp_path))
    pv.conversation(source(tmp_path, HOTKEY_TALK + SILENCE), "hotkey")
    assert rig.played() == ["start", "stop", "core_down"]


def test_the_hotkey_mid_stream_cuts_the_speech_and_drops_the_sentences_not_yet_made(rig, tmp_path):
    client = StreamClient(tmp_path, sentences=("One.", "Two.", "Three."))
    pv = smart(rig, client, decide=Decider())
    client.after_first = pv.hotkey_pressed
    t0 = time.monotonic()
    pv.conversation(source(tmp_path, HOTKEY_TALK + SILENCE), "hotkey")
    assert rig.made in ([], ["One."]) and time.monotonic() - t0 < 6
    played = rig.played()
    assert played.count("sent") <= 1 and played.count("start") == 2  # cut off, then it listened again (a start tone)


# ---- who is being addressed: the follow-up window --------------------------------------------------------------

def follow_up_source(tmp_path, utterances=1):
    return source(tmp_path, HOTKEY_TALK + (FOLLOW_UP * utterances) + SILENCE)


def test_a_follow_up_the_judge_accepts_is_sent_as_audio_and_the_hotkey_utterance_is_never_judged(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["what about tomorrow"])
    judge = Decider()
    pv = smart(rig, client, decide=judge)
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    assert judge.asked == [("what about tomorrow", False)]  # one question to the judge: the follow-up only
    assert client.stream_calls == ["audio", "audio"] and client.texts == []  # accepted: the core hears the audio itself
    assert len(client.audio) == 2 and client.transcribed == 1
    assert rig.played() == ["start", "stop", "sent", "sent", "stop", "sent", "sent"]


def test_background_talk_in_the_window_is_ignored_and_nothing_is_sent(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["honey did you take the trash out"])
    pv = smart(rig, client, decide=Decider(default="ignore"))
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    assert len(client.audio) == 1 and pv.stats["ignored"] == 1 and pv.stats["turns"] == 1
    assert rig.played() == ["start", "stop", "sent", "sent"]  # no tone for what was ignored


def test_listening_goes_on_after_something_ignored_so_the_owner_can_still_speak_in_the_window(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["and the winner is", "set another one for seven"])
    judge = Decider({"and the winner is": "ignore"})
    pv = smart(rig, client, decide=judge)
    pv.conversation(follow_up_source(tmp_path, utterances=2), "hotkey")
    assert [t for t, _ in judge.asked] == ["and the winner is", "set another one for seven"]
    assert len(client.audio) == 2 and pv.stats["ignored"] == 1  # the hotkey utterance and the second follow-up


def test_an_echo_is_dropped_and_counted(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["the reminder is set"])
    pv = smart(rig, client, decide=Decider({"the reminder is set": "echo"}))
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    assert client.texts == [] and pv.stats["echoes"] == 1 and pv.stats["ignored"] == 0


def test_stop_cuts_the_speech_ends_the_conversation_and_sends_nothing(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["never mind"])
    pv = smart(rig, client, decide=Decider({"never mind": "stop"}))
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    assert client.texts == [] and len(client.audio) == 1 and pv.stats["stops"] == 1
    assert rig.played()[-1] == "stop" and not pv.in_conversation and pv.asked is False


def test_a_judge_that_did_not_answer_sends_the_words_as_before(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["what about tomorrow"])
    pv = smart(rig, client, decide=Decider(source="fallback"))
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    assert len(client.audio) == 2 and pv.stats["judge_fallbacks"] == 1


def test_nothing_recognised_is_skipped_without_asking_the_judge(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=[""])
    judge = Decider()
    pv = smart(rig, client, decide=judge)
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    assert judge.asked == [] and client.texts == []


def test_when_the_words_cannot_be_read_the_audio_is_sent_as_before(rig, tmp_path):
    client = StreamClient(tmp_path, transcribe_error="HTTP 500")
    judge = Decider()
    pv = smart(rig, client, decide=judge)
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    assert judge.asked == [] and len(client.audio) == 2 and client.texts == [] and pv.stats["errors"] == 1


def test_the_default_judge_uses_the_spoken_log_for_echo_and_the_local_model_for_the_rest(rig, tmp_path, monkeypatch):
    asked = []

    def fake_model(messages, model, timeout, **kw):
        asked.append((messages[1]["content"], model))
        return {"directed": False, "stop": False, "confidence": 0.9}
    monkeypatch.setattr(voice_intent, "ollama_ask", fake_model)
    config(pc_voice_judge_model="qwen-test")
    client = StreamClient(tmp_path, sentences=("The reminder is set for ten minutes from now, sir.",),
                          transcripts=["the reminder is set for ten minutes from now sir", "honey dinner is ready"])
    pv = smart(rig, client)
    pv.conversation(follow_up_source(tmp_path, utterances=2), "hotkey")
    assert client.texts == [] and pv.stats["echoes"] == 1 and pv.stats["ignored"] == 1  # echo by rule, TV by model
    assert len(asked) == 1 and asked[0][1] == "qwen-test" and "honey dinner is ready" in asked[0][0]
    assert "The reminder is set for ten minutes from now, sir." in asked[0][0]  # the judge is told what Waggle said


def test_with_the_master_switch_off_a_follow_up_is_sent_as_audio_unjudged_exactly_as_before(rig, tmp_path):
    client = StreamClient(tmp_path)
    pv = rig.voice(client)  # smart=False: the rig default
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    assert client.transcribed == 0 and len(client.audio) == 2 and client.texts == [] and client.stream_calls == []
    assert pv.stats["judged"] == 0


def test_with_only_the_judge_off_the_stream_still_runs_and_follow_ups_are_not_judged(rig, tmp_path):
    config(pc_voice_judge=False)
    client = StreamClient(tmp_path)
    pv = rig.voice(client, smart=True)
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    assert client.transcribed == 0 and client.stream_calls == ["audio", "audio"]


# ---- the window after a question -------------------------------------------------------------------------------

def windows(rig, tmp_path, *, question, smart_on=True, **cfg):
    config(pc_voice_followup_s=2.0, pc_voice_question_s=7.0, **cfg)
    client = StreamClient(tmp_path, spoken="Shall I add a note?" if question else "Done, sir.")
    pv = rig.voice(client, smart=smart_on)
    waits = []
    orig = pv.listen
    pv.listen = lambda src, t, **kw: (waits.append(t), orig(src, t, **kw))[1]
    pv.conversation(source(tmp_path, HOTKEY_TALK + [(9.0, 0)]), "hotkey")
    return waits


def test_after_a_question_the_window_is_longer(rig, tmp_path):
    waits = windows(rig, tmp_path, question=True)
    assert len(waits) == 2 and 6.0 < waits[1] <= 7.0


def test_after_a_statement_the_window_is_the_usual_one(rig, tmp_path):
    waits = windows(rig, tmp_path, question=False)
    assert 1.0 < waits[1] <= 2.0


def test_the_longer_window_goes_with_the_master_switch(rig, tmp_path):
    waits = windows(rig, tmp_path, question=True, smart_on=False)
    assert waits[1] <= 2.0


def test_what_comes_after_sir_is_judged_knowing_waggle_asked(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["Waggle.", "what's the time"])
    judge = Decider()
    src = source(tmp_path, [(0.5, 0), (0.6, 3000), (1.0, 0), (0.5, 0), (1.2, 3000), (1.4, 0), (7.0, 0)])
    pv = smart(rig, client, decide=judge, wake=True, source=src)
    assert wake_run(rig, pv, lambda: client.audio)
    assert judge.asked == [("what's the time", True)] and len(client.audio) == 1
    assert pv.spoken.recent()[0] == pcvoice.LINES["sir"]  # "Sir?" is remembered as said


# ---- the wake word: echo and stop ------------------------------------------------------------------------------

def test_the_wake_name_inside_waggles_own_voice_coming_back_is_not_a_request(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["Waggle is running and all systems are fine sir"])
    pv = smart(rig, client, wake=True, source=source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (1.0, 0)]))
    pv.spoken.add("Waggle is running and all systems are fine, sir.")
    assert wake_run(rig, pv, lambda: client.transcribed)
    assert client.texts == [] and client.audio == [] and pv.stats["echoes"] == 1 and pv.stats["wake_triggers"] == 0


def test_waggle_stop_sends_nothing_and_cuts_the_speech(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["Waggle, stop"])
    pv = smart(rig, client, wake=True, source=source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (1.0, 0)]))
    assert wake_run(rig, pv, lambda: client.transcribed)
    assert client.texts == [] and pv.stats["stops"] == 1 and pv.stats["wake_triggers"] == 0
    assert rig.played() == []


def test_waggle_stop_the_timer_is_still_a_request(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["Waggle, stop the timer"])
    pv = smart(rig, client, wake=True, source=source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (7.0, 0)]))
    assert wake_run(rig, pv, lambda: client.texts)
    assert client.texts == ["stop the timer"]


def test_with_the_master_switch_off_the_wake_word_path_is_as_before(rig, tmp_path):
    client = FakeClient(tmp_path, transcripts=["Waggle, stop"])
    pv = rig.voice(client, wake=True, source=source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (7.0, 0)]))
    assert wake_run(rig, pv, lambda: client.texts)
    assert client.texts == ["stop"]


def test_spooled_messages_and_pre_made_lines_are_remembered_as_said(rig, tmp_path):
    pv = smart(rig, FakeClient(tmp_path))
    spool("Time to stand up, sir.")
    assert pv.speak_spooled() == 1 and pv.spoken.last() == "Time to stand up, sir."
    pv.say_line("busy")
    assert pv.spoken.last() == pcvoice.LINES["busy"]


# ---- the client of the stream ----------------------------------------------------------------------------------

class StreamCore(http.server.BaseHTTPRequestHandler):
    calls = []
    mode = "ok"
    pause = 0.0

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        StreamCore.calls.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                 "ctype": self.headers.get("Content-Type"), "body": body})
        if StreamCore.mode.isdigit():
            data = b'{"error": "x"}'
            self.send_response(int(StreamCore.mode))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()
        lines = [{"sentence": "Done, sir."}, "garbage", {"sentence": "  "}, {"sentence": "It is six."}]
        for obj in lines:
            self.wfile.write((obj if isinstance(obj, str) else json.dumps(obj)).encode() + b"\n")
            self.wfile.flush()
            time.sleep(StreamCore.pause)
        if StreamCore.mode == "ok":
            self.wfile.write(json.dumps({"done": True, "status": "done", "spoken": "Done, sir. It is six.",
                                         "text": "x", "turn": "t1"}).encode() + b"\n")


@pytest.fixture
def stream_core(tmp_path):
    StreamCore.calls, StreamCore.mode, StreamCore.pause = [], "ok", 0.0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StreamCore)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pcvoice.home().mkdir(parents=True, exist_ok=True)
    (pcvoice.home() / "voice.token").write_text("SECRET-TOKEN", encoding="ascii")
    yield pcvoice.CoreClient(port=srv.server_address[1], timeout=10, out_dir=tmp_path / "out")
    srv.shutdown()
    srv.server_close()


def test_the_stream_client_posts_to_the_stream_path_with_the_token_and_hands_out_each_sentence(stream_core):
    got = []
    reply = stream_core.voice_stream("audio", b"\x00\x00" * 1600, got.append)
    assert got == ["Done, sir.", "It is six."]  # garbage lines and empty sentences are skipped
    assert (reply.status, reply.spoken, reply.http, reply.streamed) == ("done", "Done, sir. It is six.", 200, 2)
    call = StreamCore.calls[0]
    assert call["path"] == "/voice/stream?client=pc" and call["auth"] == "Bearer SECRET-TOKEN"
    assert call["ctype"] == "audio/wav" and call["body"][:4] == b"RIFF"


def test_the_stream_client_sends_text_as_json(stream_core):
    stream_core.voice_stream("text", "what's the time", lambda s: None)
    assert json.loads(StreamCore.calls[0]["body"]) == {"text": "what's the time"}
    assert StreamCore.calls[0]["ctype"] == "application/json"


def test_each_sentence_arrives_while_the_server_is_still_talking(stream_core):
    StreamCore.pause = 0.25
    marks = []
    t0 = time.monotonic()
    stream_core.voice_stream("text", "hi", lambda s: marks.append((s, time.monotonic() - t0)))
    assert marks[0][0] == "Done, sir." and marks[0][1] < 0.2 and marks[1][1] > 0.6  # not all at the end


@pytest.mark.parametrize("mode, status, line", [("429", "busy", "busy"), ("503", "core_down", "core_down"),
                                                ("500", "failed", "failed")])
def test_the_stream_client_turns_refusals_into_lines(stream_core, mode, status, line):
    StreamCore.mode = mode
    r = stream_core.voice_stream("text", "hi", lambda s: None)
    assert (r.status, r.line) == (status, line)


@pytest.mark.parametrize("mode", ["401", "404", "405"])
def test_an_older_core_is_reported_as_unavailable_so_the_old_path_is_used(stream_core, mode):
    StreamCore.mode = mode
    assert stream_core.voice_stream("text", "hi", lambda s: None).status == "stream_unavailable"


def test_a_stream_that_ends_early_after_sentences_is_a_failure_that_needs_no_line(stream_core):
    StreamCore.mode = "drop"
    got = []
    r = stream_core.voice_stream("text", "hi", got.append)
    assert got == ["Done, sir.", "It is six."] and r.status == "failed" and r.streamed == 2 and r.line == ""


def test_a_core_that_is_down_is_core_down_and_no_token_is_too(tmp_path):
    pcvoice.home().mkdir(parents=True, exist_ok=True)
    (pcvoice.home() / "voice.token").write_text("T", encoding="ascii")
    assert pcvoice.CoreClient(port=1, timeout=2, out_dir=tmp_path).voice_stream("text", "x", print).status == "core_down"
    (pcvoice.home() / "voice.token").unlink()
    assert pcvoice.CoreClient(port=1, timeout=2, out_dir=tmp_path).voice_stream("text", "x", print).line == "core_down"


# ---- the core's stream endpoint (the real engine, a fake claude, a fake speech server) ------------------------------

LONG = "SPOKEN: Done, sir. The reminder is set. Shall I add a note?\nANSWER:\n" + "The written answer goes on. " * 12


class StreamLine(Line):
    """The core's health server with a fake claude that writes its answer slowly, a piece at a time."""
    pieces, delay = 14, 0.12

    def _script(self, proc, text):
        self.prompts.append(text)
        if "SLOWTURN" in text:
            time.sleep(self.slow_s)
        proc.answer(self.reply, pieces=self.pieces, delay=self.delay)

    def stream(self, body, *, query="?client=pc", token="own", content_type="application/json", read=None):
        """(status, [(seconds since the request, parsed line)]) of one POST /voice/stream."""
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        h = {"Content-Type": content_type}
        if token == "own":
            h["Authorization"] = f"Bearer {self.token}"
        elif token:
            h["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        out, t0 = [], time.monotonic()
        try:
            conn.request("POST", "/voice/stream" + query, body=data, headers=h)
            r = conn.getresponse()
            if r.status != 200:
                return r.status, json.loads(r.read() or b"{}")
            while True:
                raw = r.readline()
                if not raw:
                    break
                out.append((time.monotonic() - t0, json.loads(raw)))
                if read and len(out) >= read:
                    break
            return r.status, out
        finally:
            conn.close()


@pytest.fixture
def sline(settings, monkeypatch):
    ln = StreamLine(settings, monkeypatch)
    ln.reply = LONG
    yield ln
    ln.close()


def only_sentences(lines):
    return [o["sentence"] for _, o in lines if "sentence" in o]


def test_the_first_sentence_arrives_long_before_the_turn_ends(sline):
    code, lines = sline.stream({"text": "remind me to call Sam"})
    assert code == 200
    assert only_sentences(lines) == ["Done, sir.", "The reminder is set.", "Shall I add a note?"]
    done = [(t, o) for t, o in lines if o.get("done")]
    assert len(done) == 1 and lines[-1][1].get("done")
    first_at, done_at = lines[0][0], done[0][0]
    assert first_at < done_at - 0.5  # said while the model was still writing the answer
    final = done[0][1]
    assert final["status"] == "done" and final["spoken"] == "Done, sir. The reminder is set. Shall I add a note?"
    assert "The written answer goes on." in final["text"] and final["turn"]
    assert sline.synth_calls == []  # the core makes no sound: the PC makes it, sentence by sentence


def test_the_stream_keeps_the_spoken_cap_and_queues_a_silent_telegram_copy(sline):
    sline.reply = "SPOKEN: One. Two. Three. Four. Five.\nANSWER:\nThe full text."
    code, lines = sline.stream({"text": "count"})
    assert only_sentences(lines) == ["One.", "Two.", "Three."]
    deadline = time.time() + 10
    rows = []
    while not rows and time.time() < deadline:
        rows = [r for r in outbox.list(limit=100) if r["kind"] == "text"]
        time.sleep(0.05)
    assert rows and rows[0]["meta"]["via"] == "pc voice" and rows[0]["meta"]["silent"] is True
    assert "The full text." in rows[0]["body"]


def test_a_reply_without_a_spoken_part_is_streamed_at_the_end_from_the_answer(sline):
    sline.reply = "The build passed. The deploy is done. Nothing else is pending. And a fourth."
    code, lines = sline.stream({"text": "status"})
    assert only_sentences(lines) == ["The build passed.", "The deploy is done.", "Nothing else is pending."]
    assert lines[-1][1]["status"] == "done"


def test_audio_in_works_on_the_stream_path_as_it_does_on_voice(sline, monkeypatch, tmp_path):
    from waggle import stt
    monkeypatch.setattr(stt, "transcribe", lambda path, **k: {"ok": True, "text": "what is the time", "seconds": 0.1,
                                                              "audio_s": 1.0, "confidence": {"confident": True}})
    sline.reply = "SPOKEN: It is six, sir.\nANSWER:\nIt is six."
    code, lines = sline.stream(mic.wav_bytes(b"\x00\x00" * 8000), content_type="audio/wav")
    assert code == 200 and only_sentences(lines) == ["It is six, sir."]


def test_the_stream_needs_the_voice_token_and_a_body(sline):
    assert sline.stream({"text": "hi"}, token=None)[0] == 401
    assert sline.stream({"text": "hi"}, token="wrong")[0] == 401
    assert sline.stream({"text": ""})[0] == 400 and sline.stream({"nope": 1})[0] == 400
    code, body = sline.stream(b"x", content_type="text/plain")
    assert code == 415 and sline.prompts == []


def test_a_second_request_while_a_stream_runs_is_429_and_the_lock_is_released_afterwards(sline):
    sline.delay = 0.25
    first = {}
    t = threading.Thread(target=lambda: first.update(r=sline.stream({"text": "first"})))
    t.start()
    deadline = time.time() + 20
    while not sline.prompts and time.time() < deadline:
        time.sleep(0.02)
    code, body = sline.stream({"text": "and another"})
    assert code == 429 and body["error"]
    assert sline.post({"text": "and another"})[0] == 429  # the buffered path shares the lock
    t.join(60)
    assert first["r"][0] == 200
    sline.delay = 0.0
    assert sline.stream({"text": "later"})[0] == 200  # released when the first stream ended


def test_a_pc_that_hangs_up_mid_stream_releases_the_lock_and_the_turn_still_finishes(sline):
    code, lines = sline.stream({"text": "remind me"}, read=1)  # reads one line and goes away
    assert code == 200 and lines
    deadline = time.time() + 30
    while time.time() < deadline:
        if voiceline._busy.acquire(blocking=False):
            voiceline._busy.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail("the voice lock was never released")


def test_a_turn_still_silent_after_the_hold_time_ends_with_the_holding_line_and_the_answer_is_spoken_later(sline, monkeypatch):
    monkeypatch.setenv("WAGGLE_VOICE_HOLD_S", "0.5")
    sline.slow_s, sline.delay = 2.0, 0.0
    code, lines = sline.stream({"text": "SLOWTURN please"})
    assert only_sentences(lines) == [voiceline.HOLD_LINE]
    assert lines[-1][1]["status"] == "working" and lines[-1][1]["spoken"] == voiceline.HOLD_LINE
    deadline = time.time() + 15
    rows = []
    while not rows and time.time() < deadline:
        rows = [r for r in outbox.list(limit=100) if r["kind"] == "text"]
        time.sleep(0.05)
    assert rows and rows[0]["meta"].get("speak_pc") is True and not rows[0]["meta"].get("silent")


def test_a_turn_that_was_already_talking_at_the_hold_time_leaves_a_silent_copy(sline, monkeypatch):
    monkeypatch.setenv("WAGGLE_VOICE_HOLD_S", "0.6")
    sline.pieces, sline.delay = 40, 0.1  # the spoken part is out early, the long answer keeps going for ~4 s
    sline.reply = "SPOKEN: Done, sir. All set.\nANSWER:\n" + "More text here. " * 40
    code, lines = sline.stream({"text": "go"})
    assert only_sentences(lines)[:2] == ["Done, sir.", "All set."]
    assert lines[-1][1]["status"] == "done"
    deadline = time.time() + 20
    rows = []
    while not rows and time.time() < deadline:
        rows = [r for r in outbox.list(limit=100) if r["kind"] == "text"]
        time.sleep(0.05)
    assert rows and rows[0]["meta"].get("silent") is True and not rows[0]["meta"].get("speak_pc")


# ---- a reminder comes back in the channel it was asked in -------------------------------------------------------

def at(h, m=0, day=19):
    import datetime as dt
    return dt.datetime(2026, 11, day, h, m).timestamp()


def reminder_rows():
    return sorted(outbox.list(limit=100), key=lambda r: r["id"])


def fire(now):
    timers.fire_due(now)
    while jobs.run_one(kinds=["outreach.reminder"], now=now) is not None:
        pass


@pytest.fixture
def voice_synth(monkeypatch):
    from waggle import notify
    said = []

    def synth(text, out, settings=None):
        said.append(text)
        out.write_bytes(b"OggS")
        return True
    monkeypatch.setattr(notify, "_synth", synth)
    return said


def test_a_reminder_asked_at_the_pc_by_voice_is_marked_voice(monkeypatch):
    from waggle import guard
    tick.register_jobs()
    # the running turn is a PC voice turn: origin user, voice, words dictated at the PC
    monkeypatch.setattr(guard, "current_turn", lambda: {"origin": "user", "voice": True, "chat": "telegram:4242"})
    fid = followups.add(at(7, day=20), "stand up", chat="telegram:4242", now=at(12))
    assert timers.get(f"reminder:{fid}")["payload"].get("voice") is True
    monkeypatch.setattr(guard, "current_turn", lambda: {"origin": "user", "voice": False})
    fid2 = followups.add(at(7, day=20), "typed one", now=at(12))
    assert "voice" not in timers.get(f"reminder:{fid2}")["payload"]


def test_owner_away_a_pc_voice_reminder_is_a_telegram_voice_note_then_text(voice_synth):
    tick.register_jobs()
    fid = followups.add(at(7, day=20), "stand up", now=at(12), voice=True)
    fire(at(7, day=20))
    rows = reminder_rows()
    assert [r["kind"] for r in rows] == ["voice", "text"] and voice_synth == ["Sir, your reminder: stand up."]
    away = dict(present=False, running=True, quiet=False)
    assert [pcspeak.consider(r, **away) for r in rows] == rows  # nothing silenced, nothing spoken at the PC
    assert followups.get(fid)["status"] == "sent"


def test_owner_at_the_pc_the_reminder_is_spoken_there_once_and_the_telegram_rows_are_silent(voice_synth):
    tick.register_jobs()
    followups.add(at(7, day=20), "stand up", now=at(12), voice=True)
    fire(at(7, day=20))
    rows = reminder_rows()
    here = dict(present=True, running=True, quiet=False)
    out = [pcspeak.consider(r, **here) for r in rows]
    assert [o["meta"]["silent"] for o in out] == [True, True]
    spooled = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(pcvoice.folder(pcvoice.SPOOL).glob("*.json"))]
    assert [s["text"] for s in spooled] == ["Sir, your reminder: stand up."]  # once, with the framing


def test_a_typed_reminder_is_spoken_at_the_pc_with_the_same_framing_and_has_no_voice_note(voice_synth):
    tick.register_jobs()
    followups.add(at(7, day=20), "stand up", now=at(12), voice=False)
    fire(at(7, day=20))
    rows = reminder_rows()
    assert [r["kind"] for r in rows] == ["text"] and voice_synth == []
    pcspeak.consider(rows[0], present=True, running=True, quiet=False)
    spooled = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(pcvoice.folder(pcvoice.SPOOL).glob("*.json"))]
    assert [s["text"] for s in spooled] == ["Sir, your reminder: stand up."]


def test_a_late_reminder_says_so_in_the_spoken_sentence(voice_synth):
    tick.register_jobs()
    followups.add(at(7, day=20), "stand up", now=at(12), voice=True)
    timers.fire_due(at(9, day=20))  # the PC was off at 7
    while jobs.run_one(kinds=["outreach.reminder"], now=at(9, day=20)) is not None:
        pass
    text_row = [r for r in reminder_rows() if r["kind"] == "text"][0]
    assert text_row["meta"]["spoken"] == "Sir, your reminder, which is late: stand up."


def test_the_spoken_sentence_never_replaces_the_rows_own_keys():
    from waggle import notify
    row = notify.deliver("text", "hello", origin="reminder", ref="k1", asked=True,
                         meta={"spoken": "Hi.", "asked": False, "urgent": True})
    meta = outbox.get(row["id"])["meta"]
    assert meta["spoken"] == "Hi." and meta["asked"] is True and meta["urgent"] is False
