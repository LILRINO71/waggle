"""ChatSession / SessionPool against a fake `claude` process: start, turn, death, resume, timeout, reaping."""
import threading
import time

import pytest
from engine_fakes import FakeClaude, ev_result, ev_text

from waggle import chat_store
from waggle.chat_session import ChatSession, SessionPool
from waggle.core import store


@pytest.fixture(autouse=True)
def _close_store():
    yield
    store.close_all()


def session(fake, settings, chat="telegram:1", **kw):
    return ChatSession(chat, settings=settings, popen=fake, claude="claude", **kw)


def test_first_turn_starts_a_new_session_and_streams(settings):
    fake = FakeClaude()
    s = session(fake, settings)
    shown = []
    res = s.turn("hi", on_text=shown.append)
    proc = fake.procs[0]
    assert res.ok and res.text == "Hello from Claude." and shown[-1] == "Hello from Claude."
    assert "--session-id" in proc.cmd and "--input-format" in proc.cmd and "--resume" not in proc.cmd
    assert res.restarted and not res.resumed and res.fresh_session and res.ttfw_s is not None
    row = chat_store.get_chat("telegram:1")
    assert row["claude_session"] == proc.session_id and row["session_turns"] == 1
    assert row["model"] == "claude-haiku-5-5" and res.model == "claude-haiku-5-5"
    assert proc.kwargs["env"]["WAGGLE_CHAT"] == "telegram:1"


def test_second_turn_reuses_the_warm_process(settings):
    fake = FakeClaude()
    s = session(fake, settings)
    s.turn("one")
    res = s.turn("two")
    assert res.ok and len(fake.procs) == 1 and not res.restarted
    assert fake.procs[0].inputs == ["one", "two"] and chat_store.get_chat("telegram:1")["session_turns"] == 2


def test_process_dead_between_turns_resumes_the_stored_session(settings):
    fake = FakeClaude()
    s = session(fake, settings)
    s.turn("one")
    sid = fake.procs[0].session_id
    fake.procs[0].die(1)
    res = s.turn("two")
    assert res.ok and len(fake.procs) == 2 and res.restarted and res.resumed and not res.fresh_session
    assert fake.procs[1].arg("--resume") == sid


def test_process_dying_mid_turn_before_any_text_is_retried_once_with_resume(settings):
    calls = []

    def script(proc, text):
        calls.append(proc)
        if len(calls) == 2:
            proc.die(1, "crashed")  # the second message kills the first process
        else:
            proc.answer(f"answer {len(calls)}")
    fake = FakeClaude(script)
    s = session(fake, settings)
    s.turn("one")
    res = s.turn("two")
    assert res.ok and res.retried and res.resumed and res.text == "answer 3"
    assert fake.procs[1].arg("--resume") == fake.procs[0].session_id


def test_process_dying_after_text_returns_the_partial_text_as_a_crash(settings):
    def script(proc, text):
        proc.emit(ev_text(proc.session_id, "I started to")[:3])
        proc.die(1, "segfault")
    fake = FakeClaude(script)
    res = session(fake, settings).turn("go")
    assert not res.ok and res.kind == "crash" and res.text == "I st" and not res.retried
    assert "segfault" in res.error


def test_lost_session_starts_fresh_with_the_transcript(settings):
    fake = FakeClaude()
    s = session(fake, settings, transcript=lambda chat: "User: earlier\nWaggle: earlier answer")
    chat_store.set_session("telegram:1", "gone-session", turns=3)
    res = s.turn("hello again")
    assert res.ok and res.fresh_session and res.retried
    assert fake.procs[0].arg("--resume") == "gone-session" and fake.procs[-1].arg("--session-id")
    assert "<earlier_conversation>" in fake.procs[-1].inputs[0] and "hello again" in fake.procs[-1].inputs[0]
    assert chat_store.get_chat("telegram:1")["claude_session"] == fake.procs[-1].session_id


def test_long_sessions_restart_fresh(settings):
    settings.chat_session_max_turns = 2
    fake = FakeClaude()
    s = session(fake, settings, transcript=lambda chat: "User: x\nWaggle: y")
    chat_store.set_session("telegram:1", "old", turns=2)
    fake.known_sessions.add("old")
    res = s.turn("next")
    assert res.fresh_session and "--resume" not in fake.procs[0].cmd


def test_timeout_stops_the_process_and_the_next_turn_resumes(settings):
    answers = iter([None, "back"])

    def script(proc, text):
        a = next(answers)
        if a:
            proc.answer(a)
    fake = FakeClaude(script)
    s = session(fake, settings)
    res = s.turn("hang", timeout=0.3)
    assert res.kind == "timeout" and not res.ok and fake.procs[0].returncode is not None
    res = s.turn("again")
    assert res.ok and res.text == "back" and res.resumed


def test_error_result_sets_kind(settings):
    def script(proc, text):
        proc.emit(ev_result(proc.session_id, "Claude AI usage limit reached|1763600000", is_error=True))
    res = session(FakeClaude(script), settings).turn("x")
    assert not res.ok and res.kind == "limit" and res.text == ""


def test_missing_cli_is_reported(settings):
    res = ChatSession("c", settings=settings, popen=FakeClaude()).turn("x")  # find_claude() finds nothing in tests
    assert not res.ok and res.kind == "missing"


