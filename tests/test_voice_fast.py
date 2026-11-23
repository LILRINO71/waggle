"""The PC voice's quick tiers (waggle/voice_fast.py, waggle/fastcalc.py): the time, the date, reminders, sums and
conversions answered by code; general knowledge by the small local model within a hard cap; everything else, and
every doubt, to Claude. The exchange is a normal turn and Claude is told about it on its next message."""
import datetime as dt
import json
import logging
import time

import pytest
from engine_fakes import FakeClaude

from waggle import chat_engine, fastcalc, traces, voice_fast
from waggle.chat_session import ChatSession
from waggle.core.voiceline import LineDelivery, _timings

NOW = dt.datetime(2026, 11, 21, 11, 46)
REMINDERS = [{"id": "a", "what": "call the dentist", "due": dt.datetime(2026, 11, 21, 17, 0).timestamp(), "chat": ""},
             {"id": "b", "what": "submit the form.", "due": dt.datetime(2026, 11, 22, 9, 0).timestamp(), "chat": ""}]


def code(text, rows=None):
    return voice_fast.code_answer(text, now=NOW, rows=REMINDERS if rows is None else rows)


# ---- tier 1: code ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["what time is it", "What time is it?", "Waggle, what time is it?", "what's the time",
                                  "whats the time right now", "what time it is", "can you tell me the time please",
                                  "hey waggle what is the current time", "time"])
def test_the_time_in_the_usual_phrasings(text):
    got = code(text)
    assert got and got.tier == "code" and got.kind == "time"
    assert got.spoken == "It is eleven forty-six in the morning, sir." and got.written == "It's 11:46 AM."


def test_spoken_times_read_naturally():
    t = dt.datetime
    assert voice_fast.time_words(t(2026, 1, 1, 16, 0)) == "four o'clock in the afternoon"
    assert voice_fast.time_words(t(2026, 1, 1, 9, 5)) == "nine oh five in the morning"
    assert voice_fast.time_words(t(2026, 1, 1, 12, 0)) == "noon"
    assert voice_fast.time_words(t(2026, 1, 1, 0, 0)) == "midnight"
    assert voice_fast.time_words(t(2026, 1, 1, 23, 30)) == "eleven thirty at night"
    assert voice_fast.time_words(t(2026, 1, 1, 12, 30)) == "twelve thirty in the afternoon"


@pytest.mark.parametrize("text,said", [
    ("what's the date", "Today is Saturday, the twenty-first of November, sir."),
    ("what day is it", "Today is Saturday, the twenty-first of November, sir."),
    ("what's today's date", "Today is Saturday, the twenty-first of November, sir."),
    ("what day of the week is it", "Today is Saturday, the twenty-first of November, sir."),
    ("what day is it tomorrow", "Tomorrow is Sunday, the twenty-second of November, sir."),
    ("what was the date yesterday", "Yesterday was Friday, the twentieth of November, sir."),
    ("what's the day after tomorrow", "The day after tomorrow is Monday, the twenty-third of November, sir."),
    ("what month is it", "It is November, sir."), ("what year is it", "It is 2026, sir.")])
def test_the_date_and_day(text, said):
    got = code(text)
    assert got and got.kind == "date" and got.spoken == said


@pytest.mark.parametrize("text", [
    "what time is the meet", "what time is it in Tokyo", "what day is Thanksgiving", "what's on my screen",
    "remind me at 5", "remind me to call mom", "how's my robot code", "do I have any plans for today",
    "open a Google Doc called Amazon", "what did I say about BIOBUZZ yesterday", "and tomorrow?", "set a reminder",
    "what's the date of the next meet", "cancel my reminders", "how much time is left"])
def test_anything_that_is_not_just_the_question_goes_on(text):
    assert code(text) is None


