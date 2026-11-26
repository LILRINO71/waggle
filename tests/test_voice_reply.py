"""A voice note gets a short spoken answer plus the full text: the format, the caps, the order of the sends, every
failure branch, the record, and (when ffmpeg is present) a real conversion."""
import json
import math
import struct
import time
import wave
from pathlib import Path

import pytest
from engine_fakes import FakeBotApi, FakeClaude, FakeClock

from waggle import chat_engine, traces, voice_reply
from waggle.chat_session import ChatSession
from waggle.core import ledger
from waggle.delivery import BotApiDelivery, CollectDelivery

HEARD = "Heard: \"what's the weather like?\"\n\n"  # the first lines of a voice turn's written reply
REPLY = ("SPOKEN: It is sunny and warm today. Bring a light jacket for the evening.\n"
         "ANSWER:\nToday is sunny, 72 degrees, with a cool evening.\n\n- Jacket for tonight\n- Sunscreen")


# ---- the format -----------------------------------------------------------------------------------------

def test_split_reply_parses_the_fixed_format():
    spoken, answer, had = voice_reply.split_reply(REPLY)
    assert had and spoken == "It is sunny and warm today. Bring a light jacket for the evening."
    assert answer.startswith("Today is sunny, 72 degrees") and answer.endswith("- Sunscreen")
    assert "SPOKEN" not in answer and "ANSWER" not in answer


@pytest.mark.parametrize("text", ["**SPOKEN:** Hi there.\n**ANSWER:**\nThe long form.",
                                  "spoken: Hi there.\nanswer: The long form."])
def test_split_reply_tolerates_markdown_and_case(text):
    spoken, answer, had = voice_reply.split_reply(text)
    assert had and spoken == "Hi there." and answer == "The long form."


def test_split_reply_without_a_spoken_part_keeps_the_whole_text():
    assert voice_reply.split_reply("Just an answer. With two sentences.") == (
        "", "Just an answer. With two sentences.", False)


def test_split_reply_without_an_answer_marker_never_loses_the_text():
    assert voice_reply.split_reply("SPOKEN: Done.\n\nDetails follow here.") == ("Done.", "Details follow here.", True)
    assert voice_reply.split_reply("SPOKEN: Only this.") == ("Only this.", "Only this.", True)


def test_spoken_complete_only_after_the_answer_marker():
    assert not voice_reply.spoken_complete("SPOKEN: Half a sent")
    assert not voice_reply.spoken_complete("Plain text ANSWER: no")
    assert voice_reply.spoken_complete("SPOKEN: Done.\nANSWER:\nMore")


# ---- the caps ---------------------------------------------------------------------------------------------

def test_cap_spoken_keeps_three_sentences():
    out = voice_reply.cap_spoken("One. Two! Three? Four. Five.")
    assert out == "One. Two! Three?"


def test_cap_spoken_keeps_about_forty_five_words():
    sentence = " ".join(["word"] * 20) + "."
    out = voice_reply.cap_spoken(" ".join([sentence] * 3))
    assert len(out.split()) == 40 and out.count(".") == 2  # the third sentence would pass 45 words: dropped
    long_one = " ".join(["word"] * 80)
    out = voice_reply.cap_spoken(long_one)
    assert len(out.split()) == 45 and out.endswith(".")


def test_cap_spoken_drops_what_cannot_be_said():
    text = "See [the docs](https://example.com/x) at https://example.com now. ```py\nprint(1)\n``` `Code` is **bold** in C:\\a\\b.txt ok."
    out = voice_reply.cap_spoken(text)
    assert "http" not in out and "print" not in out and "`" not in out and "*" not in out and "C:\\" not in out
    assert "the docs" in out and "bold" in out


def test_fallback_spoken_is_the_first_sentences_of_the_answer():
    answer = "# Plan\n- First, open the report.\n- Second, check totals.\n\nThird sentence here. Fourth one."
    out = voice_reply.fallback_spoken(answer)
    assert out.count(".") <= 3 and out.startswith("Plan First, open the report.") and "#" not in out and "-" not in out[:2]
    assert voice_reply.fallback_spoken("```\nonly code\n```") == ""


