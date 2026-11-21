"""The reply is visible while the turn runs: real captured partial events (tests/fixtures/stream_*.json) are replayed
by the fake claude process with their delays. Text that streams is posted on its first words and edited as it
grows; text that arrives at once is delivered at once (never held back to look like streaming); while a tool runs
a status line shows progress and the answer replaces it in the same message. Two shapes are replayed because the
real CLI produces both:
- stream_plain_haiku.json: a 160-word answer streamed over 0.7 s after the first word;
- stream_burst_sonnet.json: the final answer of a turn with a tool call, all 46 deltas within 4 ms."""
import json
import threading
import time
from pathlib import Path

import pytest
from engine_fakes import (FakeBotApi, FakeClaude, FakeClock, FakeDelivery, ev_init, ev_result, ev_text, ev_tool)

from waggle import chat_engine
from waggle.chat_session import ChatSession
from waggle.core import store
from waggle.delivery import BotApiDelivery, plain_text

FIX = Path(__file__).parent / "fixtures"
INTERVAL = 0.1


@pytest.fixture(autouse=True)
def _close_store():
    yield
    store.close_all()


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))["deltas"]


def replay(deltas, lead=0.0, tool=False, done=None, stretch=1.0):
    """A claude script: `lead` seconds of silence (the model thinking, a tool running), then the captured deltas with
    their original gaps, then the result. done["t"] is the time the result was emitted."""
    def script(proc, text):
        sid, full = proc.session_id, "".join(d[1] for d in deltas)

        def go():
            proc.emit(ev_init(sid))
            if tool:
                proc.emit(ev_tool(sid, "Bash", {"command": "echo hi"}, "hi"))
            time.sleep(lead)
            proc.emit({"type": "stream_event", "session_id": sid, "parent_tool_use_id": None,
                       "event": {"type": "message_start", "message": {"id": "msg_r", "model": "claude-haiku-5-5"}}})
            proc.emit({"type": "stream_event", "session_id": sid, "parent_tool_use_id": None,
                       "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}})
            prev = 0.0
            for t, piece in deltas:
                time.sleep(max(0.0, t - prev) * stretch)
                prev = t
                proc.emit({"type": "stream_event", "session_id": sid, "parent_tool_use_id": None,
                           "event": {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": piece}}})
            proc.emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
                       "message": {"id": "msg_r", "role": "assistant", "content": [{"type": "text", "text": full}]}})
            if done is not None:
                done["t"] = time.monotonic()
            proc.emit(ev_result(sid, full))
        threading.Thread(target=go, daemon=True).start()
    return script


def run(settings, script, delivery=None, **kw):
    settings.edit_interval_s = INTERVAL
    fake = FakeClaude(script)
    sess = ChatSession("telegram:5", settings=settings, popen=fake, claude="claude")
    d = delivery or FakeDelivery()
    rec = chat_engine.run_turn("telegram:5", "tell me", d, platform="telegram", context="", settings=settings,
                               session=sess, **kw)
    return rec, d


def test_a_streamed_answer_is_posted_on_its_first_words_and_grows_by_edits(settings):
    deltas = load("stream_plain_haiku.json")
    full = plain_text("".join(d[1] for d in deltas))
    done = {}
    rec, d = run(settings, replay(deltas, lead=0.3, done=done, stretch=3.0))  # the 0.7 s capture over 2 s
    kinds = [k for k, _, _ in d.log]
    assert rec.delivered and kinds[0] == "post" and kinds.count("post") == 1
    assert d.log[0][1] < done["t"], "the first post must come before the answer is complete"
    assert kinds.count("edit") - 1 >= 3, kinds  # at least three edits before the final one
    sizes = [len(t) for _, _, t in d.log]
    assert sizes == sorted(sizes) and 80 <= sizes[0] < len(full) and d.messages == [full]
    assert d.log[-1][1] - done["t"] < 0.15, "the final text lands right after the last words, no interval wait"
    assert rec.ttfw_s is not None and rec.ttfw_s < rec.total_s - 0.3, (rec.ttfw_s, rec.total_s)
    assert rec.first_visible_s == rec.ttfw_s  # no tool: the first post is the first words
    assert all(b - a >= INTERVAL * 0.9 for (_, a, _), (_, b, _) in list(zip(d.log, d.log[1:]))[:-1]), d.log


def test_a_burst_after_a_tool_call_shows_a_status_early_then_the_whole_answer_in_one_edit(settings):
    deltas = load("stream_burst_sonnet.json")
    assert deltas[-1][0] < 0.05  # the capture: everything within a few milliseconds
    full = plain_text("".join(d[1] for d in deltas))
    done = {}
    rec, d = run(settings, replay(deltas, lead=0.6, tool=True, done=done))
    kinds = [k for k, _, _ in d.log]
    assert rec.delivered and kinds == ["post", "edit"], kinds
    assert d.log[0][2] == "Running a command…" and d.log[0][1] < done["t"] - 0.4  # early, while the model works
    assert d.messages == [full] and d.log[1][2] == full
    assert d.log[1][1] - done["t"] < 0.15, "no delay added to text that has arrived"
    assert rec.steps and rec.steps[0]["tool"] == "Bash" and rec.steps[0]["ok"] is True
    assert rec.first_visible_s < rec.ttfw_s - 0.4 and rec.ttfw_s <= rec.total_s  # status first, words later
    assert rec.ttfw_s >= 0.6  # ttfw is the first real words, not the status line


def test_a_burst_without_a_tool_is_one_post(settings):
    deltas = load("stream_burst_sonnet.json")
    rec, d = run(settings, replay(deltas, lead=0.1))
    assert [k for k, _, _ in d.log] == ["post"] and d.messages == [plain_text("".join(x[1] for x in deltas))]
    assert rec.ttfw_s == rec.first_visible_s


def test_a_one_line_answer_is_sent_once(settings):
    deltas = [[0.0, "Paris is the capital"], [0.02, " of France."]]
    rec, d = run(settings, replay(deltas, lead=0.1))
    assert rec.delivered and [k for k, _, _ in d.log] == ["post"] and d.messages == ["Paris is the capital of France."]
    assert rec.ttfw_s is not None and rec.ttfw_s <= rec.total_s


def test_a_slow_stream_is_posted_then_edited_one_interval_apart_and_ends_complete(settings):
    # 60 characters every 0.25 s: slower than the edit rate, so each edit shows everything that has arrived
    deltas = [[i * 0.25, ("word " * 12)] for i in range(8)]
    full = plain_text("".join(d[1] for d in deltas))
    done = {}
    rec, d = run(settings, replay(deltas, lead=0.05, done=done))
    kinds = [k for k, _, _ in d.log]
    assert kinds[0] == "post" and kinds.count("edit") >= 4 and d.messages == [full]
    assert d.log[-1][1] - done["t"] < 0.15  # the final text lands right after the last words
    assert all(b - a >= INTERVAL * 0.9 for (_, a, _), (_, b, _) in list(zip(d.log, d.log[1:]))[:-1])
    assert all(len(t) <= len(full) for _, _, t in d.log)  # no edit ever showed text that had not arrived


def test_edits_of_one_message_are_at_least_a_second_apart_on_the_real_transport(settings):
    deltas = load("stream_burst_sonnet.json")
    clock = FakeClock()
    api = FakeBotApi(clock=clock)
    d = BotApiDelivery("5", api, clock=clock, sleep=clock.sleep)
    rec, _ = run(settings, replay(deltas, lead=0.4, tool=True), delivery=d)
    stamps = [c[1] for c in api.calls if c[0] in ("send_message", "edit_message_text")]
    assert rec.delivered and len(stamps) == 2  # the status line, then the answer in the same message
    assert all(b - a >= 1.0 - 1e-9 for a, b in zip(stamps, stamps[1:])), stamps
    assert api.messages[d.message_ids[0]] == plain_text("".join(x[1] for x in deltas))