def test_reminders_are_read_from_the_store():
    got = code("what are my reminders")
    assert got.kind == "reminders" and got.spoken.startswith("You have two reminders, sir.")
    assert "call the dentist" in got.spoken and "Tomorrow at nine o'clock in the morning: submit the form." in got.spoken
    assert got.written.splitlines()[0] == "You have two reminders." and "- tomorrow, 9:00 AM: submit the form" in got.written
    assert code("do I have any reminders today").spoken.startswith("You have one reminder for today, sir.")
    assert code("what's my next reminder").spoken.startswith("Your next reminder, sir. Today at five o'clock")
    assert code("do I have any reminders", rows=[]).spoken == "You have no open reminders, sir."
    assert code("what reminders do I have tomorrow").spoken.startswith("You have one reminder for tomorrow")


def test_the_reminders_store_is_the_real_followups_store(settings):
    from waggle import followups
    followups.add(time.time() + 3600, "water the plants", chat="telegram:5", origin="user")
    got = voice_fast.code_answer("what are my reminders", chat="telegram:5")
    assert got and "water the plants" in got.spoken and got.spoken.startswith("You have one reminder, sir.")


@pytest.mark.parametrize("text,say", [
    ("what's 15 percent of 80", "15 percent of 80 is 12"), ("what is 15% of 80?", "15 percent of 80 is 12"),
    ("whats seventeen times twenty three", "17 times 23 is 391"), ("what's the square root of 144", "the square root of 144 is 12"),
    ("9 squared", "9 squared is 81"), ("how much is 100 divided by 8", "100 divided by 8 is 12.5"),
    ("what's two hundred and fifty plus six", "250 plus 6 is 256"), ("what is half of 90", "half of 90 is 45"),
    ("what's (3 + 4) * 2", "(3 plus 4) times 2 is 14"), ("2 to the power of 10", "2 to the power of 10 is 1,024")])
def test_sums(text, say):
    got = fastcalc.calc(text)
    assert got and got["say"] == say
    assert code(text).spoken == say[0].upper() + say[1:] + ", sir."


@pytest.mark.parametrize("text", ["what's 5 divided by 0", "what's 7", "hello", "who is 5 plus bob", "what's 555 1234",
                                  "twenty percent of 50 dollars", "what's 9 to the power of 9999"])
def test_a_sum_that_is_not_clean_is_not_answered(text):
    assert fastcalc.calc(text) is None


@pytest.mark.parametrize("text,say", [
    ("how many feet in a mile", "1 mile is 5,280 feet"), ("how many ounces in a pound", "1 pound is 16 ounces"),
    ("how many cups in a gallon", "1 gallon is 16 cups"), ("convert 72 fahrenheit to celsius", "72 degrees Fahrenheit is 22.2222 degrees Celsius"),
    ("what's 20 celsius in fahrenheit", "20 degrees Celsius is 68 degrees Fahrenheit"),
    ("how many seconds in a day", "1 day is 86,400 seconds"), ("how many minutes are in 3 hours", "3 hours is 180 minutes"),
    ("10 kilometers to miles", "10 kilometers is 6.2137 miles"), ("how many fluid ounces in a cup", "1 cup is 8 fluid ounces")])
def test_unit_conversions(text, say):
    got = fastcalc.convert(text)
    assert got and got["say"] == say


def test_units_of_different_kinds_are_never_guessed():
    for text in ("how many cups in a pound", "how many feet in a second"):
        assert fastcalc.convert(text) is None and fastcalc.mismatched_units(text)
        assert voice_fast.gate(text)[0] is False  # and the local model is not asked either
    assert fastcalc.convert("how many players are in a basketball team") is None


# ---- tier 2: the local model -------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["what is the capital of France", "who wrote Hamlet", "what does ephemeral mean",
                                  "how do you spell necessary", "why is the sky blue", "define serendipity"])
def test_the_gate_lets_a_plain_question_through(text):
    assert voice_fast.gate(text) == (True, "")


