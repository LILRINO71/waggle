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
