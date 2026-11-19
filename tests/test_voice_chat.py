"""Telegram voice notes, conversation memory across restarts, and chat model selection (deep vs hardest)."""
import importlib.util
import time
from pathlib import Path

import pytest

from waggle import claude_cli, router, traces, voice_turn

NOTE = '"what\'s the next thing on the bio bus robot for ethics robotics"\n[User sent audio: C:\\cache\\audio_1.ogg]'


def _plugin(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "waggle_plugin_voice", Path(__file__).parents[1] / "hermes_plugin" / "waggle" / "__init__.py")
    plug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plug)
    monkeypatch.setattr(plug.learn, "learn_async", lambda *a: None)
    monkeypatch.setattr(plug, "_refresh_hq", lambda: None)
    monkeypatch.setattr(plug, "_reindex_soon", lambda: None)
    monkeypatch.setattr(plug.memory, "context_block", lambda text: "<notes/>")
    monkeypatch.setattr(plug.projects, "context_for", lambda text: "")
    monkeypatch.setattr(plug.jobs, "resume_paused", lambda: None)
    return plug


# ---- voice notes -----------------------------------------------------------------------------------

def test_voice_note_becomes_what_was_said_with_known_mishearings_fixed():
    text, kind = voice_turn.parse(NOTE, {r"\bbio ?bus\b": "BIOBUZZ"})
    assert kind == "voice"
    assert text == "what's the next thing on the BIOBUZZ robot for FTC robotics"
    assert "User sent audio" not in text and "audio_1.ogg" not in text


def test_plain_text_is_untouched_and_a_failed_transcription_is_flagged():
    assert voice_turn.parse("send me the pdf", {}) == ("send me the pdf", "text")
    assert voice_turn.parse("[voice message could not be transcribed: timeout]", {})[1] == "voice_failed"


def test_a_caption_and_a_bad_personal_fix_do_not_break_parsing():
    text, kind = voice_turn.parse('look at this\n"open onshape please"\n[User sent audio: x.ogg]', {"(": "x"})
    assert kind == "voice" and text == "look at this\n\nopen Onshape please"


def test_voice_turn_reaches_the_router_clean_and_marked_as_spoken(monkeypatch, settings):
    settings.voice_fixes = {r"\bbio ?bus\b": "BIOBUZZ"}
    settings.save()
    plug = _plugin(monkeypatch)
    seen = {}

    def decide(text, ctx):
        seen.update(text=text, ctx=ctx)
        return {"route": "sonnet", "reply": "Finish the transfer ramp.", "answered_by": "sonnet"}
    monkeypatch.setattr(plug.router, "decide", decide)
    out = plug._pre_llm("tg1", NOTE, "telegram")
    assert out == {"action": "block", "message": "Finish the transfer ramp."}
    assert seen["text"].startswith("what's the next thing on the BIOBUZZ robot")
    assert "<voice_message>" in seen["ctx"]
    assert traces.last("tg1")["request"].startswith("what's the next thing on the BIOBUZZ")


def test_failed_voice_note_gets_an_honest_retry_message_not_a_guess(monkeypatch):
    plug = _plugin(monkeypatch)
    monkeypatch.setattr(plug.router, "decide", lambda *a: pytest.fail("router must not run"))
    out = plug._pre_llm("tg2", "[voice message could not be transcribed: model error]", "telegram")
    assert out["action"] == "block" and "couldn't make out" in out["message"]


def test_text_turn_gets_no_voice_hint(monkeypatch):
    plug = _plugin(monkeypatch)
    seen = {}
    monkeypatch.setattr(plug.router, "decide", lambda text, ctx: seen.update(ctx=ctx) or
                        {"route": "sonnet", "reply": "ok", "answered_by": "sonnet"})
    plug._pre_llm("tg3", "what time is the meet", "telegram")
    assert "<voice_message>" not in seen["ctx"]


# ---- conversation memory ---------------------------------------------------------------------------

def test_conversation_survives_a_gateway_restart(monkeypatch):
    traces.record("which intake should we build first?", session="tg9", route="sonnet", reply="The back intake.")
    traces.record("other chat", session="other", route="sonnet", reply="unrelated")
    plug = _plugin(monkeypatch)  # a fresh module = a restarted gateway: _convo starts empty
    seen = {}
    monkeypatch.setattr(plug.router, "decide", lambda text, ctx: seen.update(ctx=ctx) or
                        {"route": "sonnet", "reply": "About 2 hours.", "answered_by": "sonnet"})
    plug._pre_llm("tg9", "how long would that take?", "telegram")
    assert "User: which intake should we build first?\nWaggle: The back intake." in seen["ctx"]
    assert "unrelated" not in seen["ctx"].split("<recent_conversation>")[1]
    assert plug._recent("tg9").endswith("User: how long would that take?\nWaggle: About 2 hours.")