@pytest.mark.parametrize("text", [
    "what's on my screen", "what's my robot's intake speed", "who is the president", "what's the weather like",
    "what is the price of bitcoin", "open a google doc called amazon", "what did I say about BIOBUZZ yesterday",
    "how is the minecraft bot doing", "when is thanksgiving", "what's the latest news", "what time is the meet",
    "who won the game last night", "what's the best way to learn guitar", "and tomorrow", "what about that one",
    "what's 15 percent of 80 and then double it", "tell me a joke", "what is the population of Fontana",
    "who is the mayor of Fontana", "what is it called when", "what is a goBILDA channel", "what is Kokoro",
    "what is the derivative of x cubed", "who is the richest person alive", "what's the tallest tree"])
def test_the_gate_keeps_private_current_and_action_requests_away_from_the_model(text):
    assert voice_fast.gate(text)[0] is False


def say(answer, needs_tools=False, confident=True):
    return {"needs_tools": needs_tools, "answer": answer, "confident": confident}


def asker(first, verify=None, delay=0.0, log=None, again=None):
    """A fake ask(messages, model, timeout, schema, temperature): the answer, then the check; `again` is the second,
    warmer sample taken only when the answer holds a number."""
    verify = verify or {"well_known": True, "answers_question": True, "correct": True}

    def ask(messages, model, timeout, schema=None, temperature=0.0, **kw):
        if log is not None:
            log.append((model, timeout, schema is not None, temperature))
        time.sleep(delay)
        if schema is voice_fast.VERIFY_SCHEMA:
            return verify
        return again if (temperature and again is not None) else first
    return ask


def run(text, ask, settings, loaded=("gemma4:latest",), **kw):
    return voice_fast.route(text, settings, now=NOW, rows=[], ask=ask, loaded=lambda: list(loaded), **kw)


def test_a_confident_answer_is_said_in_waggles_manner(settings):
    calls = []
    r = run("what is the capital of France", asker(say("The capital of France is Paris."), log=calls), settings)
    assert r.tier == "local" and r.fast.kind == "knowledge" and r.fast.model == "gemma4:latest"
    assert r.fast.spoken == "The capital of France is Paris, sir." and r.fast.written == "The capital of France is Paris."
    assert [c[2] for c in calls] == [False, True]  # the answer, then the check


def test_a_number_must_come_out_the_same_twice(settings):
    calls = []
    q = "how many legs does a spider have"
    r = run(q, asker(say("A spider has eight legs."), log=calls, again=say("Spiders have 8 legs.")), settings)
    assert r.tier == "local" and [c[3] for c in calls] == [0.0, 0.0, 0.7]  # answer, check, a warmer second sample
    r = run(q, asker(say("A spider has eight legs."), again=say("Spiders have six legs.")), settings)
    assert r.tier == "claude" and r.why == "local: numbers disagree"
    r = run(q, asker(say("A spider has eight legs."), again=say("", confident=False)), settings)
    assert r.why == "local: numbers disagree"
    calls.clear()  # no number in the answer or the question: no second sample
    assert run("who wrote Hamlet", asker(say("Shakespeare wrote Hamlet."), log=calls), settings).tier == "local"
    assert [c[3] for c in calls] == [0.0, 0.0]


def test_a_figure_from_the_model_goes_to_claude_unless_it_is_a_count_that_never_changes(settings):
    r = run("what is the square footage of a football field", asker(say("About 150,000 square feet.")), settings)
    assert r.tier == "claude" and r.why == "local: numeric answer"
    r = run("what year did the Berlin wall fall", asker(say("The Berlin Wall fell in 1989.")), settings)
    assert r.tier == "claude" and r.why == "local: numeric answer"
    r = run("how many bones are in the human body", asker(say("There are 206 bones.")), settings)
    assert r.tier == "local"
    r = run("how many continents are there", asker(say("There are seven continents.")), settings)
    assert r.tier == "local"


def test_numbers_in_an_answer_are_compared_by_value():
    assert fastcalc.numbers_in("about 8,849 metres, fifty states, 3.5 times") == [8849.0, 50.0, 3.5]
    assert fastcalc.same_numbers("It is 8,848.86 m.", "It is 8,849 m.") and fastcalc.same_numbers("Fifty.", "50.")
    assert not fastcalc.same_numbers("It is 150,000.", "It is 57,600.") and not fastcalc.same_numbers("One.", "One or two.")


