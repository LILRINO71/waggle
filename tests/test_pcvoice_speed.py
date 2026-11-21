"""PC voice speed (2026-11-21): the judge that never answered, models loaded before the first turn, one timing line
per turn, a shorter notes search on voice turns, and the day's chat session started before the first message.
Fakes only: no microphone, speaker, Ollama, claude or live core."""
import json
import logging
import threading
import time

import pytest
from engine_fakes import FakeClaude
from test_pcvoice import FakeClient, rig, source, wake_run  # noqa: F401  (rig is a fixture)
from test_voice2 import HOTKEY_TALK, SILENCE, StreamClient, StreamLine, config, follow_up_source, smart  # noqa: F401
from test_voice2 import sline  # noqa: F401  (a fixture)

from waggle import chat_engine, chat_jobs, chat_session, chat_store, mic, pcvoice, voice_intent
from waggle.chat_session import ChatSession, SessionPool
from waggle.core import store, voiceline
from waggle.pcvoice import Reply


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    """Nothing here reaches a real Ollama; what was asked and warmed is recorded."""
    box = {"asked": [], "warmed": [], "fail": False}

    def ask(messages, model, timeout, **kw):
        box["asked"].append((model, timeout))
        if box["fail"]:
            raise TimeoutError("timed out")
        return {"directed": True, "stop": False, "confidence": 0.9}

    def warm(model, **kw):
        box["warmed"].append(model)
        return True
    monkeypatch.setattr(voice_intent, "ollama_ask", ask)
    monkeypatch.setattr(voice_intent, "warm", warm)
    return box


def until(cond, timeout=3.0):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        time.sleep(0.01)
    return cond()


# ---- the judge -------------------------------------------------------------------------------------------------

def test_the_judge_is_a_small_fast_model_with_a_short_wait_by_default(settings, no_real_model):
    pv = pcvoice.PcVoice(spk=None, wake=False, client=FakeClient(None), smart=True)
    d = pv._decide("what about tomorrow")
    assert d.source == "model" and d.action == "send"
    model, timeout = no_real_model["asked"][0]
    assert model == voice_intent.DEFAULT_JUDGE_MODEL == "gemma4:latest"  # not settings.local_model (22 GB)
    assert timeout <= 2.0 and pcvoice.setting("pc_voice_judge_timeout_s") <= 2.0


def test_a_configured_judge_model_still_wins(settings, no_real_model):
    config(pc_voice_judge_model="qwen-test")
    pcvoice.PcVoice(spk=None, wake=False, client=FakeClient(None), smart=True)._decide("hello there")
    assert no_real_model["asked"][0][0] == "qwen-test"


def test_after_a_failed_judgement_the_judge_is_not_waited_for_again(settings, no_real_model):
    no_real_model["fail"] = True
    pv = pcvoice.PcVoice(spk=None, wake=False, client=FakeClient(None), smart=True)
    first = pv._decide("what about tomorrow")
    assert first.action == "send" and first.source == "fallback" and "timed out" in first.reason
    second = pv._decide("and the day after")
    assert len(no_real_model["asked"]) == 1  # the second one never went to the model
    assert second.action == "send" and second.source == "fallback" and "skipped" in second.reason
    pv._judge_off.until = 0.0  # the minute is over
    no_real_model["fail"] = False
    assert pv._decide("and next week").source == "model" and len(no_real_model["asked"]) == 2
    assert not pv._judge_off.active()


def test_the_echo_and_stop_rules_still_answer_while_the_judge_is_skipped(settings, no_real_model):
    no_real_model["fail"] = True
    pv = pcvoice.PcVoice(spk=None, wake=False, client=FakeClient(None), smart=True)
    pv._decide("what about tomorrow")
    assert pv._decide("never mind").action == "stop"


def test_a_failed_judgement_loads_the_model_again_in_the_background(settings, no_real_model):
    no_real_model["fail"] = True
    pv = pcvoice.PcVoice(spk=None, wake=False, client=FakeClient(None), smart=True)
    pv._decide("what about tomorrow")
    assert until(lambda: no_real_model["warmed"]) and no_real_model["warmed"] == [voice_intent.DEFAULT_JUDGE_MODEL]


