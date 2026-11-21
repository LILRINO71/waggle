"""Who is being addressed (waggle/voice_intent.py), sentence-by-sentence streaming of the spoken part
(waggle/voice_reply.spoken_sentences, VoiceReply.on_sentence) and the voice evaluation runner. Fakes only: the model is
a function, the clock is a list, nothing calls Ollama, a microphone or a speaker."""
import json
import time

import pytest

from waggle import voice_bench, voice_intent, voice_reply
from waggle.voice_intent import Decision, SpokenLog, decide, echo_score, is_echo, judge, parse_verdict, stop_phrase


def model(directed=True, stop=False, confidence=0.9):
    """A fake judge model: returns the dict a real one would, and records what it was asked."""
    calls = []

    def ask(messages, name, timeout):
        calls.append((messages, name, timeout))
        return {"directed": directed, "stop": stop, "confidence": confidence}
    ask.calls = calls
    return ask


# ---- echo ----------------------------------------------------------------------------------------------------

SAID = "The reminder is set for ten minutes from now, sir."


@pytest.mark.parametrize("heard", [
    "the reminder is set for ten minutes from now sir",
    "reminder is set for ten minutes from now",
    "set for ten minutes from now sir",
    "the reminder is set for ten minute from now sir",  # one word misheard
])
def test_what_waggle_just_said_coming_back_is_an_echo(heard):
    assert is_echo(heard, [SAID])


@pytest.mark.parametrize("heard", [
    "set it for fifteen minutes instead",
    "what is the reminder for",
    "ten minutes",
    "from now",  # fewer than three words is never called an echo
    "okay thanks",
])
def test_the_owner_reusing_a_few_of_its_words_is_not_an_echo(heard):
    assert not is_echo(heard, [SAID])


def test_echo_score_is_the_share_of_the_transcript_found_in_order():
    assert echo_score("the reminder is set", SAID) == 1.0
    assert echo_score("sir set the reminder", SAID) < 0.75  # out of order
    assert echo_score("", SAID) == 0.0 and echo_score("hello there", "") == 0.0


def test_the_spoken_log_forgets_after_the_window_and_joins_the_last_two():
    t = [100.0]
    log = SpokenLog(window_s=60, clock=lambda: t[0])
    log.add("First sentence.")
    t[0] += 10
    log.add("Second sentence.")
    assert log.recent() == ["First sentence.", "Second sentence.", "First sentence. Second sentence."]
    assert log.last() == "Second sentence."
    t[0] += 55  # the first is now 65 s old
    assert log.recent() == ["Second sentence."]
    t[0] += 10
    assert log.recent() == []
    log.add("   ")
    assert log.recent() == []


def test_an_echo_across_a_sentence_boundary_is_caught_through_the_joined_pair():
    log = SpokenLog()
    log.add("Your calendar shows three events today.")
    log.add("The first is at nine.")
    assert is_echo("three events today the first is at nine", log.recent())


# ---- stop ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["stop", "Stop!", "never mind", "Never mind.", "okay that's enough", "Waggle, stop",
                                  "be quiet please", "cancel that", "oh forget it", "stop talking", "that will do sir"])
def test_a_whole_utterance_stop_phrase_is_a_stop(text):
    assert stop_phrase(text)


@pytest.mark.parametrize("text", ["stop the timer", "never mind the traffic, what about the train", "cancel my meeting at three",
                                  "don't stop the music", "", "what's the weather"])
def test_a_request_that_only_contains_the_word_is_not_a_stop(text):
    assert not stop_phrase(text)


# ---- the model's answer --------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ('{"directed": true, "stop": false, "confidence": 0.8}', (True, False, 0.8)),
    ({"directed": False, "stop": True, "confidence": 1}, (False, True, 1.0)),
    ('noise {"directed": false, "stop": false, "confidence": 0.2} noise', (False, False, 0.2)),
    ('{"directed": true}', (True, False, 0.5)),  # no confidence: middling
    ('{"directed": true, "confidence": 7}', (True, False, 1.0)),  # clamped
    ('{"directed": true, "confidence": "high"}', (True, False, 0.5)),
])
def test_the_verdict_is_read_from_json(raw, want):
    assert parse_verdict(raw) == want


