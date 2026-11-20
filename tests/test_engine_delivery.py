"""Delivery: StreamWriter's throttled edits and final text, splitting, and BotApiDelivery over a fake Bot API."""
import sys
import time
import types

import pytest
from engine_fakes import FakeBotApi, FakeClock, FakeDelivery, TelegramError

from waggle.delivery import BotApiDelivery, CollectDelivery, StreamWriter, plain_text, split_long


def test_split_long_cuts_at_boundaries_within_the_limit():
    text = ("A sentence that goes on. " * 40 + "\n\n") * 5
    chunks = split_long(text, 300)
    assert all(len(c) <= 300 for c in chunks) and len(chunks) > 3
    assert "".join(chunks).replace(" ", "").replace("\n", "") == text.replace(" ", "").replace("\n", "")
    assert split_long("short", 300) == ["short"]
    assert all(len(c) <= 50 for c in split_long("x" * 175, 50))  # no boundary at all: hard cut


def test_plain_text_for_chat_apps():
    assert plain_text("## Plan\n**Bold** and `code`\n- item") == "Plan\nBold and code\n• item"
    assert plain_text(r"$$\frac{a}{b} \times 2$$") == "a/b × 2"
    assert plain_text("costs $5 and $10") == "costs $5 and $10"


def test_streamed_edits_are_throttled_and_the_final_text_lands_once():
    d = FakeDelivery()
    w = StreamWriter(d, interval=0.25, tick=0.02)
    t0 = time.monotonic()
    text = ""
    while time.monotonic() - t0 < 1.2:
        text += "word "
        w.update(text)
        time.sleep(0.01)
    out = w.finish(text + "end.")
    pushes = [t for kind, t, _ in d.log]
    gaps = [b - a for a, b in zip(pushes, pushes[1:])]
    assert out["delivered"] and out["messages"] == 1 and out["message_ids"] == [100]
    assert len(pushes) <= 1.2 / 0.25 + 2 and all(g >= 0.24 for g in gaps), gaps
    assert d.messages == [(text + "end.").strip()] and [k for k, _, _ in d.log].count("post") == 1
    assert out["first_shown_at"] is not None


def test_final_text_is_sent_even_when_unchanged_streaming_never_ran():
    d = FakeDelivery()
    out = StreamWriter(d, interval=0.2).finish("Only the final.")
    assert out["delivered"] and d.messages == ["Only the final."] and len(d.log) == 1


def test_failed_final_edit_becomes_a_new_message():
    d = FakeDelivery()
    w = StreamWriter(d, interval=0.05, tick=0.01)
    w.update("partial answer")
    time.sleep(0.15)
    d.fail_edit = True
    out = w.finish("partial answer, now complete")
    assert out["delivered"] and d.messages[-1] == "partial answer, now complete"


def test_post_failure_is_reported_not_raised():
    d = FakeDelivery(fail_post=True)
    out = StreamWriter(d, stream=False).finish("hello")
    assert not out["delivered"] and out["error"] and out["message_ids"] == []

    class Boom(FakeDelivery):
        def post(self, text):
            raise ConnectionError("network down")
    out = StreamWriter(Boom(), stream=False).finish("hello")
    assert not out["delivered"] and "network down" in out["error"]


def test_long_reply_continues_in_new_messages():
    d = FakeDelivery(max_len=500)
    text = "\n\n".join(f"Paragraph {i}: " + "lorem ipsum " * 12 for i in range(12))
    out = StreamWriter(d, stream=False).finish(text)
    assert out["delivered"] and out["messages"] == len(d.messages) > 3
    assert all(len(m) <= 500 for m in d.messages)


def test_no_streaming_when_the_transport_cannot_edit():
    d = FakeDelivery(can_edit=False)
    w = StreamWriter(d, interval=0.01, tick=0.005)
    w.update("a")
    w.update("ab")
    time.sleep(0.05)
    w.finish("abc")
    assert [k for k, _, _ in d.log] == ["post"] and d.messages == ["abc"]


def test_collect_delivery_keeps_the_reply():
    c = CollectDelivery()
    StreamWriter(c, interval=0.01, tick=0.005).finish("kept")
    assert c.text == "kept"