def test_the_judge_model_is_loaded_when_the_process_starts(settings, no_real_model):
    pv = pcvoice.PcVoice(spk=None, wake=False, client=FakeClient(None), smart=True)
    pv.warm_models()
    assert until(lambda: no_real_model["warmed"]) and no_real_model["warmed"] == [voice_intent.DEFAULT_JUDGE_MODEL]
    config(pc_voice_judge=False)
    off = pcvoice.PcVoice(spk=None, wake=False, client=FakeClient(None), smart=True)
    off.warm_models()
    time.sleep(0.1)
    assert len(no_real_model["warmed"]) == 1  # a judge that is switched off is not loaded


# ---- the recogniser in the core ---------------------------------------------------------------------------------

class WarmClient(StreamClient):
    def __init__(self, tmp_path, **kw):
        super().__init__(tmp_path, **kw)
        self.warms = 0

    def warm_stt(self):
        self.warms += 1
        return True


def test_the_recogniser_is_asked_for_once_per_idle_spell_not_every_time(settings):
    client = WarmClient(None)
    pv = pcvoice.PcVoice(spk=None, wake=False, client=client, smart=True)
    pv._warm_stt()
    pv._warm_stt()
    assert until(lambda: client.warms == 1)
    time.sleep(0.05)
    assert client.warms == 1
    pv._stt_used = time.monotonic() - pcvoice.STT_WARM_S - 1  # it has been idle long enough to be unloaded
    pv._warm_stt()
    assert until(lambda: client.warms == 2)


def test_the_hotkey_starts_the_recogniser_while_the_start_tone_plays(rig, tmp_path):
    client = WarmClient(tmp_path)
    pv = smart(rig, client)
    pv.conversation(source(tmp_path, [(2.0, 0)]), "hotkey")
    assert until(lambda: client.warms == 1)


def test_speech_in_the_room_starts_the_recogniser_before_the_utterance_ends(rig, tmp_path):
    client = WarmClient(tmp_path, transcripts=["just talking"])
    pv = smart(rig, client, wake=True, source=source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (7.0, 0)]))
    assert wake_run(rig, pv, lambda: client.transcribed)
    assert client.warms >= 1


def test_the_client_asks_the_core_to_load_its_recogniser_with_a_quarter_second_of_silence(tmp_path):
    client = pcvoice.CoreClient(port=1, timeout=2, out_dir=tmp_path)
    calls = []
    client._post = lambda path, body, ctype, timeout=None: (calls.append((path, body, ctype)), (200, {}, b"{}"))[1]
    assert client.warm_stt() is True
    path, body, ctype = calls[0]
    assert path == "/voice/transcribe" and ctype == "audio/wav"
    assert len(body) == 44 + 2 * (mic.RATE // 4) and set(body[44:]) == {0}
    client._post = lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError("down"))
    assert client.warm_stt() is False


# ---- the timing line ---------------------------------------------------------------------------------------------

def test_the_timing_line_names_each_stage_in_order_and_a_dash_for_what_was_not_measured():
    line = pcvoice.format_timing({"end": 1.21, "stt": 0.31, "judge": 0.2, "context": None, "first_words": 3.1,
                                  "first_audio": 3.95, "total": 5.4}, {"judge": "model", "peak": 5200})
    assert line == ("turn timing: end=1.21s stt=0.31s judge=0.20s context=- first_words=3.10s first_audio=3.95s "
                    "total=5.40s (judge=model peak=5200)")


class TimedClient(StreamClient):
    """The stream answers after a pause and reports what the core measured."""

    def voice_stream(self, kind, payload, on_sentence):
        time.sleep(0.15)
        r = super().voice_stream(kind, payload, on_sentence)
        r.timings = {"stt_s": 0.35, "context_s": 0.8, "model_first_s": 2.1}
        return r


def timing_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("turn timing:")]