@pytest.mark.parametrize("raw", ["", "I think so", "{}", '{"directed": "yes"}', "[1]", None, '{"directed": 1}'])
def test_an_unreadable_verdict_is_none(raw):
    assert parse_verdict(raw) is None


def test_the_prompt_carries_what_waggle_said_and_whether_it_asked():
    ask = model()
    judge("yes please", last_spoken="Shall I add a note?", asked_question=True, model="m", ask=ask)
    msgs, name, timeout = ask.calls[0]
    user = msgs[1]["content"]
    assert name == "m" and 'Waggle last said: "Shall I add a note?"' in user and "asked the owner a question: yes" in user
    assert 'Heard: "yes please"' in user and msgs[0]["role"] == "system"


# ---- the decision --------------------------------------------------------------------------------------------

def test_directed_and_sure_is_sent():
    d = decide("what about tomorrow", ask=model(True, False, 0.9))
    assert (d.action, d.source) == ("send", "model")


def test_not_directed_is_ignored():
    d = decide("honey did you take the trash out", ask=model(False, False, 0.95))
    assert (d.action, d.source) == ("ignore", "model")


def test_directed_but_unsure_is_ignored_unless_waggle_just_asked_a_question():
    unsure = model(True, False, 0.4)
    assert decide("maybe", ask=unsure).action == "ignore"
    assert decide("maybe", ask=unsure, asked_question=True).action == "send"
    assert decide("maybe", ask=unsure, min_confidence=0.3).action == "send"  # the threshold is a setting


def test_a_sure_stop_from_the_model_stops_and_an_unsure_one_does_not():
    assert decide("hold on, that's not what I meant", ask=model(False, True, 0.8)).action == "stop"
    assert decide("hmm", ask=model(True, True, 0.55)).action == "send"  # directed, not sure it was a stop
    assert decide("hmm", ask=model(False, True, 0.4)).action == "ignore"


def test_the_stop_phrase_rule_answers_without_asking_the_model():
    ask = model()
    d = decide("never mind", ask=ask)
    assert (d.action, d.source) == ("stop", "rule") and ask.calls == []


def test_an_echo_is_dropped_without_asking_the_model():
    ask = model()
    d = decide("the reminder is set for ten minutes from now sir", spoken_texts=[SAID], ask=ask)
    assert (d.action, d.source) == ("echo", "rule") and ask.calls == []


def test_rules_can_be_left_out_to_measure_the_model_alone():
    ask = model(False, False, 0.9)
    d = decide("never mind", ask=ask, rules=False)
    assert d.action == "ignore" and len(ask.calls) == 1


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), ConnectionRefusedError("down"), ValueError("bad"),
                                     lambda *a: "garbage", lambda *a: {"directed": "maybe"}])
def test_a_slow_down_or_confused_model_falls_back_to_sending(failure):
    def ask(messages, name, timeout):
        if isinstance(failure, Exception):
            raise failure
        return failure(messages, name, timeout)
    d = decide("what about tomorrow", ask=ask)
    assert (d.action, d.source) == ("send", "fallback") and d.reason and d.verdict.source == "fallback"


def test_without_a_judge_it_is_todays_behaviour():
    d = decide("what about tomorrow", use_model=False)
    assert (d.action, d.source) == ("send", "fallback")


def test_judge_passes_the_timeout_and_model_to_the_ask_function():
    ask = model()
    judge("hi", model="qwen3.6:35b-a3b", ask=ask, timeout=2.5)
    assert ask.calls[0][1:] == ("qwen3.6:35b-a3b", 2.5)


def test_the_real_ollama_call_asks_for_json_no_thinking_and_keeps_the_model_loaded(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"message": {"content": '{"directed": true, "stop": false, "confidence": 0.7}'}}).encode()

    def fake_urlopen(req, timeout=None):
        seen.update(url=req.full_url, body=json.loads(req.data), timeout=timeout)
        return Resp()
    monkeypatch.setattr(voice_intent.urllib.request, "urlopen", fake_urlopen)
    msgs = [{"role": "user", "content": "x"}]
    assert voice_intent.ollama_ask(msgs, "m1", 2.0, url="http://127.0.0.1:1/") == {
        "directed": True, "stop": False, "confidence": 0.7}
    b = seen["body"]
    assert seen["url"] == "http://127.0.0.1:1/api/chat" and seen["timeout"] == 2.0
    assert b["model"] == "m1" and b["think"] is False and b["stream"] is False and b["keep_alive"] == "30m"
    assert b["format"]["required"] == ["directed", "stop", "confidence"] and b["options"]["temperature"] == 0