# ---- BotApiDelivery ------------------------------------------------------------------------------------------

def make(clock=None, **kw):
    clock = clock or FakeClock()
    api = FakeBotApi(clock=clock)
    return BotApiDelivery("42", api, clock=clock, sleep=clock.sleep, **kw), api, clock


def test_post_and_edit_respect_the_minimum_gap_per_message():
    d, api, clock = make()
    mid = d.post("first")
    assert mid == 501 and api.messages[mid] == "first"
    assert d.edit(mid, "first, longer")
    assert d.edit(mid, "first, longer still")
    edits = [c[1] for c in api.calls if c[0] == "edit_message_text"]
    posted = [c[1] for c in api.calls if c[0] == "send_message"][0]
    assert edits[0] - posted >= 1.0 and edits[1] - edits[0] >= 1.0
    other = d.post("second message")
    t = clock()
    d.edit(other, "second, edited")  # its own gap, not the first message's
    assert clock() - t >= 0.999 and d.message_ids == [501, 502]


def test_rate_limited_waits_retry_after_once_then_succeeds():
    d, api, clock = make()
    mid = d.post("x")
    clock.t += 5
    api.fail["edit_message_text"] = [TelegramError("Too Many Requests", "rate_limited", retry_after=3)]
    assert d.edit(mid, "y") and api.messages[mid] == "y" and 3 in clock.slept
    api.fail["send_message"] = [TelegramError("Bad Gateway", "retryable")]
    assert d.post("z") is not None


def test_permanent_edit_failure_returns_false_and_not_modified_counts_as_done():
    d, api, clock = make()
    mid = d.post("x")
    api.fail["edit_message_text"] = [TelegramError("message to edit not found", "permanent")]
    assert d.edit(mid, "y") is False
    api.fail["edit_message_text"] = [TelegramError("Bad Request: message is not modified", "permanent")]
    assert d.edit(mid, "x") is True
    api.fail["send_message"] = [TelegramError("Unauthorized", "auth")]
    with pytest.raises(TelegramError):
        d.post("nope")


def test_writer_over_bot_api_continues_past_4096_and_always_sends_the_final_edit():
    clock = FakeClock()
    d, api, _ = make(clock=clock)
    w = StreamWriter(d, interval=1.2, clock=clock, sleep=clock.sleep, stream=False)
    text = "\n\n".join(f"Section {i}. " + "words and more words " * 20 for i in range(25))
    out = w.finish(text)
    sent = [api.messages[i] for i in out["message_ids"]]
    assert len(text) > 9000 and out["delivered"] and len(sent) >= 3
    assert all(len(m) <= 4096 for m in sent) and out["message_ids"] == d.message_ids


def test_final_edit_after_a_permanent_failure_is_reposted():
    clock = FakeClock()
    d, api, _ = make(clock=clock)
    w = StreamWriter(d, interval=1.2, clock=clock, sleep=clock.sleep, stream=False)
    w.handles, w.shown = [d.post("draft")], ["draft"]
    api.fail["edit_message_text"] = [TelegramError("message to edit not found", "permanent")]
    out = w.finish("the final answer")
    assert out["delivered"] and api.messages[out["message_ids"][-1]] == "the final answer"


def test_reply_to_only_on_the_first_message_typing_files_and_voice():
    d, api, _ = make(reply_to=7)
    d.post("a")
    d.post("b")
    assert [c[4] for c in api.calls if c[0] == "send_message"] == [7, None]
    api.fail["send_chat_action"] = [TelegramError("down", "retryable")]
    assert d.typing() is None  # best effort: never raises
    assert d.send_file("report.pdf", caption="here") and d.send_voice("reply.ogg", duration=4)
    assert ("send_voice", api.calls[-1][1], "42", "reply.ogg", None, 4) == api.calls[-1]