def test_every_voice_turn_logs_one_timing_line_with_every_stage(rig, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="waggle.pcvoice")
    client = TimedClient(tmp_path, transcripts=["what about tomorrow"])
    pv = smart(rig, client)
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    rig.played()
    lines = timing_lines(caplog)
    assert len(lines) == 2  # the hotkey turn and the follow-up
    for line in lines:
        assert " end=1." in line  # the silence that ended the utterance (1.2 s, a little more by frame size)
        assert "first_words=0." in line and "first_audio=0." in line and "total=" in line
        assert "context=0.80s" in line and "core_stt=0.35" in line and "model_first=2.1" in line
    assert "stt=-" in lines[0] and "judge=-" in lines[0]  # the hotkey is never transcribed or judged here
    assert "stt=0." in lines[1] and "judge=0." in lines[1] and "judge=model" in lines[1]
    assert "what about tomorrow" not in "\n".join(r.getMessage() for r in caplog.records)  # never the words


def test_a_wake_word_turn_logs_its_timings_too(rig, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="waggle.pcvoice")
    client = TimedClient(tmp_path, transcripts=["Hey Waggle, what's the time?"])
    pv = smart(rig, client, wake=True, source=source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (7.0, 0)]))
    assert wake_run(rig, pv, lambda: client.texts)
    rig.played()
    assert until(lambda: timing_lines(caplog))
    line = timing_lines(caplog)[0]
    assert "stt=0." in line and "first_audio=0." in line and "end=0." in line  # the wake word waits 0.8 s


def test_the_fallback_says_why_in_the_log_and_never_logs_the_words(rig, tmp_path, caplog, no_real_model):
    caplog.set_level(logging.INFO, logger="waggle.pcvoice")
    no_real_model["fail"] = True
    client = StreamClient(tmp_path, transcripts=["what about tomorrow"])
    pv = smart(rig, client)  # the real default judge, failing
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "heard in a conversation window: send (fallback: TimeoutError: timed out)" in text
    assert "what about tomorrow" not in text


# ---- the core's side ---------------------------------------------------------------------------------------------

def test_the_streams_last_line_carries_what_the_core_measured(sline, monkeypatch):
    from waggle import stt
    monkeypatch.setattr(stt, "transcribe", lambda path, **k: {"ok": True, "text": "what is the time", "seconds": 0.12,
                                                              "audio_s": 1.0, "confidence": {"confident": True}})
    sline.reply = "SPOKEN: It is six, sir.\nANSWER:\nIt is six."
    code, lines = sline.stream(mic.wav_bytes(bytes(16000)), content_type="audio/wav")
    final = lines[-1][1]
    assert code == 200 and final["done"] and final["timings"]["stt_s"] == 0.12
    assert final["timings"]["context_s"] >= 0 and final["timings"]["model_first_s"] > 0


def test_the_client_reads_the_core_timings_and_ignores_anything_else():
    assert pcvoice.core_timings({"stt_s": 0.3, "context_s": 1, "x": "no", "y": True}) == {"stt_s": 0.3, "context_s": 1.0}
    assert pcvoice.core_timings("nope") == {} and pcvoice.core_timings(None) == {}


def test_a_voice_turn_gives_the_notes_search_less_time_than_a_typed_one(sline, monkeypatch):
    seen = []
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: seen.append(k.get("budget_s")) or "")
    sline.stream({"text": "what is the time"})
    assert seen == [chat_engine.VOICE_CONTEXT_BUDGET_S] and chat_engine.VOICE_CONTEXT_BUDGET_S < chat_engine.CONTEXT_BUDGET_S


def test_the_notes_search_that_is_slower_than_its_budget_is_left_out(settings, monkeypatch):
    from waggle import memory
    monkeypatch.setattr(memory, "retrieval_block", lambda *a, **k: time.sleep(1.5) or "a long note")
    t0 = time.time()
    ctx = chat_engine.build_context("telegram:1", "hello", True, settings, budget_s=0.2)
    assert time.time() - t0 < 1.2 and "memory" not in ctx.marker["sources"]


# ---- the day's first chat session --------------------------------------------------------------------------------

@pytest.fixture
def day(monkeypatch):
    store_day = {"v": None}
    real = chat_session.day_key
    monkeypatch.setattr(chat_session, "day_key", lambda now=None: store_day["v"] or real(now))
    yield store_day
    store.close_all()


def new_pool(settings, fake):
    return SessionPool(factory=lambda chat: ChatSession(chat, settings=settings, popen=fake, claude="claude",
                                                        transcript=lambda c: "SEED: yesterday we talked"),
                       settings=settings)