@pytest.mark.parametrize("text", ["what is the tallest building in the world", "how many moons does Jupiter have",
                                  "what is the fastest animal"])
def test_facts_that_change_are_not_for_the_model(text):
    assert voice_fast.gate(text)[0] is False


@pytest.mark.parametrize("first,verify,why", [
    (say("", confident=False), None, "not confident"),
    (say("It is Paris.", confident=False), None, "not confident"),
    (say("Needs your files.", needs_tools=True), None, "needs tools"),
    (say("Probably Paris."), None, "not confident"),
    (say("I think it is Paris."), None, "not confident"),
    (say("The answer is **Paris**."), None, "not confident"),
    (say(" ".join(["word"] * 40)), None, "not confident"),
    (say("Paris."), {"well_known": True, "answers_question": False, "correct": True}, "check failed"),
    (say("Paris."), {"well_known": True, "answers_question": True, "correct": False}, "check failed"),
    (say("Paris."), {"well_known": False, "answers_question": True, "correct": True}, "check failed"),
    (say("I do not have any information about that."), None, "not confident"),
    (say("It is a fictional device."), None, "not confident"),
    ("not a dict", None, "not confident")])
def test_any_doubt_goes_to_claude(settings, first, verify, why):
    r = run("what is the capital of France", asker(first, verify), settings)
    assert r.tier == "claude" and r.why == f"local: {why}"


def test_a_slow_model_is_given_up_on_at_the_cap_and_then_skipped_for_a_while(settings):
    settings.voice_fast_cap_s = 0.3
    t = time.monotonic()
    r = run("what is the capital of France", asker(say("Paris."), delay=2.0), settings)
    assert r.tier == "claude" and r.why == "local: timeout" and time.monotonic() - t < 1.0
    t = time.monotonic()
    again = run("who wrote Hamlet", asker(say("Shakespeare.")), settings)
    assert again.tier == "claude" and again.why == "local: backoff" and time.monotonic() - t < 0.1


def test_a_failing_model_is_a_miss_not_an_error(settings):
    def broken(messages, model, timeout, schema=None):
        raise OSError("connection refused")
    r = run("what is the capital of France", broken, settings)
    assert r.tier == "claude" and r.why == "local: model error"


def test_a_model_that_is_not_loaded_is_never_waited_for(settings):
    r = run("what is the capital of France", asker(say("Paris.")), settings, loaded=("qwen3.6:35b-a3b",))
    assert r.tier == "claude" and r.why == "local: model not loaded" and r.ms < 100
    r = run("what is the capital of France", asker(say("Paris.")), settings, loaded=("gemma4",))
    assert r.tier == "local"  # "gemma4" and "gemma4:latest" are the same model


def test_the_model_is_the_judges_unless_set(settings):
    assert voice_fast.model_for(settings) == "gemma4:latest"
    settings.voice_fast_model = "other:1b"
    assert voice_fast.model_for(settings) == "other:1b"


def test_the_options_are_the_judges_so_ollama_keeps_the_loaded_copy():
    assert "num_ctx" not in voice_fast.OPTIONS and "num_ctx" not in str(voice_fast.SCHEMA)
    assert voice_fast.OPTIONS == {"temperature": 0, "num_predict": 110}


def test_a_low_confidence_transcript_is_not_answered_by_the_model_but_a_clock_still_is(settings):
    info = {"stt_confident": False, "stt_avg_logprob": -1.4}
    r = run("what is the capital of France", asker(say("Paris.")), settings, voice_info=info)
    assert r.tier == "claude" and r.why == "low speech confidence"
    assert run("what time is it", asker(say("x")), settings, voice_info=info).tier == "code"
    dictated = {"stt_confident": False, "stt_confident_why": ["PC wake word: no recogniser numbers"]}
    assert run("what is the capital of France", asker(say("Paris.")), settings, voice_info=dictated).tier == "local"