# ---- one voice turn through the engine ---------------------------------------------------------------------

class SpyApi(FakeBotApi):
    """The Telegram fake plus a record of the voice file's bytes at the moment it was sent."""
    sent_voice = None

    def send_voice(self, chat_id, path, *, caption=None, duration=None):
        self._maybe_fail("send_voice")
        self.sent_voice = Path(path).read_bytes() if Path(path).exists() else None
        return super().send_voice(chat_id, path, caption=caption, duration=duration)


@pytest.fixture
def audio(monkeypatch):
    """Fake speech server and converter: they record what they were asked and write small files."""
    box = {"say": [], "convert": [], "fail_synth": None, "fail_convert": None, "synth_delay": 0.0, "secs": 6.5}

    def synth(text, out, voice, speed, timeout):
        box["say"].append((text, voice, speed))
        time.sleep(box["synth_delay"])
        if box["fail_synth"]:
            raise box["fail_synth"]
        Path(out).write_bytes(b"RIFFwav")
        return box["secs"]

    def convert(wav, ogg, timeout):
        box["convert"].append(Path(wav).name)
        if box["fail_convert"]:
            raise box["fail_convert"]
        Path(ogg).write_bytes(b"OggS" + b"\0" * 20)

    monkeypatch.setattr(voice_reply, "synth_wav", synth)
    monkeypatch.setattr(voice_reply, "to_ogg", convert)
    return box


def voice_turn_of(settings, reply=REPLY, voice=True, api=None, delivery=None, script=None):
    settings.edit_interval_s = 0.05
    clock = FakeClock()
    api = api or SpyApi(clock=clock)
    d = delivery or BotApiDelivery("42", api, clock=clock, sleep=clock.sleep)
    fake = FakeClaude(script or (lambda proc, text: proc.answer(reply, pieces=6)))
    sess = ChatSession("telegram:42", settings=settings, popen=fake, claude="claude")
    rec = chat_engine.run_turn("telegram:42", "what's the weather like?", d, context="", settings=settings,
                               session=sess, platform="telegram", voice=voice,
                               voice_info={"voice": True, "transcript_source": "fake", "stt_s": 1.2})
    return rec, api, fake


def sends(api):
    return [c[0] for c in api.calls if c[0] in ("send_voice", "send_message", "edit_message_text")]


def extra(rec):
    return json.loads(traces.get(rec.trace_id)["extra"])["voice_turn"]


def test_voice_message_first_then_the_full_text(settings, audio):
    rec, api, fake = voice_turn_of(settings)
    assert sends(api) == ["send_voice", "send_message"]
    voice = next(c for c in api.calls if c[0] == "send_voice")
    assert voice[5] in (6, 7)  # the duration the speech server reported, rounded
    assert api.sent_voice and api.sent_voice.startswith(b"OggS")
    text = api.messages[rec.message_ids[0]]
    assert text.startswith(HEARD + "Today is sunny") and "SPOKEN" not in text and "ANSWER" not in text
    assert "Heard" not in audio["say"][0][0]  # the Heard line is never spoken
    assert audio["say"][0][0] == "It is sunny and warm today. Bring a light jacket for the evening."
    assert audio["say"][0][1:] == (settings.voice_note_voice, settings.voice_note_speed)
    assert "<voice_reply_format>" in fake.procs[0].inputs[0]
    assert rec.outcome == "ok" and rec.delivered


def test_the_turn_record_says_everything_about_the_voice_turn(settings, audio):
    rec, api, _ = voice_turn_of(settings)
    v = extra(rec)
    assert v["voice_requested"] and v["voice_delivered"] and v["voice_message_id"] == api.next_id - 1  # the voice came first, then the text
    assert v["transcript_source"] == "fake" and v["stt_s"] == 1.2
    assert v["audio_s"] == 6.5 and v["synth_s"] >= 0 and v["convert_s"] >= 0 and v["model_s"] > 0
    assert v["spoken_from_model"] is True and v["spoken_words"] == 13 and v["voice_error"] == ""
    assert v["voice_sent_s"] > 0
    row = next(r for r in ledger.status() if r["name"] == "telegram_voice")
    assert row["ready"] and row["method"] == "bot api message id" and str(v["voice_message_id"]) in row["evidence_ref"]


