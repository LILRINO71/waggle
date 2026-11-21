"""run_turn: streaming, exactly-once final delivery, honest failures, the outbox, and the full turn record."""
import json
import time

import pytest
from engine_fakes import FakeClaude, FakeDelivery, ev_result, ev_text, ev_tool

from waggle import chat_engine, quota, traces
from waggle.chat_session import ChatSession
from waggle.core import outbox, store


@pytest.fixture(autouse=True)
def _close_store():
    chat_engine._available.clear()
    yield
    store.close_all()


@pytest.fixture
def no_local(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the local model must not answer by default")
    monkeypatch.setattr(chat_engine, "local_answer", boom)


def turn(settings, script=None, delivery=None, chat="telegram:5", **kw):
    if settings.edit_interval_s > 0.5:
        settings.edit_interval_s = 0.05  # the final edit waits out the interval; keep the tests fast
    fake = FakeClaude(script)
    sess = ChatSession(chat, settings=settings, popen=fake, claude="claude")
    d = delivery or FakeDelivery()
    kw.setdefault("platform", "telegram")
    rec = chat_engine.run_turn(chat, "what's up?", d, context="", settings=settings, session=sess, **kw)
    return rec, d, fake


def row(rec):
    r = traces.get(rec.trace_id)
    for k in ("message_ids", "outcome_why", "claims"):
        r[k] = json.loads(r[k]) if r[k] else None
    return r


def test_streamed_turn_is_recorded_in_full(settings, no_local):
    settings.edit_interval_s = 0.2
    text = "The answer, written in several pieces so that the stream has something to show."

    def script(proc, msg):
        proc.answer(text, pieces=8, delay=0.08)
    rec, d, fake = turn(settings, script)
    edits = [t for kind, t, _ in d.log]
    assert rec.reply == text and d.messages == [text] and [k for k, _, _ in d.log].count("post") == 1
    assert len(d.log) >= 2 and all(b - a >= 0.19 for a, b in list(zip(edits, edits[1:]))[:-1])  # the final edit is not held
    assert d.typed == 1
    r = row(rec)
    assert r["answered_by"] == "claude-haiku-5-5" and r["turn_outcome"] == "ok" and r["outcome_why"] is None
    assert r["delivered"] == 1 and r["delivery"] == "fake" and r["message_ids"] == [100]
    assert 0 < r["ttfw_s"] <= r["total_s"] and r["origin"] == "user" and r["turn_id"] == rec.turn_id
    assert r["version"].startswith("waggle ") and r["ok"] == 1 and r["chat"] == "telegram:5"
    assert outbox.list() == []


def test_claude_failure_is_an_honest_message_never_the_local_model(settings, no_local):
    def script(proc, msg):
        proc.emit(ev_result(proc.session_id, "Internal server error", is_error=True))
    rec, d, _ = turn(settings, script)
    assert rec.answered_by == "none" and rec.failure_notice and rec.outcome == "failed"
    assert d.messages == [rec.reply] and "couldn't answer" in rec.reply and "local" not in rec.reply.lower()
    r = row(rec)
    assert r["answered_by"] == "none" and r["turn_outcome"] == "failed" and "error" in r["fallback_reason"]
    assert r["claims"] is None and r["ok"] == 0


def test_missing_cli_is_an_honest_failure(settings, no_local):
    sess = ChatSession("telegram:5", settings=settings, popen=FakeClaude())  # no claude found in tests
    d = FakeDelivery()
    rec = chat_engine.run_turn("telegram:5", "hi", d, context="", settings=settings, session=sess)
    assert rec.outcome == "failed" and "isn't installed" in rec.reply and rec.fallback_reason.startswith("missing")


def test_quota_block_skips_claude(settings, monkeypatch, no_local):
    monkeypatch.setattr(quota, "can_escalate", lambda model, s=None: (False, "Claude limit reached"))
    rec, d, fake = turn(settings)
    assert fake.procs == [] and rec.outcome == "failed" and "usage limit" in rec.reply


def test_local_model_only_when_asked_and_then_labelled(settings):
    def script(proc, msg):
        proc.emit(ev_result(proc.session_id, "boom", is_error=True))
    rec, d, _ = turn(settings, script, local=lambda chat, text, ctx, s: "Local guess.")
    assert rec.answered_by == "local" and rec.reply.startswith("Local guess.") and "small local model" in rec.reply
    assert rec.outcome == "degraded" and "answered by the local model" in rec.outcome_why


def test_tool_error_with_a_delivered_answer_is_degraded_and_the_tool_result_is_kept(settings, no_local):
    secret = "ghp_" + "b" * 36

    def script(proc, msg):
        sid = proc.session_id
        proc.emit(ev_tool(sid, "Bash", {"command": "gh auth status"}, f"not logged in; token {secret} " + "y" * 900,
                          is_error=True))
        proc.emit(ev_text(sid, "From memory: your repo is fine.", msg_id="m2"))
        proc.emit(ev_result(sid, "From memory: your repo is fine."))
    rec, d, _ = turn(settings, script)
    assert rec.delivered and rec.outcome == "degraded" and "tool error: Bash" in rec.outcome_why
    r = row(rec)
    step = r["steps"][0]
    assert r["turn_outcome"] == "degraded" and step["tool"] == "Bash" and step["ok"] == 0
    assert len(step["result"]) == traces.STEP_RESULT_CAP and secret not in step["result"] and "not logged in" in step["result"]


def test_refused_tool_degrades_the_turn_and_says_why(settings, no_local):
    def script(proc, msg):
        sid = proc.session_id
        proc.emit(ev_tool(sid, "Write", {"file_path": "x"}, "NEEDS_APPROVAL code ABCD", is_error=True))
        proc.answer("I need your OK first: reply yes.")
    rec, _, _ = turn(settings, script)
    assert rec.outcome == "degraded" and rec.steps[0]["denied"]
    assert any(w.startswith("needs approval: Write") for w in rec.outcome_why)


def test_undelivered_reply_goes_to_the_outbox_once(settings, no_local):
    rec, d, _ = turn(settings, delivery=FakeDelivery(fail_post=True))
    assert not rec.delivered and rec.delivery == "outbox" and rec.outbox_id is not None
    item = outbox.get(rec.outbox_id)
    assert item["idem_key"] == f"turn:{rec.turn_id}" and item["body"] == "Hello from Claude." and item["chat"] == "telegram:5"
    assert item["meta"]["kind"] == "reply" and item["origin"] == "turn"
    assert chat_engine.queue_reply(rec) == rec.outbox_id and len(outbox.list()) == 1
    assert rec.outcome == "degraded" and "delivery deferred to the outbox" in rec.outcome_why
    assert row(rec)["delivered"] == 0


def test_reply_neither_delivered_nor_queued_is_failed(settings, monkeypatch, no_local):
    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(outbox, "enqueue", broken)
    rec, _, _ = turn(settings, delivery=FakeDelivery(fail_post=True))
    assert rec.outbox_id is None and rec.delivery == "none" and rec.outcome == "failed"
    assert "disk full" in rec.delivery_error


def test_cut_off_answer_keeps_the_text_and_says_so(settings, no_local):
    def script(proc, msg):
        proc.emit(ev_text(proc.session_id, "Half of it")[:4])
        proc.die(1, "killed")
    rec, d, _ = turn(settings, script)
    assert rec.partial and rec.reply.startswith("Half") and "cut off" in rec.reply
    assert rec.answered_by == "claude-haiku-5-5" and rec.outcome == "degraded"


def test_claims_and_the_session_details_are_recorded(settings, no_local):
    def script(proc, msg):
        sid = proc.session_id
        proc.emit(ev_tool(sid, "Write", {"file_path": "C:\\work\\out\\plan.md", "content": "x"}, "File written"))
        proc.answer("Done: wrote C:\\work\\out\\plan.md and committed abc1234 (see https://example.com/pr/7).")
    rec, _, _ = turn(settings, script)
    r = row(rec)
    assert "C:\\work\\out\\plan.md" in r["claims"]["files"] and r["claims"]["commits"] == ["abc1234"]
    assert r["claims"]["urls"] == ["https://example.com/pr/7)"[:-1]]
    assert json.loads(r["extra"])["session"]["fresh"] is True


def test_turn_record_survives_a_session_layer_bug(settings, no_local):
    class Broken:
        def turn(self, *a, **k):
            raise KeyError("bug")
    d = FakeDelivery()
    rec = chat_engine.run_turn("telegram:5", "hi", d, context="", settings=settings, session=Broken())
    assert rec.outcome == "failed" and d.messages == [rec.reply] and "KeyError" in rec.fallback_reason


def test_origin_marks_non_user_turns_and_keeps_them_out_of_learning(settings, no_local, monkeypatch):
    rec_cli, _, _ = turn(settings, chat="cli:1", platform="cli")
    rec_tg, _, _ = turn(settings, chat="telegram:5")
    monkeypatch.setenv("WAGGLE_ORIGIN", "bench")
    rec_bench, _, _ = turn(settings, chat="telegram:6")
    assert (rec_cli.origin, rec_tg.origin, rec_bench.origin) == ("test", "user", "bench")
    learnable = {r["id"] for r in traces.all_traces()}
    assert learnable == {rec_tg.trace_id}
    assert len(traces.all_traces(include_non_user=True)) == 3
    assert [r["id"] for r in traces.search("what's up")] == [rec_tg.trace_id]
    assert traces.stats()["turns"] == 3


def test_origin_for():
    env = {}
    assert chat_engine.origin_for("telegram", env) == "user"
    assert chat_engine.origin_for("cli", env) == "test"
    assert chat_engine.origin_for("", env) == "test"
    assert chat_engine.origin_for("subagent", env) == "agent"
    assert chat_engine.origin_for("bench", env) == "bench"
    assert chat_engine.origin_for("telegram", {"WAGGLE_AGENT": "chat"}) == "agent"
    assert chat_engine.origin_for("telegram", {"WAGGLE_ORIGIN": "canary"}) == "canary"
    assert chat_engine.origin_for("telegram", {"WAGGLE_ORIGIN": "nonsense"}) == "user"
    with pytest.raises(ValueError):
        traces.record("x", origin="robot")


def test_outcome_rule():
    o = traces.turn_outcome
    assert o(answered_by="claude-x", delivered=True) == ("ok", [])
    assert o(answered_by="none", delivered=True, failure_notice=True)[0] == "failed"
    assert o(answered_by="claude-x", delivered=False, queued=False)[0] == "failed"
    assert o(answered_by="claude-x", delivered=False, queued=True)[0] == "degraded"
    assert o(answered_by="claude-x", delivered=True, steps=[{"tool": "Read", "ok": None}])[0] == "degraded"
    assert o(answered_by="claude-x", delivered=True, steps=[{"tool": "Read", "ok": False, "denied": True}])[0] == "degraded"
    assert o(answered_by="local", delivered=True)[0] == "degraded"
    assert o(answered_by="claude-x", delivered=True, partial=True) == ("degraded", ["answer cut off"])


def test_engine_stats_counts_outcomes_and_lost(settings, no_local):
    turn(settings)
    rec, _, _ = turn(settings, delivery=FakeDelivery(fail_post=True))
    stats = traces.engine_stats(platform="telegram")
    assert stats["turns"] == 2 and stats["claude_share"] == 1.0 and stats["pending"] == 1 and stats["lost"] == 0
    assert stats["outcomes"] == {"ok": 1, "degraded": 1} and stats["ttfw_p50_s"] is not None
    outbox.claim_due()
    outbox.mark_failed(rec.outbox_id, "gone", permanent=True)
    assert traces.engine_stats(platform="telegram")["lost"] == 1


def test_recent_background_results_reach_the_next_context(settings):
    from waggle import chat_store
    oid = outbox.enqueue("text", "Background job done: the report", chat="telegram:5",
                         meta={"kind": "background"}, now=time.time())
    outbox.claim_due()
    outbox.mark_sent(oid, "777")
    assert "the report" in chat_engine.recent_deliveries("telegram:5", since=time.time() - 60)
    assert chat_engine.recent_deliveries("telegram:5", since=time.time() + 60) == ""
    prev = chat_store.touch_chat("telegram:5", "telegram", user=True)
    assert prev == 0.0 and chat_store.touch_chat("telegram:5", user=True) > 0


def test_slow_notes_search_is_left_out_of_the_context_not_waited_for(settings, monkeypatch):
    from waggle import memory
    monkeypatch.setattr(chat_engine, "CONTEXT_BUDGET_S", 0.2)
    monkeypatch.setattr(memory, "retrieval_block", lambda text, *a, **k: time.sleep(3) or "A NOTE")
    t = time.time()
    ctx = chat_engine.build_context("telegram:5", "hello", False, settings)
    assert time.time() - t < 1.5 and "A NOTE" not in ctx and "<waggle_context>" in ctx
    monkeypatch.setattr(memory, "retrieval_block", lambda text, *a, **k: "A NOTE")
    assert "A NOTE" in chat_engine.build_context("telegram:5", "hello", False, settings)


def test_the_usage_check_is_made_once_per_half_minute(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(quota, "can_escalate", lambda m, s: calls.append(m) or (True, ""))
    assert chat_engine.claude_available(settings) == (True, "") and chat_engine.claude_available(settings) == (True, "")
    assert len(calls) == 1
    monkeypatch.setattr(quota, "can_escalate", lambda m, s: (False, "budget"))
    chat_engine._available.clear()
    assert chat_engine.claude_available(settings) == (False, "limit: budget")