def test_warm_loads_the_model_without_a_prompt_and_never_raises(monkeypatch):
    seen = {}

    class Resp:
        def read(self):
            return b"{}"
    monkeypatch.setattr(voice_intent.urllib.request, "urlopen",
                        lambda req, timeout=None: seen.update(body=json.loads(req.data), url=req.full_url) or Resp())
    assert voice_intent.warm("m1", url="http://127.0.0.1:1") is True
    assert seen["url"].endswith("/api/generate") and seen["body"] == {"model": "m1", "keep_alive": "30m"}

    def down(req, timeout=None):
        raise ConnectionRefusedError("down")
    monkeypatch.setattr(voice_intent.urllib.request, "urlopen", down)
    assert voice_intent.warm("m1") is False


# ---- speaking sentence by sentence (the core's side) ---------------------------------------------------------

REPLY = ("SPOKEN: Done, sir. The reminder is set for ten minutes from now. Shall I add a note to it?\n"
         "ANSWER:\nReminder set for ten minutes from now.\n\nDetails: nothing else is pending.")


def stream(text, step=7, **kw):
    """The sentences handed out while `text` arrives `step` characters at a time, with the length of the text
    seen at each hand-out."""
    got, seen = [], []
    vr = voice_reply.VoiceReply("unused", audio=False, on_sentence=lambda s: (got.append(s), seen.append(len(partial[0]))),
                                **kw)
    partial = [""]
    for i in range(step, len(text) + step, step):
        partial[0] = text[:i]
        vr.feed(partial[0])
    vr.finish(text)
    return got, seen, vr


def test_the_first_sentence_is_handed_out_while_the_rest_is_still_being_written():
    got, seen, vr = stream(REPLY)
    assert got == ["Done, sir.", "The reminder is set for ten minutes from now.", "Shall I add a note to it?"]
    assert seen[0] < REPLY.index("The reminder is set") + 10 and seen[0] < REPLY.index("ANSWER")
    assert seen[1] < REPLY.index("ANSWER") + 4  # the second one too, before the written answer
    assert vr.spoken == " ".join(got)


def test_no_sentence_is_ever_handed_out_twice_and_the_order_is_kept():
    for step in (1, 3, 11, 50, 1000):
        got, _, _ = stream(REPLY, step=step)
        assert got == ["Done, sir.", "The reminder is set for ten minutes from now.", "Shall I add a note to it?"], step


def test_a_sentence_is_not_handed_out_until_it_is_finished():
    t = "SPOKEN: The build passed, sir. The deploy is half done"
    assert voice_reply.spoken_sentences(t) == ["The build passed, sir."]
    assert voice_reply.spoken_sentences(t + ".") == ["The build passed, sir."]  # no text after the full stop yet
    assert voice_reply.spoken_sentences(t + ". ") == ["The build passed, sir."]
    assert voice_reply.spoken_sentences(t + ". It", final=False)[-1] == "The deploy is half done."
    assert voice_reply.spoken_sentences(t + ".", final=True)[-1] == "The deploy is half done."


def test_the_spoken_cap_holds_while_streaming_three_sentences_and_forty_five_words():
    many = "SPOKEN: One. Two. Three. Four. Five.\nANSWER:\nx"
    got, _, vr = stream(many)
    assert got == ["One.", "Two.", "Three."] and vr.spoken == "One. Two. Three."
    words = " ".join(f"w{i}" for i in range(30))
    long_reply = f"SPOKEN: {words}. {words}. Short.\nANSWER:\nx"
    got, _, vr = stream(long_reply)
    assert got == [words + "."]  # the second would pass 45 words, so the first is all there is
    assert len(" ".join(got).split()) <= voice_reply.MAX_WORDS
    one_long = "SPOKEN: " + " ".join(f"w{i}" for i in range(80)) + ".\nANSWER:\nx"
    got, _, _ = stream(one_long)
    assert len(got) == 1 and len(got[0].split()) == voice_reply.MAX_WORDS