def test_the_model_cannot_exceed_the_caps(settings, audio):
    long = " ".join(["Sentence number %d has several plain words in it." % i for i in range(1, 9)])
    reply = f"SPOKEN: {long}\nANSWER:\n{long}"
    rec, api, _ = voice_turn_of(settings, reply)
    said = audio["say"][0][0]
    assert said.count(".") <= 3 and len(said.split()) <= 45
    assert api.messages[rec.message_ids[0]].count("Sentence number") == 8  # the text keeps everything


def test_no_spoken_part_means_the_rule_builds_one_from_the_answer(settings, audio):
    rec, api, _ = voice_turn_of(settings, "The build passed. All 40 tests ran. Nothing failed. Deploy at five.")
    assert audio["say"][0][0] == "The build passed. All 40 tests ran. Nothing failed."
    assert sends(api) == ["send_voice", "send_message"] and extra(rec)["spoken_from_model"] is False
    assert api.messages[rec.message_ids[0]] == HEARD + "The build passed. All 40 tests ran. Nothing failed. Deploy at five."


@pytest.mark.parametrize("what,setup,why", [
    ("speech fails", lambda a: a.update(fail_synth=RuntimeError("server down")), "server down"),
    ("conversion fails", lambda a: a.update(fail_convert=RuntimeError("ffmpeg: no libopus")), "no libopus"),
])
def test_audio_failures_send_the_text_alone_and_degrade_the_turn(settings, audio, what, setup, why):
    setup(audio)
    rec, api, _ = voice_turn_of(settings)
    assert sends(api) == ["send_message"] and rec.delivered
    v = extra(rec)
    assert v["voice_delivered"] is False and why in v["voice_error"]
    assert rec.outcome == "degraded" and any("spoken reply not sent" in w and why in w for w in rec.outcome_why)
    row = next(r for r in ledger.status() if r["name"] == "telegram_voice")
    assert not row["ready"] and row["last_failure_at"] is not None
    assert not list(Path(settings.vault).parent.glob("**/voice_out/*.wav"))  # no temp audio left behind


def test_speech_that_takes_too_long_is_given_up_on(settings, audio, monkeypatch):
    monkeypatch.setattr(voice_reply, "BUDGET_S", 0.3)
    audio["synth_delay"] = 1.5
    t = time.time()
    rec, api, _ = voice_turn_of(settings)
    assert sends(api) == ["send_message"] and "longer than 0.3 s" in extra(rec)["voice_error"]
    assert rec.outcome == "degraded" and time.time() - t < 3


def test_a_voice_message_telegram_refuses_is_a_degraded_turn_with_the_text_sent(settings, audio):
    from engine_fakes import TelegramError
    api = SpyApi()
    api.fail["send_voice"] = [TelegramError("Bad Request: wrong file", "permanent")]
    rec, api, _ = voice_turn_of(settings, api=api)
    assert sends(api) == ["send_message"] and rec.delivered  # the refused voice message never landed; the text did
    assert extra(rec)["voice_delivered"] is False and "wrong file" in extra(rec)["voice_error"]
    assert rec.outcome == "degraded"


def test_audio_over_twenty_seconds_is_made_again_shorter(settings, audio, monkeypatch):
    seconds = iter([26.0, 14.0])

    def synth(text, out, voice, speed, timeout):
        audio["say"].append(text)
        Path(out).write_bytes(b"x")
        return next(seconds)
    monkeypatch.setattr(voice_reply, "synth_wav", synth)
    long = "SPOKEN: " + " ".join(["This sentence has exactly seven words."] * 3) + "\nANSWER:\nfull"
    rec, api, _ = voice_turn_of(settings, long)
    assert len(audio["say"]) == 2 and audio["say"][1].count(".") == 2 and extra(rec)["audio_s"] == 14.0
    assert extra(rec)["voice_delivered"]