def test_a_new_day_starts_the_fresh_session_before_the_first_message(settings, day):
    fake = FakeClaude()
    pool = new_pool(settings, fake)
    pool.get("telegram:1").turn("good night")
    assert len(fake.procs) == 1 and pool.rollover(keep={"telegram:1"}) == []  # same day: nothing to do
    day["v"] = "2999-01-01"  # 4 am has passed
    assert pool.rollover(keep={"telegram:1"}) == ["telegram:1"]
    assert len(fake.procs) == 2 and "--session-id" in fake.procs[1].cmd and "--resume" not in fake.procs[1].cmd
    assert chat_session.session_day("telegram:1") == "2999-01-01"
    assert pool.rollover(keep={"telegram:1"}) == [] and len(fake.procs) == 2  # once a day
    res = pool.get("telegram:1").turn("good morning")  # the first message finds today's process ready
    assert res.ok and len(fake.procs) == 2 and not res.restarted
    assert "SEED: yesterday we talked" in fake.procs[1].inputs[0]


def test_the_new_days_seed_is_read_when_the_first_message_arrives(settings, day):
    fake, seeds = FakeClaude(), ["early", "early and what happened at seven"]
    pool = SessionPool(factory=lambda chat: ChatSession(chat, settings=settings, popen=fake, claude="claude",
                                                        transcript=lambda c: seeds.pop(0) if len(seeds) > 1 else seeds[0]),
                       settings=settings)
    pool.get("telegram:1").turn("good night")
    day["v"] = "2999-01-01"
    pool.rollover(keep={"telegram:1"})
    pool.get("telegram:1").turn("good morning")
    assert "early and what happened at seven" in fake.procs[1].inputs[0]


def test_the_rollover_leaves_a_chat_that_is_mid_turn_and_a_chat_that_is_not_the_owners(settings, day):
    fake = FakeClaude()
    pool = new_pool(settings, fake)
    busy, other = pool.get("telegram:1"), pool.get("telegram:2")
    busy.turn("hi")
    other.turn("hi")
    day["v"] = "2999-01-01"
    with busy.lock:
        assert pool.rollover(keep={"telegram:1", "telegram:2"}) == ["telegram:2"]  # the busy one is skipped
    assert pool.rollover(keep={"telegram:1"}) == ["telegram:1"]
    day["v"] = "3999-01-01"
    assert pool.rollover(keep=set()) == []  # only the owner's chats


def test_the_reap_timer_also_rolls_the_day_over(settings, day):
    settings.engine_chats = ["1"]
    settings.save()
    fake = FakeClaude()
    chat_engine.set_pool(new_pool(settings, fake))
    try:
        chat_engine.pool().get("telegram:1").turn("good night")
        day["v"] = "2999-01-01"
        got = chat_jobs.reap()
        assert got["rolled"] == ["telegram:1"] and got["stopped"] == [] and len(fake.procs) == 2
    finally:
        chat_engine.set_pool(None)


# ---- the owner's test 11:45: a wake word lost to the warm-up, and silence until the answer ------------------------

def test_a_busy_recogniser_is_waited_for_instead_of_dropping_what_was_said(tmp_path):
    client = pcvoice.CoreClient(port=1, timeout=2, out_dir=tmp_path)
    codes = [429, 429, 200]
    client._post = lambda *a, **k: (codes.pop(0), {}, b'{"text": "Waggle, what time is it?", "ok": true}')
    naps = []
    r = client.transcribe(bytes(3200), sleep=naps.append)
    assert r["text"] == "Waggle, what time is it?" and len(naps) == 2


def test_a_recogniser_busy_for_too_long_still_gives_up(tmp_path):
    client = pcvoice.CoreClient(port=1, timeout=2, out_dir=tmp_path)
    client._post = lambda *a, **k: (429, {}, b"{}")
    t = [0.0]

    def nap(s):
        t[0] += s
    r = client.transcribe(bytes(3200), busy_wait_s=1.0, sleep=nap, clock=lambda: t[0])
    assert r["error"] == "HTTP 429" and t[0] >= 1.0


