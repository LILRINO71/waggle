"""Step 1: the chat session is replaced by size (chat_session_max_tokens), with a local-model summary carried into
the fresh session, instead of after a fixed number of turns."""
import threading

from engine_fakes import FakeClaude, ev_init, ev_result, ev_text

from waggle import chat_session, chat_store
from waggle.chat_session import ChatSession
from waggle.core import store

import pytest


@pytest.fixture(autouse=True)
def _close_store():
    yield
    store.close_all()


def sized(tokens_by_turn):
    """A fake claude whose n-th answer reports a last call of tokens_by_turn[n] context tokens."""
    state = {"n": 0}

    def script(proc, text):
        n = state["n"]
        state["n"] += 1
        tok = tokens_by_turn[min(n, len(tokens_by_turn) - 1)]
        proc.emit(ev_init(proc.session_id))
        proc.emit(ev_text(proc.session_id, f"answer {n}", msg_id=f"m{n}"))
        usage = {"input_tokens": 5, "cache_read_input_tokens": tok * 3, "output_tokens": 10,  # summed over calls
                 "iterations": [{"input_tokens": 3, "cache_read_input_tokens": tok - 13, "output_tokens": 10}]}
        proc.emit(ev_result(proc.session_id, f"answer {n}", usage=usage))
    return FakeClaude(script)


def test_context_tokens_reads_the_last_call_not_the_sum():
    usage = {"input_tokens": 9, "cache_read_input_tokens": 90000, "output_tokens": 50,
             "iterations": [{"input_tokens": 2, "cache_read_input_tokens": 30000, "output_tokens": 20},
                            {"input_tokens": 3, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 31000,
                             "output_tokens": 30}]}
    assert chat_session.context_tokens(usage) == 32033
    assert chat_session.context_tokens({"input_tokens": 4, "cache_read_input_tokens": 100}) == 104
    assert chat_session.context_tokens(None) == 0


def test_a_small_session_is_resumed_past_the_old_turn_limit(settings):
    settings.chat_session_max_turns, settings.chat_session_max_tokens = 2, 60000
    fake = sized([20000])
    s = ChatSession("c", settings=settings, popen=fake, claude="claude", transcript=lambda chat, **k: "old talk",
                    summarizer=lambda convo, st: "should not be asked")
    for i in range(4):
        s.turn(f"t{i}")
        s.stop()  # each turn restarts the process: it must resume, not start fresh
    assert all(p.arg("--resume") for p in fake.procs[1:])
    assert chat_store.get_chat("c")["session_turns"] == 4


def test_crossing_the_size_starts_a_fresh_session_with_the_carried_summary(settings):
    settings.chat_session_max_tokens = 60000
    fake = sized([30000, 61000, 17000])
    asked = []
    done = threading.Event()

    def summarizer(convo, st):
        asked.append(convo)
        return "- The owner is building a robot.\n- Waggle promised a parts list."
    s = ChatSession("c", settings=settings, popen=fake, claude="claude",
                    transcript=lambda chat, n=8, max_chars=12000: f"User: q{n}\nWaggle: a{n}",
                    summarizer=summarizer)
    r1 = s.turn("first")
    assert r1.context_tokens == 30000 and not r1.rotating
    old = fake.procs[0].session_id
    orig = s.rotate
    s.rotate = lambda: (orig(), done.set())
    r2 = s.turn("second")
    assert r2.context_tokens == 61000 and r2.rotating
    assert done.wait(5)
    assert asked == [f"User: q{chat_session.SUMMARY_TURNS}\nWaggle: a{chat_session.SUMMARY_TURNS}"]
    assert len(fake.procs) == 2 and not fake.procs[0].returncode is None  # old process stopped, fresh one pre-started
    fresh = fake.procs[1]
    assert not fresh.arg("--resume") and fresh.session_id != old
    r3 = s.turn("third")
    assert r3.ok and len(fake.procs) == 2  # the pre-started process answers: no cold start
    sent = fresh.inputs[0]
    assert "The owner is building a robot." in sent and "Most recent exchanges:" in sent
    assert f"User: q{chat_session.CARRY_RECENT}" in sent and sent.endswith("third")
    s.stop()
    s.turn("fourth")  # the carried summary is not sent twice
    assert "building a robot" not in fake.procs[-1].inputs[-1]


def test_without_the_local_model_the_fresh_session_gets_the_transcript(settings):
    settings.chat_session_max_tokens = 1000
    fake = sized([5000, 900])
    s = ChatSession("c", settings=settings, popen=fake, claude="claude",
                    transcript=lambda chat, n=8, max_chars=12000: "User: hi\nWaggle: hello",
                    summarizer=lambda convo, st: "")
    s.rotate = lambda: False  # the background thread lost the race: the next turn must still start fresh
    s.turn("one")
    s.turn("two")
    assert len(fake.procs) == 2 and not fake.procs[1].arg("--resume")
    assert fake.procs[1].inputs[0].startswith("<earlier_conversation>\nUser: hi\nWaggle: hello")


def test_size_limit_zero_falls_back_to_the_turn_count(settings):
    settings.chat_session_max_tokens, settings.chat_session_max_turns = 0, 1
    fake = sized([500000])
    s = ChatSession("c", settings=settings, popen=fake, claude="claude", transcript=lambda chat: "")
    s.turn("one")
    s.stop()
    s.turn("two")
    assert not fake.procs[1].arg("--resume")


def test_local_summary_is_empty_when_the_model_fails():
    def boom(*a, **k):
        raise RuntimeError("ollama down")
    assert chat_session.local_summary("User: x", chat_fn=boom) == ""
    assert chat_session.local_summary("User: x", chat_fn=lambda m, **k: '{"notes": ["a", "- b"]}') == "- a\n- b"
    assert chat_session.local_summary("   ", chat_fn=lambda *a, **k: "x") == ""
    got = chat_session.local_summary("User: x", chat_fn=lambda msgs, **k: " - a fact ")
    assert got == "- a fact"