def test_a_typed_message_never_gets_audio(settings, audio):
    rec, api, fake = voice_turn_of(settings, "Plain answer. Two sentences.", voice=False)
    assert sends(api) == ["send_message"] and audio["say"] == [] and "<voice_reply_format>" not in fake.procs[0].inputs[0]
    assert "voice_requested" not in (json.loads(traces.get(rec.trace_id)["extra"]).get("voice_turn") or {})


def test_a_chat_that_cannot_take_voice_keeps_the_old_behaviour(settings, audio):
    d = CollectDelivery()
    rec, _, fake = voice_turn_of(settings, "Short answer.", delivery=d)
    assert audio["say"] == [] and d.text == HEARD + "Short answer." and "<voice_reply_format>" not in fake.procs[0].inputs[0]


def test_a_failure_notice_is_text_only(settings, audio):
    class Dead:
        def turn(self, *a, **k):
            raise RuntimeError("down")
    settings.edit_interval_s = 0.05
    api = SpyApi()
    d = BotApiDelivery("42", api)
    rec = chat_engine.run_turn("telegram:42", "hello", d, context="", settings=settings, session=Dead(), voice=True,
                               platform="telegram")
    assert rec.failure_notice and sends(api) == ["send_message"] and audio["say"] == []


def test_speech_starts_while_the_model_is_still_writing_the_answer(settings, audio, monkeypatch):
    started = {}

    def synth(text, out, voice, speed, timeout):
        started["at"] = time.time()
        Path(out).write_bytes(b"x")
        return 3.0
    monkeypatch.setattr(voice_reply, "synth_wav", synth)

    def script(proc, msg):
        def go():
            from engine_fakes import ev_init, ev_result, ev_text
            proc.emit(ev_init(proc.session_id))
            for ev in ev_text(proc.session_id, "SPOKEN: Quick one.\nANSWER:\n" + "x" * 60, pieces=2):
                proc.emit(ev)
            time.sleep(0.6)  # the model is slow with the long part
            started["answer_done"] = time.time()
            proc.emit(ev_result(proc.session_id, ""))
        import threading
        threading.Thread(target=go, daemon=True).start()
    voice_turn_of(settings, script=script)
    assert started["at"] < started["answer_done"]


# ---- a voice turn is a real turn: the same tools as a typed message ---------------------------------------

def test_a_voice_note_asking_for_a_reminder_really_sets_it(settings, audio, monkeypatch):
    """The words come from the engine's own transcription (no Hermes wrapper in the prompt), the model uses the
    reminder tool exactly as it does for typed text, and the reply is voice + text."""
    from engine_fakes import ev_tool
    from waggle import waggle_mcp
    from waggle.core import timers
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")
    monkeypatch.setenv("WAGGLE_CHAT", "telegram:42")
    monkeypatch.setenv("WAGGLE_TURN_ORIGIN", "user")
    monkeypatch.setattr(chat_engine.stt, "transcribe", lambda path, **kw: {
        "ok": True, "text": "Remind me to stand up in 10 seconds.", "error": "", "seconds": 1.1, "load_s": 0.0,
        "audio_s": 2.0, "source": "fake whisper"})
    made = {}

    def script(proc, text):
        made["prompt"] = text
        out = waggle_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "remind_me", "arguments": {"text": "stand up", "in_minutes": 0.17}}})
        made["tool"] = out["result"]["content"][0]["text"]
        proc.emit(ev_tool(proc.session_id, "mcp__waggle__remind_me", {"text": "stand up", "in_minutes": 0.17},
                          made["tool"], tool_id="t1"))
        proc.answer("SPOKEN: Done, I will remind you to stand up in ten seconds.\nANSWER:\nReminder set: stand up, in 10 seconds.")
    settings.edit_interval_s = 0.05
    api = SpyApi()
    chat_engine.set_delivery_factory("telegram", lambda address: BotApiDelivery(address, api))
    sess = ChatSession("telegram:42", settings=settings, popen=FakeClaude(script), claude="claude")
    rec = chat_engine.handle_inbound("telegram:42", "", kind="voice", audio="C:/cache/audio_1.ogg", settings=settings,
                                     session=sess)
    chat_engine.set_delivery_factory("telegram", None)
    assert "Remind me to stand up in 10 seconds." in made["prompt"] and "audio_1.ogg" not in made["prompt"]
    assert "[User sent audio" not in made["prompt"]
    assert [s["tool"] for s in rec.steps] == ["mcp__waggle__remind_me"] and "reminder" in json.loads(made["tool"])
    assert timers.get(f"reminder:{json.loads(made['tool'])['reminder']}") is not None
    assert sends(api) == ["send_voice", "send_message"] and rec.outcome == "ok"
    v = extra(rec)
    assert v["audio_path"] == "C:/cache/audio_1.ogg" and v["transcript_source"] == "fake whisper" and v["voice_delivered"]