def test_bot_api_is_imported_lazily(monkeypatch):
    built = []
    fake_mod = types.ModuleType("waggle.core.telegram")

    class BotApi(FakeBotApi):
        def __init__(self):
            super().__init__()
            built.append(self)
    fake_mod.BotApi = BotApi
    monkeypatch.setitem(sys.modules, "waggle.core.telegram", fake_mod)
    d = BotApiDelivery("9")
    assert built == []
    d.post("hello")
    assert len(built) == 1 and built[0].messages


def test_a_short_answer_that_completes_within_the_hold_is_posted_once_without_an_edit():
    d = FakeDelivery()
    w = StreamWriter(d, interval=1.0, tick=0.01, first_hold_s=0.3)
    w.update("Four.")
    time.sleep(0.1)
    t = time.monotonic()
    out = w.finish("Four.")
    assert out["delivered"] and [k for k, _, _ in d.log] == ["post"] and time.monotonic() - t < 0.6


def test_the_first_post_waits_at_most_the_hold():
    d = FakeDelivery()
    w = StreamWriter(d, interval=1.0, tick=0.01, first_hold_s=0.15)
    w.update("Short start")
    time.sleep(0.3)
    assert [k for k, _, _ in d.log] == ["post"]
    w2 = StreamWriter(FakeDelivery(), interval=1.0, tick=0.01, first_hold_s=5, first_min_chars=10)
    w2.update("Long enough already")
    time.sleep(0.1)
    assert w2.d.log and w2.d.log[0][0] == "post"
    w.finish("Short start, done.")
    w2.finish("Long enough already.")


# ---- pacing: a long answer that is ready at once is still revealed step by step ------------------------------

def _long_text(n=900):
    words = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu " * 40).split()
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > n:
            break
        out += w + " "
    return out.strip()


def test_a_long_answer_that_is_ready_at_once_is_revealed_in_steps_one_interval_apart():
    d = FakeDelivery()
    text = _long_text(900)
    w = StreamWriter(d, interval=0.15, tick=0.01)
    w.update(text)  # the whole text arrives in one burst
    time.sleep(0.05)
    out = w.finish(text)
    kinds = [k for k, _, _ in d.log]
    sizes = [len(t) for _, _, t in d.log]
    stamps = [t for _, t, _ in d.log]
    assert out["delivered"] and kinds[0] == "post" and kinds.count("post") == 1
    assert kinds.count("edit") - 1 >= 3 and sizes == sorted(sizes) and sizes[0] <= 200, (kinds, sizes)
    assert d.messages == [text] and all(b - a >= 0.14 for a, b in zip(stamps, stamps[1:]))
    assert out["edits"] == kinds.count("edit") and out["first_shown_at"] is not None
    assert all(t.endswith(tuple("abcdefghijklmnopqrstuvwxyz")) for _, _, t in d.log)  # cut at a word end


def test_the_reveal_of_a_huge_answer_is_capped_at_pace_steps():
    d = FakeDelivery(max_len=10000)
    text = _long_text(3000)
    w = StreamWriter(d, interval=0.03, tick=0.005, pace_steps=4)
    w.update(text)
    w.finish(text)
    assert len(d.log) <= 4 + 3 and d.messages == [text]  # one post, at most pace_steps steps and the final edit


def test_pacing_can_be_turned_off_and_short_answers_are_never_split():
    text = _long_text(900)
    d = FakeDelivery()
    w = StreamWriter(d, interval=0.05, tick=0.005, pace_chars=0)
    w.update(text)
    time.sleep(0.1)
    w.finish(text)
    assert [k for k, _, _ in d.log] == ["post"] and d.messages == [text]
    d2 = FakeDelivery()
    short = _long_text(380)  # under pace_min_chars
    w2 = StreamWriter(d2, interval=0.05, tick=0.005)
    w2.update(short)
    time.sleep(0.1)
    w2.finish(short)
    assert [k for k, _, _ in d2.log] == ["post"] and d2.messages == [short]


def test_a_reply_that_was_never_streamed_is_posted_once_whole():
    d = FakeDelivery()
    text = _long_text(900)
    StreamWriter(d, interval=0.05).finish(text)  # a failure notice, an approval answer: nothing was streamed
    assert [k for k, _, _ in d.log] == ["post"] and d.messages == [text]