def test_an_expired_approval_code_in_the_history_is_flagged_as_closed(monkeypatch):
    from waggle import policy
    traces.record("send it to my email", session="tg8", route="act",
                  reply="I need your OK for: send message [VWV4]. Reply approve VWV4.")
    plug = _plugin(monkeypatch)
    seen = {}
    monkeypatch.setattr(plug.router, "decide", lambda text, ctx: seen.update(ctx=ctx) or
                        {"route": "sonnet", "reply": "ok", "answered_by": "sonnet"})
    plug._pre_llm("tg8", "thanks", "telegram")
    assert "No approval is open for this chat right now" in seen["ctx"]
    policy.request_approval("Bash", {"command": "rm C:/tmp/x.txt"}, "deleting files", "tg8")
    plug._pre_llm("tg8", "and now?", "telegram")
    code = policy.pending("tg8")[0]["code"]
    assert f"Open approvals: {code} (deleting files)" in seen["ctx"]


def test_long_replies_are_kept_whole_enough_for_follow_ups(monkeypatch):
    plug = _plugin(monkeypatch)
    long_reply = "step " * 450  # 2250 chars: the old 1200-char cap cut the parts list off
    plug._remember_turn("s", "Waggle", long_reply)
    assert len(plug._convo["s"][0][1]) >= 2200


def test_session_turns_are_oldest_first_and_skip_stale_ones():
    traces.record("old", session="a", reply="r0")
    con = traces._db()
    con.execute("UPDATE traces SET time=? WHERE request='old'", (time.time() - 2 * 86400,))
    con.commit()
    con.close()
    traces.record("q1", session="a", reply="r1")
    traces.record("q2", session="a", reply="r2")
    assert traces.session_turns("a") == [("q1", "r1"), ("q2", "r2")]
    assert traces.session_turns("") == []


# ---- chat model selection --------------------------------------------------------------------------

def _route(route, hard=False):
    return {"json": {"route": route, "reason": "x", "reply": "", "build_prompt": "", "title": "", "hard": hard}}


def test_deep_goes_to_sonnet_at_medium_effort_and_hard_deep_to_opus(settings):
    settings.chat_model, settings.chat_deep_model = "sonnet", "opus"
    calls = []

    def asker(prompt, model=None, purpose="", effort=None, **k):
        calls.append((purpose, model, effort))
        if purpose == "route":
            return _route("deep", hard=calls[0] is calls[-1] and "prove" in prompt)
        return {"text": f"careful answer by {model}"}
    d = router.decide("derive the formula for shooter exit velocity step by step", settings=settings, asker=asker)
    assert d["route"] == "sonnet" and d["answered_by"] == "sonnet" and calls[-1] == ("deep", "sonnet", "medium")
    calls.clear()
    d = router.decide("prove this scheduling problem is NP-hard", settings=settings, asker=asker)
    assert d["answered_by"] == "opus" and calls[-1] == ("deep", "opus", "medium")


def test_a_slow_opus_falls_back_to_the_chat_model(settings):
    settings.chat_model, settings.chat_deep_model = "sonnet", "opus"
    calls = []

    def asker(prompt, model=None, purpose="", timeout=None, **k):
        calls.append((purpose, model, timeout))
        if purpose == "route":
            return _route("deep", hard=True)
        if model == "opus":
            raise claude_cli.ClaudeError("timed out", "timeout")
        return {"text": "Sonnet's answer"}
    d = router.decide("prove it", settings=settings, asker=asker)
    assert d["reply"] == "Sonnet's answer" and d["answered_by"] == "sonnet"
    assert ("deep", "opus", 150) in calls



# ---- approvals over chat ---------------------------------------------------------------------------

def test_approval_line_names_the_exact_action_and_code(settings):
    from waggle import agent, policy
    aid = policy.request_approval("Bash", {"command": "rm C:/tmp/approval-test.txt"}, "deleting files", "tg5")
    res = agent.AgentResult(text="", ok=True, approvals=policy.pending("tg5"))
    line = agent.approval_message(res)
    code = policy.pending("tg5")[0]["code"]
    assert "deleting files `rm C:/tmp/approval-test.txt`" in line and f"[{code}]" in line
    assert aid not in line