def test_a_transcript_with_hermes_media_marker_reaches_the_model_as_words_only(settings, audio, monkeypatch):
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")
    seen = {}

    def script(proc, text):
        seen["prompt"] = text
        proc.answer("SPOKEN: Sure.\nANSWER:\nSure thing.")
    settings.edit_interval_s = 0.05
    api = SpyApi()
    sess = ChatSession("telegram:42", settings=settings, popen=FakeClaude(script), claude="claude")
    text = '"Remind me to stand up in 10 seconds."\n[User sent audio: C:\\cache\\audio_9.ogg]'
    rec = chat_engine.handle_inbound("telegram:42", text, kind="voice", delivery=BotApiDelivery("42", api),
                                     settings=settings, session=sess)
    assert seen["prompt"].rstrip().endswith("Remind me to stand up in 10 seconds.") and "audio_9" not in seen["prompt"]
    v = extra(rec)
    assert v["audio_path"] == "C:\\cache\\audio_9.ogg" and v["transcript_source"] == "hermes gateway"


# ---- real ffmpeg (skipped when it is not installed) --------------------------------------------------------

@pytest.mark.skipif(voice_reply.find_ffmpeg() is None, reason="no ffmpeg under ~/.hermes/tools")
def test_real_ffmpeg_makes_an_ogg_opus_file(tmp_path):
    wav = tmp_path / "tone.wav"
    rate = 24000
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(int(rate * 1.5))))
    ogg = tmp_path / "tone.ogg"
    voice_reply.to_ogg(wav, ogg, timeout=30)
    assert ogg.read_bytes()[:4] == b"OggS" and b"OpusHead" in ogg.read_bytes()[:200]
    assert 1.3 <= voice_reply.ogg_seconds(ogg) <= 1.8


def test_a_missing_ffmpeg_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_reply, "find_ffmpeg", lambda: None)
    with pytest.raises(RuntimeError, match="ffmpeg was not found"):
        voice_reply.to_ogg(tmp_path / "a.wav", tmp_path / "a.ogg", 5)


# ---- speech-to-text ------------------------------------------------------------------------------------------

class _Seg:
    def __init__(self, text, no_speech=0.0, logprob=-0.2):
        self.text, self.no_speech_prob, self.avg_logprob = text, no_speech, logprob


class _Model:
    def __init__(self, segs, duration=2.5):
        self.segs, self.duration, self.calls = segs, duration, []

    def transcribe(self, audio, **kw):
        self.calls.append((audio, kw))
        return iter(self.segs), type("Info", (), {"duration": self.duration})()


def _note(tmp_path, name="n.ogg"):
    f = tmp_path / name
    f.write_bytes(b"OggS....")
    return f