def test_the_switches(settings):
    settings.voice_fast_local = False
    r = run("what is the capital of France", asker(say("Paris.")), settings)
    assert r.tier == "claude" and r.why == "local off"
    assert run("what time is it", None, settings).tier == "code"
    settings.voice_fast_path = False
    r = run("what time is it", None, settings)
    assert r.tier == "claude" and r.why == "off"


# ---- the engine --------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    chat_engine._available.clear()
    yield


def voice_turn(settings, text, script=None, fake=None, chat="telegram:42", note="", **kw):
    """One PC-voice style turn (a LineDelivery that hands out sentences, no audio), with a fake Claude."""
    settings.edit_interval_s = 0.05
    heard = []
    d = LineDelivery(audio=False, fmt="wav", on_sentence=heard.append)
    fake = fake or FakeClaude(script or (lambda proc, msg: proc.answer("SPOKEN: From Claude, sir.\nANSWER:\nFrom Claude.")))
    sess = ChatSession(chat, settings=settings, popen=fake, claude="claude")
    rec = chat_engine.run_turn(chat, text, d, context="", settings=settings, session=sess, platform="telegram",
                               voice=True, note=note, voice_info={"voice": True, "transcript_source": "fake"}, **kw)
    return rec, d, heard, fake


def test_a_spoken_clock_question_never_reaches_claude(settings, monkeypatch):
    class Boom:
        def turn(self, *a, **k):
            raise AssertionError("Claude must not be asked")
    monkeypatch.setattr(chat_engine, "claude_available", lambda s: (_ for _ in ()).throw(AssertionError("no check")))
    settings.edit_interval_s = 0.05
    heard = []
    d = LineDelivery(audio=False, fmt="wav", on_sentence=heard.append)
    t = time.time()
    rec = chat_engine.run_turn("telegram:42", "what time is it", d, context="", settings=settings, session=Boom(),
                               platform="telegram", voice=True, voice_info={"voice": True})
    assert time.time() - t < 1.0
    assert rec.answered_by == "code" and rec.outcome == "ok" and rec.delivered and not rec.failure_notice
    assert len(heard) == 1 and heard[0].startswith("It is ") and heard[0].endswith(", sir.")
    assert d.text.startswith('Heard: "what time is it"') and "SPOKEN" not in d.text and "ANSWER" not in d.text
    assert rec.voice["fast_tier"] == "code" and rec.voice["fast_kind"] == "time" and rec.voice["spoken"] == heard[0]
    assert rec.voice["fast_s"] < 0.5 and rec.steps == [] and rec.usage == {}
    # recorded as a normal turn
    row = traces.get(rec.trace_id)
    assert row["answered_by"] == "code" and row["turn_outcome"] == "ok" and row["request"] == "what time is it"
    assert row["reply"].startswith("It's ") and row["chat"] == "telegram:42" and row["origin"] == "user"
    assert json.loads(row["extra"])["voice_turn"]["fast_tier"] == "code"


def test_the_local_tier_is_a_normal_turn_answered_by_local(settings, monkeypatch):
    monkeypatch.setattr(voice_fast, "loaded_models", lambda url="", timeout=0: ["gemma4:latest"])
    monkeypatch.setattr(voice_fast, "ollama_ask", asker(say("The capital of France is Paris.")))
    rec, d, heard, fake = voice_turn(settings, "what is the capital of France")
    assert fake.procs == []  # Claude was never started
    assert rec.answered_by == "local" and rec.model == "gemma4:latest" and rec.outcome == "ok"  # not a degraded fallback
    assert heard == ["The capital of France is Paris, sir."]
    assert "(Answered by Waggle's small local model" not in d.text and "The capital of France is Paris." in d.text
    assert traces.get(rec.trace_id)["answered_by"] == "local"


