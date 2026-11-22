"""Hearing a voice note well: the cleaned transcript the model reads (both versions kept in the record), the
recogniser's own confidence flag, and the "Heard:" line at the top of the written reply."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from engine_fakes import FakeBotApi, FakeClaude

from waggle import chat_engine, stt, traces, voice_reply
from waggle.chat_session import ChatSession
from waggle.delivery import BotApiDelivery

REPLY = "SPOKEN: Done, sir.\nANSWER:\nThe reminder is set for ten minutes from now."


@pytest.fixture(autouse=True)
def _engine(monkeypatch):
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")

    def synth(text, out, voice, speed, timeout):
        Path(out).write_bytes(b"RIFFwav")
        return 3.0

    def convert(wav, ogg, timeout):
        Path(ogg).write_bytes(b"OggS" + b"\0" * 20)
    monkeypatch.setattr(voice_reply, "synth_wav", synth)
    monkeypatch.setattr(voice_reply, "to_ogg", convert)
    yield
    chat_engine.set_delivery_factory("telegram", None)


def fake_transcribe(monkeypatch, text, confidence=None):
    conf = confidence or {"confident": True, "avg_logprob": -0.21, "no_speech_prob": 0.02,
                          "compression_ratio": 1.3, "segments": 1, "dropped": 0, "why": []}
    monkeypatch.setattr(chat_engine.stt, "transcribe", lambda path, **kw: {
        "ok": True, "text": text, "error": "", "seconds": 1.0, "load_s": 0.0, "audio_s": 2.0,
        "source": "fake whisper", "confidence": conf})


def voice_turn_of(settings, spoken_text, reply=REPLY, kind="voice", typed=None):
    settings.edit_interval_s = 0.05
    seen = {}

    def script(proc, text):
        seen["prompt"] = text
        proc.answer(reply)
    api = FakeBotApi()
    sess = ChatSession("telegram:42", settings=settings, popen=FakeClaude(script), claude="claude")
    if kind == "voice":
        rec = chat_engine.handle_inbound("telegram:42", "", kind="voice", audio="C:/cache/a.ogg", settings=settings,
                                         session=sess, delivery=BotApiDelivery("42", api))
    elif kind == "marker":  # a transcript still wrapped in Hermes's media marker
        rec = chat_engine.handle_inbound("telegram:42", spoken_text, kind="voice", settings=settings, session=sess,
                                         delivery=BotApiDelivery("42", api))
    else:
        rec = chat_engine.handle_inbound("telegram:42", typed, kind="text", settings=settings, session=sess,
                                         delivery=BotApiDelivery("42", api))
    return rec, api, seen


def trace_voice(rec):
    return json.loads(traces.get(rec.trace_id)["extra"])["voice_turn"]


def sent_texts(api):
    return [c[3] for c in api.calls if c[0] == "send_message"]


# ---- H1: the model reads the cleaned words, the record keeps both --------------------------------------------

def test_the_model_reads_the_cleaned_transcript_and_the_record_keeps_both(settings, monkeypatch):
    raw = "Um, can you, could you remind me to to call Sam in, uh, ten minutes?"
    fake_transcribe(monkeypatch, raw)
    rec, api, seen = voice_turn_of(settings, raw)
    assert seen["prompt"].rstrip().endswith("Could you remind me to call Sam in, ten minutes?")
    assert "Um," not in seen["prompt"] and "to to" not in seen["prompt"]
    v = trace_voice(rec)
    assert v["transcript_raw"] == raw
    assert v["transcript_cleaned"] == "Could you remind me to call Sam in, ten minutes?"
    assert traces.get(rec.trace_id)["request"] == v["transcript_cleaned"]  # the turn's text is what the model read


def test_a_clean_transcript_is_passed_through_unchanged(settings, monkeypatch):
    fake_transcribe(monkeypatch, "What is on my calendar tomorrow?")
    rec, api, seen = voice_turn_of(settings, "")
    v = trace_voice(rec)
    assert v["transcript_raw"] == v["transcript_cleaned"] == "What is on my calendar tomorrow?"


def test_known_mishearings_are_fixed_before_the_cleaning(settings, monkeypatch):
    fake_transcribe(monkeypatch, "Uh, open on shape for the ethics robotics team")
    rec, api, seen = voice_turn_of(settings, "")
    v = trace_voice(rec)
    assert v["transcript_cleaned"] == "Open Onshape for the FTC robotics team"
    assert v["transcript_raw"] == "Uh, open on shape for the ethics robotics team"


def test_a_gateway_transcript_is_cleaned_too_and_keeps_both(settings):
    text = '"Um, set a timer for, uh, five minutes"\n[User sent audio: C:\\cache\\audio_3.ogg]'
    rec, api, seen = voice_turn_of(settings, text, kind="marker")
    assert seen["prompt"].rstrip().endswith("Set a timer for, five minutes")
    v = trace_voice(rec)
    assert v["transcript_raw"] == "Um, set a timer for, uh, five minutes"
    assert v["transcript_cleaned"] == "Set a timer for, five minutes"
    assert v["audio_path"] == "C:\\cache\\audio_3.ogg" and v["transcript_source"] == "hermes gateway"


# ---- H2: was it heard well? -----------------------------------------------------------------------------------

def seg(text="hello", logp=-0.2, nosp=0.01, ratio=1.2):
    return SimpleNamespace(text=text, avg_logprob=logp, no_speech_prob=nosp, compression_ratio=ratio)


def test_the_bar_is_the_recognisers_own_defaults():
    import inspect

    from faster_whisper.transcribe import WhisperModel
    p = inspect.signature(WhisperModel.transcribe).parameters
    assert stt.CONF_AVG_LOGPROB == p["log_prob_threshold"].default
    assert stt.CONF_NO_SPEECH == p["no_speech_threshold"].default
    assert stt.CONF_COMPRESSION == p["compression_ratio_threshold"].default


def test_clear_speech_is_confident_with_the_numbers_kept():
    c = stt.confidence([seg(logp=-0.25, nosp=0.02, ratio=1.4), seg(logp=-0.4, nosp=0.05, ratio=1.1)])
    assert c["confident"] is True and c["why"] == [] and c["segments"] == 2
    assert c["avg_logprob"] == -0.4 and c["no_speech_prob"] == 0.05 and c["compression_ratio"] == 1.4


@pytest.mark.parametrize("weak,part", [(seg(logp=-1.2), "average log probability"),
                                       (seg(nosp=0.75), "no-speech probability"),
                                       (seg(ratio=2.9), "compression ratio")])
def test_one_weak_number_makes_the_note_not_confident(weak, part):
    c = stt.confidence([seg(), weak])
    assert c["confident"] is False and any(part in w for w in c["why"])


def test_the_worst_segment_decides_even_when_the_rest_is_fine():
    assert stt.confidence([seg()] * 5 + [seg(logp=-1.5)])["confident"] is False


def test_exactly_on_the_library_default_still_passes():
    assert stt.confidence([seg(logp=-1.0, nosp=0.6, ratio=2.4)])["confident"] is True
    assert stt.confidence([seg(logp=-1.01)])["confident"] is False
    assert stt.confidence([seg(nosp=0.61)])["confident"] is False
    assert stt.confidence([seg(ratio=2.41)])["confident"] is False


def test_no_segments_is_never_confident():
    c = stt.confidence([])
    assert c["confident"] is False and c["segments"] == 0 and c["avg_logprob"] is None


class FakeModel:
    def __init__(self, segments):
        self.segments = segments

    def transcribe(self, samples, **kw):
        return iter(self.segments), SimpleNamespace(duration=2.0)


def transcribed(monkeypatch, tmp_path, segments):
    f = tmp_path / "note.ogg"
    f.write_bytes(b"OggS")
    monkeypatch.setattr(stt, "decode", lambda path, timeout=60: [0.0] * 32000)
    return stt.transcribe(f, loader=lambda name: (FakeModel(segments), 0.0))


def test_transcribe_reports_the_confidence_of_the_kept_segments(monkeypatch, tmp_path):
    res = transcribed(monkeypatch, tmp_path, [seg("Remind me to call Sam.", logp=-0.3), seg("Tomorrow.", logp=-0.5)])
    assert res["ok"] and res["text"] == "Remind me to call Sam. Tomorrow."
    assert res["confidence"]["confident"] is True and res["confidence"]["avg_logprob"] == -0.5


def test_a_segment_the_silence_gate_drops_does_not_count_against_the_note(monkeypatch, tmp_path):
    ghost = seg("Thanks for watching!", logp=-1.6, nosp=0.9)  # invented over silence: dropped by the gate
    res = transcribed(monkeypatch, tmp_path, [seg("Send the report."), ghost])
    assert res["text"] == "Send the report." and res["confidence"]["confident"] is True
    assert res["confidence"]["dropped"] == 1


def test_a_weak_recognition_is_not_confident(monkeypatch, tmp_path):
    res = transcribed(monkeypatch, tmp_path, [seg("Delete the old backups.", logp=-1.3, nosp=0.1)])
    assert res["ok"] and res["confidence"]["confident"] is False


def test_the_voice_info_carries_stt_confident_and_the_numbers(settings, monkeypatch):
    fake_transcribe(monkeypatch, "Remind me to call Sam.")
    rec, api, seen = voice_turn_of(settings, "")
    v = trace_voice(rec)
    assert v["stt_confident"] is True and v["stt_avg_logprob"] == -0.21 and v["stt_no_speech_prob"] == 0.02
    assert v["stt_compression_ratio"] == 1.3 and rec.voice["stt_confident"] is True


def test_a_weak_note_is_flagged_in_the_voice_info(settings, monkeypatch):
    fake_transcribe(monkeypatch, "Delete the old backups.", {
        "confident": False, "avg_logprob": -1.4, "no_speech_prob": 0.1, "compression_ratio": 1.1, "segments": 1,
        "dropped": 0, "why": ["average log probability -1.40 is below -1.0"]})
    rec, api, seen = voice_turn_of(settings, "")
    v = trace_voice(rec)
    assert v["stt_confident"] is False and v["stt_confident_why"] and v["stt_avg_logprob"] == -1.4


def test_a_gateway_transcript_has_no_numbers_so_it_is_not_confident(settings):
    text = '"Delete the old backups"\n[User sent audio: C:\\cache\\audio_4.ogg]'
    rec, api, seen = voice_turn_of(settings, text, kind="marker")
    assert trace_voice(rec)["stt_confident"] is False


# ---- H3: the written reply starts with what was heard ---------------------------------------------------------

def written(rec, api):
    return api.messages[rec.message_ids[0]]


def test_the_written_reply_to_a_voice_turn_starts_with_the_heard_line(settings, monkeypatch):
    fake_transcribe(monkeypatch, "Um, remind me to call Sam in ten minutes.")
    rec, api, seen = voice_turn_of(settings, "")
    assert written(rec, api) == ('Heard: "Remind me to call Sam in ten minutes."\n\n'
                                 "The reminder is set for ten minutes from now.")


def test_the_heard_line_is_never_spoken(settings, monkeypatch):
    said = []
    monkeypatch.setattr(voice_reply, "synth_wav", lambda text, out, v, sp, t: said.append(text) or Path(out).write_bytes(b"RIFF") or 3.0)
    fake_transcribe(monkeypatch, "Remind me to call Sam.")
    rec, api, seen = voice_turn_of(settings, "")
    assert said == ["Done, sir."] and "Heard" not in said[0]
    assert "Heard" not in seen["prompt"]  # nor does the model see it


def test_a_typed_turn_has_no_heard_line(settings):
    rec, api, seen = voice_turn_of(settings, "", reply="It is three o'clock.", kind="text", typed="read me the headline")  # not a fast-path question
    assert written(rec, api) == "It is three o'clock." and "heard_line" not in (rec.voice or {})


def test_a_long_transcript_is_cut_to_two_hundred_characters_with_an_ellipsis():
    line = chat_engine.heard_line("word " * 100)
    quoted = line[len('Heard: "'):-1]
    assert len(quoted) == 200 and quoted.endswith("\u2026") and not quoted[:-1].endswith(" ")
    assert chat_engine.heard_line("short one") == 'Heard: "short one"'
    assert chat_engine.heard_line("x" * 200) == 'Heard: "' + "x" * 200 + '"'  # exactly 200 is not cut
    assert chat_engine.heard_line("   ") == ""


def test_the_record_keeps_the_answer_and_reads_claims_from_it_alone(settings, monkeypatch):
    fake_transcribe(monkeypatch, "Open notes.txt and tell me what is in it.")
    rec, api, seen = voice_turn_of(settings, "", reply="SPOKEN: It is empty, sir.\nANSWER:\nThe file is empty.")
    row = traces.get(rec.trace_id)
    assert row["reply"] == "The file is empty." and rec.reply == "The file is empty."
    assert rec.claims is None  # the user's file name in the Heard line is not something Waggle claims to have made
    assert written(rec, api).startswith('Heard: "Open notes.txt')


def test_an_undelivered_voice_reply_goes_to_the_outbox_with_its_heard_line(settings, monkeypatch):
    from waggle.core import outbox
    fake_transcribe(monkeypatch, "Remind me to call Sam.")

    class Down(BotApiDelivery):
        def post(self, text):
            return None
    settings.edit_interval_s = 0.05
    sess = ChatSession("telegram:42", settings=settings, popen=FakeClaude(lambda proc, t: proc.answer(REPLY)),
                       claude="claude")
    rec = chat_engine.handle_inbound("telegram:42", "", kind="voice", audio="C:/cache/a.ogg", settings=settings,
                                     session=sess, delivery=Down("42", FakeBotApi()))
    assert rec.outbox_id is not None
    body = outbox.get(rec.outbox_id)["body"]
    assert body == 'Heard: "Remind me to call Sam."\n\nThe reminder is set for ten minutes from now.'


def test_a_direct_reply_to_a_voice_turn_also_starts_with_the_heard_line(settings, monkeypatch):
    fake_transcribe(monkeypatch, "Approve zzzz.")
    rec, api, seen = voice_turn_of(settings, "")
    text = written(rec, api)
    assert text.startswith('Heard: "Approve zzzz."\n\n') and "don't have an approval" in text


def test_a_gateway_transcript_gets_the_line_with_the_cleaned_words(settings):
    text = '"Um, set a timer for, uh, five minutes"\n[User sent audio: C:\\cache\\audio_5.ogg]'
    rec, api, seen = voice_turn_of(settings, text, kind="marker")
    assert written(rec, api).startswith('Heard: "Set a timer for, five minutes"\n\n')


def test_a_voice_note_that_could_not_be_transcribed_has_nothing_to_show(settings, monkeypatch):
    monkeypatch.setattr(chat_engine.stt, "transcribe", lambda path, **kw: {
        "ok": False, "text": "", "error": "no speech", "seconds": 0.5, "load_s": 0.0, "audio_s": 0.0,
        "source": "fake", "confidence": stt.confidence([])})
    rec, api, seen = voice_turn_of(settings, "")
    assert "Heard" not in written(rec, api) and "couldn't make out" in written(rec, api)