def test_stt_joins_segments_drops_silence_hallucinations_and_uses_hermes_settings(tmp_path, monkeypatch):
    from waggle import stt
    model = _Model([_Seg(" Remind me"), _Seg("Thank you.", no_speech=0.9, logprob=-1.5), _Seg("to stand up. ")])
    monkeypatch.setattr(stt, "decode", lambda path, timeout=60: "samples")
    r = stt.transcribe(_note(tmp_path), loader=lambda name: (model, 0.25))
    assert r["ok"] and r["text"] == "Remind me to stand up." and r["load_s"] == 0.25 and r["audio_s"] == 2.5
    kw = model.calls[0][1]
    assert kw["beam_size"] == 5 and kw["vad_filter"] is True and kw["condition_on_previous_text"] is False
    assert kw["no_speech_threshold"] == 0.6 and kw["log_prob_threshold"] == -1.0


def test_stt_failures_are_results_not_exceptions(tmp_path, monkeypatch):
    from waggle import stt
    monkeypatch.setattr(stt, "decode", lambda path, timeout=60: "samples")
    assert "no speech" in stt.transcribe(_note(tmp_path), loader=lambda n: (_Model([]), 0))["error"]
    assert stt.transcribe(tmp_path / "missing.ogg")["error"] == "the audio file is missing"
    assert stt.transcribe(_note(tmp_path, "a.txt"))["error"] == "not an audio file"
    empty = tmp_path / "e.ogg"
    empty.write_bytes(b"")
    assert stt.transcribe(empty)["error"] == "the audio file is empty"

    def boom(name):
        raise OSError("model files missing")
    assert "model files missing" in stt.transcribe(_note(tmp_path), loader=boom)["error"]


@pytest.mark.skipif(voice_reply.find_ffmpeg() is None, reason="no ffmpeg under ~/.hermes/tools")
def test_stt_decodes_with_the_real_ffmpeg_to_sixteen_khz_samples(tmp_path):
    from waggle import stt
    wav = tmp_path / "t.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\0\0" * 24000)
    samples = stt.decode(wav)
    assert 15900 <= len(samples) <= 16100 and str(samples.dtype) == "float32"

def test_recogniser_is_given_the_users_project_names(tmp_path):
    from types import SimpleNamespace
    from waggle import stt
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Projects" / "Kalshi Claude Desk.md").write_text("x", encoding="utf-8")
    hint = stt.vocabulary(SimpleNamespace(vault=str(tmp_path)))
    assert "Waggle" in hint and "Kalshi Claude Desk" in hint and len(hint) <= 600
    assert stt.vocabulary(SimpleNamespace(vault=str(tmp_path / "missing"))).startswith("Names that may come up: Waggle")


# ---- the ack line is not said twice (2026-11-26: "On it, sir, starting now. On it, sir, ...") ------------------

def test_after_the_ack_line_a_repeat_of_it_is_not_said_again():
    from waggle.voice_reply import ACK_LINE, VoiceReply, after_ack
    assert after_ack("On it, sir. Starting now.") == "" and after_ack("On it, sir.") == ""
    assert after_ack("On it, sir: the coder agent is building it.") == "The coder agent is building it."
    assert after_ack("The coder is on it.") == "The coder is on it."
    said = []
    vr = VoiceReply(".", audio=False, on_sentence=said.append)
    assert vr.ack()
    vr.feed("SPOKEN: On it, sir. Starting now. The coder agent is building Land Planner as job 9. ")
    vr.finish("SPOKEN: On it, sir. Starting now. The coder agent is building Land Planner as job 9. I will post the "
              "link.\nANSWER:\nJob 9.")
    assert said == [ACK_LINE, "The coder agent is building Land Planner as job 9.", "I will post the link."]
    assert vr.spoken.startswith("The coder agent")


def test_without_the_ack_line_the_answer_is_said_as_written():
    from waggle.voice_reply import VoiceReply
    said = []
    vr = VoiceReply(".", audio=False, on_sentence=said.append)
    vr.finish("SPOKEN: On it, sir. The reminder is set.\nANSWER:\nSet.")
    assert said[0].startswith("On it, sir")