def test_a_miss_goes_to_claude_exactly_as_before(settings, monkeypatch):
    monkeypatch.setattr(voice_fast, "loaded_models", lambda url="", timeout=0: ["gemma4:latest"])
    monkeypatch.setattr(voice_fast, "ollama_ask", asker(say("", confident=False)))
    rec, d, heard, fake = voice_turn(settings, "what is the meaning of life")
    assert rec.answered_by.startswith("claude") and heard == ["From Claude, sir."]
    assert rec.voice["fast_why"] == "local: not confident" and "fast_tier" not in rec.voice
    assert "<voice_reply_format>" in fake.procs[0].inputs[0] and "<waggle_context>" not in fake.procs[0].inputs[0]


@pytest.mark.parametrize("text", ["what's on my screen", "open a Google Doc called Amazon", "remind me at 5",
                                  "what did I say about BIOBUZZ yesterday", "how's my robot code"])
def test_requests_that_need_claude_go_to_claude(settings, monkeypatch, text):
    monkeypatch.setattr(voice_fast, "loaded_models", lambda url="", timeout=0: ["gemma4:latest"])
    monkeypatch.setattr(voice_fast, "ollama_ask", lambda *a, **k: pytest.fail("the model must not be asked"))
    rec, d, heard, fake = voice_turn(settings, text)
    assert rec.answered_by.startswith("claude") and heard == ["From Claude, sir."]


def test_a_note_or_a_restart_keeps_the_fast_path_out_of_it(settings):
    rec, _, heard, _ = voice_turn(settings, "what time is it", note="The user approved: x.")
    assert rec.answered_by.startswith("claude")
    rec, _, heard, _ = voice_turn(settings, "what time is it", attempt=2)
    assert rec.answered_by.startswith("claude")


def typed_turn(settings, text, script=None, fake=None, chat="telegram:42", **kw):
    """One typed Telegram message with a fake Claude: (record, delivery, fake)."""
    from engine_fakes import FakeDelivery
    settings.edit_interval_s = 0.05
    d = FakeDelivery()
    fake = fake or FakeClaude(script or (lambda proc, msg: proc.answer("From Claude.")))
    sess = ChatSession(chat, settings=settings, popen=fake, claude="claude")
    kw.setdefault("platform", "telegram")
    rec = chat_engine.run_turn(chat, text, d, context="", settings=settings, session=sess, **kw)
    return rec, d, fake


def test_a_typed_local_tier_question_waits_for_claude(settings, monkeypatch):
    monkeypatch.setattr(voice_fast, "loaded_models", lambda url="", timeout=0: pytest.fail("the model must not be asked"))
    monkeypatch.setattr(voice_fast, "ollama_ask", lambda *a, **k: pytest.fail("the model must not be asked"))
    rec, d, fake = typed_turn(settings, "what is the capital of France")
    assert rec.answered_by.startswith("claude") and d.messages == ["From Claude."] and "fast_tier" not in rec.voice


@pytest.mark.parametrize("text, starts", [("what time is it", "It's "), ("what day is it", "Today is "),
                                          ("what's 15 percent of 80", "15 percent of 80"), ("how many feet in a mile", "1 mile is 5,280 feet")])
def test_a_typed_code_question_is_answered_at_once_and_never_reaches_claude(settings, text, starts):
    rec, d, fake = typed_turn(settings, text)
    assert fake.procs == [] and rec.answered_by == "code" and rec.outcome == "ok" and rec.delivered
    assert d.messages[-1].startswith(starts) and "SPOKEN" not in d.messages[-1] and "ANSWER" not in d.messages[-1]
    assert "sir" not in d.messages[-1] and "Heard" not in d.messages[-1]  # the written text, not the spoken one
    assert rec.voice["fast_tier"] == "code" and rec.voice["typed"] is True and rec.steps == []
    row = traces.get(rec.trace_id)
    assert row["answered_by"] == "code" and row["turn_outcome"] == "ok" and row["request"] == text
    assert json.loads(row["extra"])["voice"] is False


def test_a_typed_reminders_question_reads_the_store(settings):
    from waggle import followups
    followups.add(dt.datetime.now().timestamp() + 7200, "call the dentist", chat="telegram:42")
    rec, d, fake = typed_turn(settings, "what are my reminders")
    assert fake.procs == [] and rec.answered_by == "code" and "call the dentist" in d.messages[-1]