def test_speech_without_the_wake_name_is_counted_in_the_log_but_never_its_words(rig, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="waggle.pcvoice")
    client = FakeClient(tmp_path, transcripts=["bagel what time is it"])
    pv = rig.voice(client, wake=True, source=source(tmp_path, [(0.5, 0), (1.5, 3000), (1.0, 0), (7.0, 0)]))
    assert wake_run(rig, pv, lambda: client.transcribed)
    text = chr(10).join(r.getMessage() for r in caplog.records)
    assert "heard speech, no wake name (1 so far)" in text
    assert "bagel" not in text and client.texts == []


def test_the_judge_asks_for_the_models_default_context_so_it_shares_mail_triages_loaded_copy(monkeypatch):
    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"message": {"content": '{"directed": true, "stop": false, "confidence": 0.9}'}}).encode()

    def fake_open(req, timeout=None):
        sent.update(json.loads(req.data))
        return Resp()
    monkeypatch.setattr(voice_intent.urllib.request, "urlopen", fake_open)
    voice_intent.ollama_ask([{"role": "user", "content": "x"}], "gemma4:latest", 1.0)
    assert "num_ctx" not in sent.get("options", {})  # a different num_ctx makes Ollama reload the model


# ---- the owner, 11:50: "I should just be able to respond without saying waggle" -----------------------------------

def test_after_an_answer_a_soft_tone_says_it_is_still_listening_and_no_wake_name_is_needed(rig, tmp_path):
    client = StreamClient(tmp_path, transcripts=["what about tomorrow"])
    pv = smart(rig, client)
    pv.conversation(follow_up_source(tmp_path), "hotkey")
    assert "open" in rig.played()
    assert len(client.stream_calls) == 2  # the follow-up went out without "Waggle"


def test_the_follow_up_windows_are_long_enough_to_think(settings):
    assert pcvoice.setting("pc_voice_followup_s") >= 12 and pcvoice.setting("pc_voice_question_s") >= 20


# ---- the owner, 11:55: "say you're starting the task", "robot-like pauses", leaked SPOKEN/ANSWER ----------------

def test_a_reply_that_writes_a_line_before_its_tools_still_splits_into_spoken_and_answer():
    from waggle import voice_reply
    t = "A blank doc is open. Next I'll set its title.\n\nSPOKEN: Done, sir. The doc is open.\n\nANSWER:\nIt is open in Chrome."
    assert voice_reply.split_reply(t) == ("Done, sir. The doc is open.", "It is open in Chrome.", True)
    assert voice_reply.spoken_sentences(t) == ["Done, sir.", "The doc is open."]


def test_the_first_tool_call_of_a_spoken_request_says_on_it_once_and_never_blocks_the_answer(tmp_path):
    from waggle import voice_reply
    heard = []
    vr = voice_reply.VoiceReply(tmp_path, synth=lambda *a: 1.0, convert=voice_reply.keep_wav, ext="wav",
                                on_sentence=heard.append)
    assert vr.ack() is True and vr.ack() is False
    vr.feed("SPOKEN: Done, sir. The doc is open.\nANSWER:\nOpen.")
    assert heard == [voice_reply.ACK_LINE, "Done, sir.", "The doc is open."]


def test_no_on_it_once_the_answer_has_started_or_without_a_live_speaker(tmp_path):
    from waggle import voice_reply
    heard = []
    vr = voice_reply.VoiceReply(tmp_path, synth=lambda *a: 1.0, convert=voice_reply.keep_wav, ext="wav",
                                on_sentence=heard.append)
    vr.feed("SPOKEN: It's noon. Anything else?\nANSWER:\nNoon.")
    assert vr.ack() is False and voice_reply.ACK_LINE not in heard
    assert voice_reply.VoiceReply(tmp_path, synth=lambda *a: 1.0).ack() is False  # a phone voice note: no stream


def test_sentences_that_arrive_together_are_spoken_as_one_piece_after_the_first(tmp_path):
    made = []
    gate = threading.Event()

    class Sounds:
        def make(self, text, out):
            made.append(text)
            gate.wait(2)
            return False

    sp = pcvoice.SentenceSpeaker(Sounds(), spk=None)
    sp.add("First one.")
    sp.add("Second one.")
    sp.add("Third one.")
    sp.close()
    gate.set()
    sp.finished(5)
    assert made[0] == "First one."
    assert "Second one. Third one." in made or made[1:] == ["Second one.", "Third one."]