def test_markdown_and_paths_are_not_spoken_in_a_streamed_sentence():
    got, _, _ = stream("SPOKEN: **Done**, sir. See C:\\Users\\x\\file.txt for `details`.\nANSWER:\nx")
    assert got == ["Done, sir.", "See for details."]


def test_a_reply_without_a_spoken_part_is_spoken_at_the_end_from_the_answer():
    text = "The build passed. The deploy is done. Nothing else is pending. And a fourth."
    got, seen, vr = stream(text)
    assert got == ["The build passed.", "The deploy is done.", "Nothing else is pending."]
    assert seen == [len(text)] * 3 and not vr.had_spoken  # nothing before the end: there is no SPOKEN part to trust


def test_a_reply_with_spoken_but_no_answer_marker_uses_the_first_paragraph():
    text = "SPOKEN: Done, sir. All good.\n\nThe long version follows here."
    got, _, vr = stream(text)
    assert got == ["Done, sir.", "All good."]


def test_a_failing_sentence_callback_never_breaks_the_reply():
    def boom(s):
        raise RuntimeError("the PC went away")
    vr = voice_reply.VoiceReply("unused", audio=False, on_sentence=boom)
    vr.feed(REPLY)
    assert vr.finish(REPLY)[1].startswith("Done, sir.")


def test_cap_spoken_is_unchanged_by_the_refactor():
    assert voice_reply.cap_spoken("One. Two. Three. Four.") == "One. Two. Three."
    assert voice_reply.cap_sentences(["a b", "c d"], 3, 3) == ["a b"]
    assert voice_reply.cap_spoken("x " * 3 + "y.", 3, 2) == "x x."


# ---- the evaluation runner -----------------------------------------------------------------------------------

def test_the_bench_file_has_fifty_labelled_cases_of_four_kinds():
    cases = voice_bench.load()
    assert 40 <= len(cases) <= 60 and len({c["id"] for c in cases}) == len(cases)
    kinds = {c["want"] for c in cases}
    assert kinds == {"directed", "not_directed", "stop", "echo"}
    assert all(c["text"].strip() and "asked" in c and "last_spoken" in c for c in cases)


def test_with_no_model_the_rules_score_exactly_what_today_does_not_catch():
    rows = voice_bench.run(voice_bench.load(), use_model=False)
    sc = voice_bench.score(rows)
    assert sc["by_label"]["echo"] == "8/8" and sc["by_label"]["directed"] == "20/20"  # no false echo, no false stop
    assert sc["by_label"]["not_directed"] == "0/14"  # without a model everything is sent: today's behaviour
    assert set(sc["by_source"]) == {"rule", "fallback"}


def test_a_perfect_fake_model_scores_everything_and_a_bad_one_is_reported():
    cases = voice_bench.load()
    want = {c["text"]: c["want"] for c in cases}

    def oracle(messages, name, timeout):
        heard = messages[1]["content"].split('Heard: "')[1].rstrip('"')
        w = want[heard]
        return {"directed": w == "directed", "stop": w == "stop", "confidence": 0.9}
    sc = voice_bench.score(voice_bench.run(cases, ask=oracle, model="fake"))
    assert sc["accuracy"] == 1.0 and sc["wrong"] == []
    sc = voice_bench.score(voice_bench.run(cases, ask=oracle, model="fake", rules=False))
    assert sc["accuracy"] == 1.0  # model alone: an echo labelled "echo" counts when the model says not directed

    def always_yes(messages, name, timeout):
        return {"directed": True, "stop": False, "confidence": 0.9}
    sc = voice_bench.score(voice_bench.run(cases, ask=always_yes, model="fake"))
    assert sc["by_label"]["not_directed"] == "0/14" and any(w["id"] == "n01" for w in sc["wrong"])


def test_the_runner_is_reachable_as_python_m_waggle_bench_voice(capsys):
    from waggle import bench
    assert bench.main(["voice", "--no-model"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "rules only" and out["cases"] == len(voice_bench.load())


def test_decision_is_importable_and_prints():
    assert "send" in repr(Decision("send", "fallback")) and "model" in repr(voice_intent.Verdict(True, False, 0.9, "model"))
    assert time.monotonic() > 0