def test_claude_sees_a_typed_quick_exchange_on_its_next_message(settings):
    rec, _, _ = typed_turn(settings, "what day is it")
    assert rec.answered_by == "code"
    rec2, d2, fake = typed_turn(settings, "and tomorrow?")
    first = fake.procs[0].inputs[0]
    assert rec2.answered_by.startswith("claude")
    assert "answered these without you" in first and "User: what day is it" in first and "Waggle: Today is " in first


@pytest.mark.parametrize("text", ["/time", "/status what time is it", "  /help"])
def test_a_slash_command_goes_the_normal_way(settings, text):
    rec, d, fake = typed_turn(settings, text)
    assert rec.answered_by.startswith("claude") and len(fake.procs) == 1


def test_attachments_notes_restarts_and_other_origins_go_the_normal_way(settings):
    cases = [dict(attachments="<waggle_attachments>a photo</waggle_attachments>"),
             dict(note="The user approved: x."), dict(attempt=2), dict(origin="canary"), dict(origin="agent"),
             dict(origin="bench")]
    for kw in cases:
        rec, d, fake = typed_turn(settings, "what time is it", **kw)
        assert rec.answered_by.startswith("claude"), kw


def test_a_test_marked_message_is_a_canary_and_goes_the_normal_way(settings):
    marked, text = chat_engine.test_marked("[waggle-test] what time is it")
    origin = chat_engine.marked_origin("user") if marked else "user"
    rec, d, fake = typed_turn(settings, text, origin=origin)
    assert origin == "canary" and rec.answered_by.startswith("claude")


def test_the_typed_switch_turns_only_the_typed_path_off(settings):
    settings.text_fast_path = False
    rec, d, fake = typed_turn(settings, "what time is it")
    assert rec.answered_by.startswith("claude")
    rec, _, heard, _ = voice_turn(settings, "what time is it")  # the voice path is its own switch
    assert rec.answered_by == "code"
    settings.text_fast_path = True
    settings.voice_fast_path = False
    rec, d, fake = typed_turn(settings, "what time is it")
    assert rec.answered_by == "code"


def test_text_route_is_the_code_tier_only_and_never_raises(settings, monkeypatch):
    r = voice_fast.text_route("what time is it", settings, now=NOW)
    assert r.tier == "code" and r.fast.written == "It's 11:46 AM."
    assert voice_fast.text_route("who wrote Hamlet", settings, now=NOW).why == "not a code answer"
    assert voice_fast.text_route("/time", settings, now=NOW).why == "slash command"
    settings.text_fast_path = False
    assert voice_fast.text_route("what time is it", settings, now=NOW).why == "off"
    settings.text_fast_path = True
    monkeypatch.setattr(voice_fast, "code_answer", lambda *a, **k: 1 / 0)
    assert voice_fast.text_route("what time is it", settings, now=NOW).tier == "claude"


def test_the_typed_turn_logs_one_line_without_the_words(settings, caplog):
    with caplog.at_level(logging.INFO, logger="waggle.voice_fast"):
        typed_turn(settings, "what time is it")
    lines = [r.getMessage() for r in caplog.records if r.name == "waggle.voice_fast"]
    assert len(lines) == 1 and lines[0].startswith("typed turn tier=code time=") and "time is it" not in lines[0]


def test_the_switch_turns_the_whole_path_off(settings):
    settings.voice_fast_path = False
    rec, _, heard, _ = voice_turn(settings, "what time is it")
    assert rec.answered_by.startswith("claude") and heard == ["From Claude, sir."]