def test_turns_on_one_chat_are_serialised(settings):
    def script(proc, text):
        proc.answer(text.upper(), pieces=2, delay=0.05)
    s = session(FakeClaude(script), settings)
    out = []
    threads = [threading.Thread(target=lambda t=t: out.append(s.turn(t).text)) for t in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(out) == ["A", "B", "C"]


def test_reap_stops_only_idle_free_sessions(settings):
    settings.chat_idle_s = 10
    fake = FakeClaude()
    pool = SessionPool(factory=lambda chat: session(fake, settings, chat=chat), settings=settings)
    idle, busy, fresh = pool.get("a"), pool.get("b"), pool.get("c")
    for s in (idle, busy, fresh):
        s.turn("hi")
    now = time.time()
    idle.last_used = busy.last_used = now - 60
    busy.lock.acquire()
    try:
        assert pool.reap(now=now) == ["a"]
    finally:
        busy.lock.release()
    assert not idle.alive() and busy.alive() and fresh.alive() and pool.get("a") is idle
    res = idle.turn("back")
    assert res.ok and res.resumed
    pool.stop_all()
    assert not busy.alive()


def test_a_prewarmed_session_that_never_got_a_message_is_not_resumed(settings):
    # Claude writes a session down only once it has a message: resuming a warm-up's id fails with "No conversation found"
    fake = FakeClaude()
    s = session(fake, settings)
    assert s.warm()["started"]
    first_id = fake.procs[0].session_id
    s.stop()  # the core restarts, or an origin change restarts the process, before any message
    assert s.warm() == {"started": True, "resumed": False}
    assert "--resume" not in fake.procs[1].cmd and fake.procs[1].session_id != "" and first_id
    assert s.turn("hi").ok


def test_a_session_that_timed_out_on_its_first_message_is_resumed(settings):
    answers = iter([None, "back"])

    def script(proc, text):
        a = next(answers)
        if a:
            proc.answer(a)
    fake = FakeClaude(script)
    s = session(fake, settings)
    assert s.turn("hang", timeout=0.3).kind == "timeout"  # Claude had the message: the session exists now
    res = s.turn("again")
    assert res.ok and res.resumed and chat_store.get_chat("telegram:1")["session_turns"] == 2


def test_no_conversation_found_in_a_result_starts_a_fresh_session_and_answers(settings):
    def script(proc, text):
        if proc.resume == "ghost":
            proc.emit(ev_result(proc.session_id, "", is_error=True, subtype="error_during_execution",
                                errors=["No conversation found with session ID: ghost"]))
        else:
            proc.answer("fresh answer")
    fake = FakeClaude(script)
    fake.known_sessions.add("ghost")
    chat_store.set_session("telegram:1", "ghost", turns=3)
    s = session(fake, settings, transcript=lambda chat: "User: x\nWaggle: y")
    res = s.turn("hello")
    assert res.ok and res.text == "fresh answer" and res.retried and res.fresh_session
    assert "--resume" not in fake.procs[-1].cmd and "<earlier_conversation>" in fake.procs[-1].inputs[0]
    assert chat_store.get_chat("telegram:1")["claude_session"] != "ghost"


def test_thinking_is_off_for_chat_replies_unless_asked(settings):
    import json
    fake = FakeClaude()
    session(fake, settings).turn("hi")
    cfg = json.loads(fake.procs[0].arg("--settings"))
    assert cfg["alwaysThinkingEnabled"] is False and "PreToolUse" in cfg["hooks"]
    settings.chat_thinking = True
    fake2 = FakeClaude()
    session(fake2, settings, chat="telegram:2").turn("hi")
    assert "alwaysThinkingEnabled" not in json.loads(fake2.procs[0].arg("--settings"))


def test_a_new_day_starts_a_fresh_session_seeded_with_the_transcript(settings):
    from waggle import chat_session
    fake = FakeClaude()
    s = session(fake, settings, transcript=lambda chat: "User: x\nWaggle: y")
    chat_store.set_session("telegram:1", "old", turns=3)
    fake.known_sessions.add("old")
    chat_session.set_session_day("telegram:1", "2000-01-01")  # yesterday, as far as this session knows
    res = s.turn("good morning")
    assert res.fresh_session and "--resume" not in fake.procs[0].cmd
    assert "<earlier_conversation>" in fake.procs[0].inputs[0]
    assert chat_session.session_day("telegram:1") == chat_session.day_key()


def test_the_same_day_resumes(settings):
    from waggle import chat_session
    fake = FakeClaude()
    s = session(fake, settings)
    chat_store.set_session("telegram:1", "old", turns=3)
    fake.known_sessions.add("old")
    chat_session.set_session_day("telegram:1", chat_session.day_key())
    res = s.turn("hi")
    assert res.resumed and fake.procs[0].arg("--resume") == "old"


def test_a_warm_process_from_yesterday_is_restarted_fresh(settings):
    from waggle import chat_session
    fake = FakeClaude()
    s = session(fake, settings, transcript=lambda chat: "User: x\nWaggle: y")
    assert s.turn("evening").ok
    chat_session.set_session_day("telegram:1", "2000-01-01")
    res = s.turn("next morning")
    assert res.ok and len(fake.procs) == 2 and "--resume" not in fake.procs[1].cmd


def test_day_key_turns_at_four_am():
    import datetime as dt
    from waggle import chat_session
    t = dt.datetime(2026, 11, 21, 3, 59).timestamp()
    assert chat_session.day_key(t) == "2026-11-20"
    assert chat_session.day_key(t + 120) == "2026-11-21"