def test_claude_sees_the_quick_exchange_on_its_next_message_once(settings):
    voice_turn(settings, "what day is it")
    fake = FakeClaude(lambda proc, msg: proc.answer("SPOKEN: Sunday, sir.\nANSWER:\nSunday."))
    rec, _, heard, fake = voice_turn(settings, "and tomorrow?", fake=fake)
    first = fake.procs[0].inputs[0]
    assert rec.answered_by.startswith("claude")
    assert "answered these without you" in first and "User: what day is it" in first
    assert "Waggle: Today is " in first and first.index("User: what day is it") < first.index("and tomorrow?")
    # Claude answered it, so the next message does not repeat the exchange
    rec2, _, _, fake2 = voice_turn(settings, "thanks, and the day after", fake=fake)
    assert "answered these without you" not in fake.procs[-1].inputs[-1]


def test_unseen_note_lists_only_what_claude_did_not_answer(settings):
    chat = "telegram:77"
    for req, rep, by in (("old q", "old a", "claude-sonnet-5-5"), ("what time is it", "It's 9 AM.", "code"),
                         ("who wrote Hamlet", "Shakespeare.", "local"), ("approve ABCD", "Approved.", "none")):
        traces.record(req, session=chat, chat=chat, reply=rep, answered_by=by, origin="user")
    note = voice_fast.unseen_note(chat)
    assert "User: what time is it\nWaggle: It's 9 AM." in note and "User: who wrote Hamlet" in note
    assert "old q" not in note and "approve ABCD" not in note
    assert voice_fast.unseen_note("telegram:other") == ""
    traces.record("and then?", session=chat, chat=chat, reply="Done.", answered_by="claude-sonnet-5-5", origin="user")
    assert voice_fast.unseen_note(chat) == ""


def test_one_info_line_per_voice_turn_names_the_tier_and_never_the_words(settings, caplog):
    with caplog.at_level(logging.INFO, logger="waggle.voice_fast"):
        voice_turn(settings, "what time is it")
        voice_turn(settings, "what's on my screen")
    lines = [r.getMessage() for r in caplog.records if r.name == "waggle.voice_fast"]
    assert len(lines) == 2
    assert lines[0].startswith("voice turn tier=code time=") and lines[0].endswith("s")
    assert lines[1].startswith("voice turn tier=claude time=") and "why=gate: private or time-sensitive word" in lines[1]
    assert all("time is it" not in x and "screen" not in x for x in lines)


def test_the_streamed_response_reports_the_tier(settings):
    rec, _, _, _ = voice_turn(settings, "what time is it")
    assert _timings(rec)["tier"] == "code" and _timings(rec)["fast_s"] >= 0
    rec, _, _, _ = voice_turn(settings, "what's on my screen")
    assert "tier" not in _timings(rec) or _timings(rec)["tier"] == "claude"


def test_a_quick_answer_is_not_counted_as_a_local_fallback_by_the_learning_loop(settings):
    assert traces.turn_outcome(answered_by="local", delivered=True)[0] == "degraded"
    assert traces.turn_outcome(answered_by="local", delivered=True, fast=True)[0] == "ok"


# ---- Step 1: a deploy's code-path canary is answered in code --------------------------------------------------------

def test_a_code_path_canary_is_answered_in_code_and_never_reaches_claude(settings):
    q = "canary ab12cd (code path): reply with exactly: pong ab12cd"
    rec, d, fake = typed_turn(settings, q, origin="canary")
    assert fake.procs == [] and rec.answered_by == "code" and rec.delivered
    assert d.messages[-1] == "pong ab12cd" and rec.voice["fast_kind"] == "canary"
    rec2, d2, fake2 = typed_turn(settings, "and what's new?")  # the owner's next message: the canary is not in it
    assert "pong ab12cd" not in fake2.procs[0].inputs[0]


def test_the_same_words_from_the_owner_or_the_old_wording_still_go_to_claude(settings):
    rec, _, fake = typed_turn(settings, "canary ab12cd (code path): reply with exactly: pong ab12cd")  # origin user
    assert rec.answered_by.startswith("claude") and fake.procs
    rec, _, fake = typed_turn(settings, "canary ab12cd: reply with exactly: pong ab12cd", origin="canary")
    assert rec.answered_by.startswith("claude") and fake.procs  # the daily full round trip
