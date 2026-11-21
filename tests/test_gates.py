"""The cheap gate in front of Claude (gates.py), the notify-or-not check (notifycheck.py) and the per-kind engine
cooldowns (cooldowns.py), with fakes only: no Claude, no Ollama, no network, a temp home."""
import datetime as dt
import json
import subprocess
import time
import types

import pytest

from waggle import (autowork, claude_cli, cooldowns, engines, events, gates, goals, initiative, notifycheck, objectives,
                    routines, vault)
from waggle.config import Settings


def at(h, m=0, day=19):
    return dt.datetime(2026, 11, day, h, m).timestamp()


@pytest.fixture
def s(settings):
    settings.quiet_hours = [23, 7]
    settings.watch_roots = []
    settings.save()
    return Settings.load()


def answer(**kw):
    return lambda: kw


# ---- thresholds, both sides -----------------------------------------------------------------------------------

@pytest.mark.parametrize("p_needed, wake, outcome", [
    (0.05, False, gates.SKIP),    # P(not needed) = 0.95: exactly the line, skips
    (0.0, False, gates.SKIP),
    (0.06, False, gates.RUN),     # 0.94: not sure enough, runs
    (0.5, False, gates.RUN),
    (0.0, True, gates.RUN),       # said wake: always runs, whatever the number
    (1.0, True, gates.RUN),
])
def test_skip_needs_wake_false_and_p_not_needed_at_least_095(s, p_needed, wake, outcome):
    v = gates.evaluate("t", answer(wake=wake, p_needed=p_needed), settings=s, now=at(12))
    assert v.outcome == outcome and v.reason


def test_a_script_answer_without_numbers_is_certain(s):
    assert gates.evaluate("t", answer(wake=False), settings=s, now=at(12)).skip
    assert gates.evaluate("t", answer(wake=True), settings=s, now=at(12)).outcome == gates.RUN
    assert gates.evaluate("t", lambda: False, settings=s, now=at(12)).skip
    assert gates.evaluate("t", lambda: True, settings=s, now=at(12)).outcome == gates.RUN
    # a contradictory answer (wake=True with p_needed 0) is not a skip
    assert gates.evaluate("t", answer(wake=True, p_needed=0.0), settings=s, now=at(12)).outcome == gates.RUN


def test_the_reason_and_data_come_back(s):
    v = gates.evaluate("t", answer(wake=False, p_needed=0.01, data={"why": "nothing changed"}), settings=s,
                       now=at(12))
    assert v.skip and "nothing changed" in v.reason and v.data == {"why": "nothing changed"}


# ---- quiet hours: hold unless P(urgent) >= 0.80 ----------------------------------------------------------------

def test_quiet_hours_hold_until_morning_unless_urgent(s):
    night = at(2, day=20)
    held = gates.evaluate("t", answer(wake=True, p_needed=1.0, p_urgent=0.79), settings=s, now=night, quiet_hold=True)
    assert held.hold and held.until == at(7, day=20) and not held.proceed
    ok = gates.evaluate("t", answer(wake=True, p_needed=1.0, p_urgent=0.80), settings=s, now=night, quiet_hold=True)
    assert ok.outcome == gates.RUN and ok.proceed
    # no urgency stated counts as 0
    assert gates.evaluate("t", answer(wake=True), settings=s, now=night, quiet_hold=True).hold
    # daytime: nothing is held
    assert gates.evaluate("t", answer(wake=True, p_urgent=0.0), settings=s, now=at(12), quiet_hold=True).outcome \
        == gates.RUN
    # work that does not reach the owner (quiet_hold False) is never held
    assert gates.evaluate("t", answer(wake=True, p_urgent=0.0), settings=s, now=night).outcome == gates.RUN
    # a sure skip stays a skip at night
    assert gates.evaluate("t", answer(wake=False, p_needed=0.0), settings=s, now=night, quiet_hold=True).skip


# ---- timeout / error -> the old path ----------------------------------------------------------------------------

def test_timeout_error_and_nonsense_follow_the_old_path(s):
    def slow():
        time.sleep(1.0)
        return {"wake": False, "p_needed": 0.0}

    def boom():
        raise RuntimeError("model fell over")

    for check, word in ((slow, "timed out"), (boom, "model fell over"), (lambda: "maybe", "dict or a bool"),
                        (lambda: {"p_needed": "x"}, "neither")):
        v = gates.evaluate("t", check, settings=s, now=at(2, day=20), quiet_hold=True, timeout=0.1)
        assert v.outcome == gates.BYPASS and v.proceed and word in v.reason  # not held, not skipped
    t0 = time.time()
    gates.evaluate("t", slow, settings=s, timeout=0.1)
    assert time.time() - t0 < 0.6  # the caller never waits for a slow check


def test_no_gate_and_gates_off_bypass(s):
    assert gates.evaluate("t", None, settings=s).outcome == gates.BYPASS
    assert events.read("gate") == []  # a place without a gate leaves no line
    s.gates_enabled = False
    v = gates.evaluate("t", answer(wake=False, p_needed=0.0), settings=s)
    assert v.outcome == gates.BYPASS and "switched off" in v.reason


# ---- the count ----------------------------------------------------------------------------------------------------

def test_outcomes_are_logged_and_counted(s):
    gates.evaluate("initiative.decide", answer(wake=False, p_needed=0.0, data={"why": "dup"}), settings=s,
                   now=at(12), subject="3 signals")
    gates.evaluate("initiative.decide", answer(wake=False, p_needed=0.0), settings=s, now=at(13))
    gates.evaluate("autowork.pick", answer(wake=False, p_needed=0.0), settings=s, now=at(14))
    gates.evaluate("t", answer(wake=True), settings=s, now=at(12))
    gates.evaluate("t", answer(wake=True), settings=s, now=at(2), quiet_hold=True)
    gates.evaluate("t", lambda: 1 / 0, settings=s)
    rows = events.read("gate")
    assert [r["outcome"] for r in rows] == ["SKIP", "SKIP", "SKIP", "RUN", "HOLD", "BYPASS"]
    assert rows[0]["gate"] == "initiative.decide" and rows[0]["reason"] and rows[0]["subject"] == "3 signals"
    c = gates.counts(0)
    assert (c["SKIP"], c["RUN"], c["HOLD"], c["BYPASS"], c["skipped_calls"]) == (3, 1, 1, 1, 3)
    assert c["by_gate"] == {"initiative.decide": 2, "autowork.pick": 1}


def test_the_evening_line_says_how_many_calls_were_skipped(s):
    now = time.time()
    assert gates.evening_line(now, s) == ""
    gates.evaluate("autowork.pick", answer(wake=False), settings=s)
    gates.evaluate("autowork.pick", answer(wake=False), settings=s)
    line = gates.evening_line(time.time(), s)
    assert "skipped 2 Claude calls" in line and "autowork.pick" in line
    from waggle import brief
    assert "skipped 2 Claude calls" in brief.evening_note(s, now=time.time())


def test_local_check_asks_the_model_and_reads_json(s):
    seen = []

    def ask(messages, settings, timeout):
        seen.append((messages, timeout))
        return json.dumps({"wake": False, "p_needed": 0.01, "p_urgent": 0, "why": "all newsletters"})

    v = gates.evaluate("t", gates.local_check("anything to decide?", "facts here", settings=s, ask=ask),
                       settings=s, now=at(12))
    assert v.skip and "all newsletters" in v.reason
    assert "anything to decide?" in seen[0][0][1]["content"] and "facts here" in seen[0][0][1]["content"]
    # a model that is unreachable is a failed check -> the old path (conftest blocks the real Ollama)
    assert gates.evaluate("t", gates.local_check("q", "f", settings=s), settings=s).outcome == gates.BYPASS
    assert not gates.ollama_ready(s)


# ---- initiative: the gate in front of the decision call ----------------------------------------------------------

def _sig(key, kind="github_pr", urgent=False, topic=None, **extra):
    return {"key": key, "kind": kind, "title": f"title {key}", "topic": topic or key, "urgent": urgent, **extra}


class Asker:
    def __init__(self, action="text"):
        self.calls, self.action = 0, action

    def __call__(self, prompt, **k):
        self.calls += 1
        data = json.loads(prompt.split("New signals:\n", 1)[1].split("\n\nUnprompted", 1)[0])
        return {"json": {"decisions": [{"key": d["key"], "action": self.action, "message": "hi", "reason": "r"}
                                       for d in data]}}


def test_initiative_skips_the_call_and_keeps_every_signal_for_the_evening(s):
    initiative.record([_sig("a"), _sig("b")], at(10))
    asker = Asker()
    info = {}
    rows = initiative.decide(s, at(11), asker=asker, info=info, gate=answer(wake=False, p_needed=0.01,
                                                                        data={"why": "routine"}))
    assert asker.calls == 0 and info["gate"]["outcome"] == "SKIP" and info["model"].startswith("not asked")
    assert [r["action"] for r in rows] == ["evening", "evening"] and all("gate:" in r["reason"] for r in rows)
    assert initiative.pending() == []
    assert {x["key"] for x in initiative.evening_items(at(12), s)} == {"a", "b"}  # nothing lost
    assert gates.counts(0)["by_gate"] == {"initiative.decide": 1}


def test_initiative_runs_when_the_gate_is_unsure_fails_or_is_off(s):
    for gate in (answer(wake=False, p_needed=0.2), lambda: 1 / 0, answer(wake=True)):
        initiative.record([_sig(f"k{id(gate)}")], at(10))
        asker = Asker(action="none")
        initiative.decide(s, at(11), asker=asker, gate=gate)
        assert asker.calls == 1
    initiative.record([_sig("off")], at(10))
    asker = Asker(action="none")
    initiative.decide(s, at(11), asker=asker, gate=False)
    assert asker.calls == 1


def test_initiative_never_gates_urgent_followup_or_asked_signals(s):
    for i, extra in enumerate(({"urgent": True}, {"kind": "followup"}, {"kind": "automation"}, {"asked": True})):
        key = f"followup:p{i}" if extra.get("kind") == "followup" else f"p{i}"
        initiative.record([_sig(key, **extra)], at(10))
        asker = Asker(action="none")
        info = {}
        initiative.decide(s, at(11), asker=asker, info=info, gate=answer(wake=False, p_needed=0.0))
        assert asker.calls == 1 and "gate" not in info, extra


def test_initiative_hold_leaves_the_signals_pending(s):
    initiative.record([_sig("a")], at(1, day=20))
    asker = Asker()
    info = {}
    rows = initiative.decide(s, at(2, day=20), asker=asker, info=info, gate=answer(wake=True, p_urgent=0.1))
    assert rows == [] and asker.calls == 0 and info["held"] == 1 and info["gate"]["outcome"] == "HOLD"
    assert len(initiative.pending()) == 1


def test_initiative_default_check_skips_repeats_and_a_used_up_cap(s):
    initiative.record([_sig("a", topic="ci:x")], at(8))
    initiative.record([_sig("seed", topic="pr:old")], at(8))
    # raise a topic through a real decision so it is on record as told
    asker = Asker()
    initiative.decide(s, at(9), asker=asker, gate=False, only_urgent=False)
    assert asker.calls == 1
    initiative.record([_sig("a2", topic="ci:x")], at(10))  # the same topic again, two signals ago
    asker = Asker()
    rows = initiative.decide(s, at(11), asker=asker)
    assert asker.calls == 0 and rows[0]["action"] == "evening" and "repeats a topic" in rows[0]["reason"]
    # the cap
    s.daily_text_cap = 0
    initiative.record([_sig("fresh", topic="new:topic")], at(12))
    asker = Asker()
    rows = initiative.decide(s, at(13), asker=asker)
    assert asker.calls == 0 and "daily cap" in rows[0]["reason"]


def test_initiative_default_check_uses_the_local_model_only_when_it_is_ready(s, monkeypatch):
    initiative.record([_sig("n1", kind="release")], at(10))
    asker = Asker(action="none")
    initiative.decide(s, at(11), asker=asker)  # no Ollama in tests: the model is not asked, the call runs
    assert asker.calls == 1
    initiative.record([_sig("n2", kind="release")], at(12))
    monkeypatch.setattr(gates, "ollama_ready", lambda settings=None, timeout=1.0: True)
    monkeypatch.setattr(gates, "_local_ask", lambda m, st, t: {"wake": False, "p_needed": 0.0, "why": "newsletter"})
    asker = Asker()
    rows = initiative.decide(s, at(13), asker=asker)
    assert asker.calls == 0 and "newsletter" in rows[0]["reason"]


# ---- the work loop's pick call ----------------------------------------------------------------------------------

def _idle(goal_id, now, n=2, **kw):
    for i in range(n):
        autowork.record_step(goal_id, "failed", title="choose the next step",
                             detail=kw.get("detail", "no useful provable step inside the charter"),
                             now=now - (n - i) * 600)


def test_the_loop_skips_the_pick_after_two_idle_picks_and_tries_again_later(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(objectives.quota, "can_escalate", lambda m, st: (True, ""))
    goals.add("g", "Do a thing", "why", indicators=[{"name": "proven steps", "measure": {"type": "proven_steps"}}],
              charter={"actions": ["write_note"], "write_roots": [str(tmp_path)], "channels": [], "money": 0,
                       "expires": (dt.date.today() + dt.timedelta(days=30)).isoformat()})
    goals.enable("g")
    goals.set_autonomy(True)
    goals.set_max_steps(20)
    now = time.time()
    calls = []

    def ask(prompt, **k):
        calls.append(prompt)
        return {"json": {"action": "none", "title": "", "instructions": "", "why": "nothing", "evidence": []}}

    _idle("g", now)
    out = autowork.run_once(settings, now=now, asker=ask)
    assert out.get("outcome") == "gated", out
    assert calls == []  # no model call, no step recorded
    assert gates.counts(0)["by_gate"] == {"autowork.pick": 1}
    out = autowork.run_once(settings, now=now + 7 * 3600, asker=ask)  # six hours later it looks again
    assert out["outcome"] == "failed" and len(calls) == 1


def test_the_loop_does_not_skip_after_a_model_outage_or_one_idle_pick(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(objectives.quota, "can_escalate", lambda m, st: (True, ""))
    goals.add("g", "Do a thing", "why", indicators=[{"name": "proven steps", "measure": {"type": "proven_steps"}}],
              charter={"actions": ["write_note"], "write_roots": [str(tmp_path)], "channels": [], "money": 0,
                       "expires": (dt.date.today() + dt.timedelta(days=30)).isoformat()})
    goals.enable("g")
    goals.set_autonomy(True)
    goals.set_max_steps(20)
    now = time.time()
    calls = []

    def ask(prompt, **k):
        calls.append(1)
        return {"json": {"action": "none", "why": "nothing", "evidence": []}}

    _idle("g", now, n=2, detail="model call failed: timeout")  # an outage is not "nothing to do"
    autowork.run_once(settings, now=now, asker=ask)
    assert len(calls) == 1
    # that pick found nothing: one idle pick in a row (the outage rows are not idle) -> the gate is not sure yet
    autowork.run_once(settings, now=now + 100, asker=ask)
    assert len(calls) == 2
    # two in a row now: the third look is skipped
    out = autowork.run_once(settings, now=now + 200, asker=ask)
    assert out["outcome"] == "gated" and len(calls) == 2


# ---- routines: an optional gate, and notify-or-not ---------------------------------------------------------------

@pytest.fixture
def routine(s, monkeypatch):
    monkeypatch.setattr(routines.quota, "can_escalate", lambda m, st: (True, ""))
    monkeypatch.setattr(notifycheck, "_ask", lambda m, st, t: {"decision": "yes", "reason": "worth it"})
    ran = []

    def run(agent_name, prompt, **k):
        ran.append(prompt)
        return types.SimpleNamespace(text="3 unpushed commits in waggle", ok=True, error="", approvals=[], deferred=0)
    run.ran = ran
    return run


def test_a_routine_gate_skips_the_run_and_the_schedule_moves_on(s, routine):
    gates.register("repos_clean", lambda routine, settings: {"wake": False, "p_needed": 0.0,
                                                             "data": {"why": "no repo changed"}})
    rid = routines.add("repo-watch", "every 1h", "check repos", "monitor", "telegram", gate={"name": "repos_clean"})
    assert routines.run_one(rid, s, run_fn=routine, now=at(12)) == "gate-skipped"
    r = routines._load()[rid]
    assert routine.ran == [] and "no repo changed" in r["last_status"] and r["next_run"] == at(13)
    assert gates.counts(0)["by_gate"] == {"routine.repo-watch": 1}
    # the gate is kept when the routine is re-added without one
    routines.add("repo-watch", "every 2h", "check repos", "monitor", "telegram")
    assert routines._load()[rid]["gate"] == {"name": "repos_clean"}


def test_a_routine_gate_that_wakes_fails_or_is_absent_runs_the_routine(s, routine):
    gates.register("maybe", lambda routine, settings: {"wake": True, "p_urgent": 1.0})
    gates.register("broken", lambda routine, settings: 1 / 0)
    for gate in ({"name": "maybe"}, {"name": "broken"}, None, {"question": "x"}):  # the last: Ollama not ready
        rid = routines.add(f"r{len(routine.ran)}", "every 1h", "check", "monitor", "telegram", gate=gate)
        assert routines.run_one(rid, s, run_fn=routine, verify_fn=lambda *a, **k: (True, ""), now=at(12)) == "ok"
    assert len(routine.ran) == 4


def test_a_routine_that_would_reach_the_owner_waits_in_quiet_hours(s, routine):
    gates.register("meh", lambda routine, settings: {"wake": True, "p_urgent": 0.2})
    rid = routines.add("night-watch", "every 1h", "check", "monitor", "telegram", gate={"name": "meh"})
    assert routines.run_one(rid, s, run_fn=routine, now=at(2, day=20)) == "held"
    assert routines._load()[rid]["next_run"] == at(7, day=20) and routine.ran == []
    # a vault-only routine reaches nobody: no hold
    rid2 = routines.add("night-vault", "every 1h", "check", "monitor", "vault", gate={"name": "meh"})
    assert routines.run_one(rid2, s, run_fn=routine, now=at(2, day=20)) == "ok"


def test_an_always_report_routine_ignores_its_gate(s, routine):
    gates.register("never", lambda routine, settings: False)
    rid = routines.add("nightly", "daily 20:30", "review", "monitor", "vault", always_report=True,
                       gate={"name": "never"})
    assert routines.run_one(rid, s, run_fn=routine, now=at(20, 30)) == "ok"


def test_notify_check_yes_sends_the_message(s, routine):
    rid = routines.add("repo-watch", "every 1h", "check repos", "monitor", "telegram")
    from waggle import notify
    before = len(notify.PHONE_LOG)
    routines.run_one(rid, s, run_fn=routine, verify_fn=lambda *a, **k: (True, ""), now=at(12))
    assert len(notify.PHONE_LOG) == before + 1 and "unpushed" in notify.PHONE_LOG[-1]
    assert initiative.evening_items(at(12), s) == []


def test_notify_check_no_moves_the_result_into_the_evening_note(s, routine, monkeypatch):
    monkeypatch.setattr(notifycheck, "_ask", lambda m, st, t: {"decision": "no", "reason": "nothing new"})
    rid = routines.add("repo-watch", "every 1h", "check repos", "monitor", "telegram")
    from waggle import notify
    before = len(notify.PHONE_LOG)
    verify = []
    routines.run_one(rid, s, run_fn=routine, verify_fn=lambda *a, **k: verify.append(1) or (True, ""), now=at(12))
    assert len(notify.PHONE_LOG) == before and verify == []  # no message, and the check call is saved too
    items = initiative.evening_items(at(12), s)
    assert len(items) == 1 and "repo-watch" in items[0]["title"] and "unpushed" in items[0]["title"]
    assert "unpushed" in vault.read("Routines/repo-watch")  # a telegram-only routine's full text is kept
    from waggle import brief
    note = brief.evening_note(s, now=at(21))
    assert "Saved for tonight" in note and "unpushed" in note
    assert any(e["notify"] is False and e["by"] == "model" for e in events.read("notify_check"))


def test_notify_check_failure_goes_to_the_evening_note_never_a_push(s, routine, monkeypatch):
    def down(m, st, t):
        raise OSError("ollama is down")

    monkeypatch.setattr(notifycheck, "_ask", down)
    rid = routines.add("repo-watch", "every 1h", "check repos", "monitor", "both")
    from waggle import notify
    before = len(notify.PHONE_LOG)
    routines.run_one(rid, s, run_fn=routine, verify_fn=lambda *a, **k: (True, ""), now=at(12))
    assert len(notify.PHONE_LOG) == before
    assert len(initiative.evening_items(at(12), s)) == 1
    ev = events.read("notify_check")[-1]
    assert ev["by"] == "failure" and ev["notify"] is False and "evening" in ev["reason"]
    assert "1 background result moved into this note" in gates.evening_line(time.time(), s)


def test_notify_check_unit(s):
    assert notifycheck.decide("q", "r", settings=s, ask=lambda m, st, t: {"decision": "Yes"})["notify"] is True
    assert notifycheck.decide("q", "r", settings=s, ask=lambda m, st, t: '{"decision": "no", "reason": "x"}') == {
        "notify": False, "reason": "x", "by": "model"}
    for bad in ({"decision": "maybe"}, {}, "not json", 7):
        out = notifycheck.decide("q", "r", settings=s, ask=lambda m, st, t, b=bad: b)
        assert out["notify"] is False and out["by"] == "failure"
    s.notify_check_enabled = False
    assert notifycheck.decide("q", "r", settings=s, ask=lambda *a: 1 / 0) == {
        "notify": True, "reason": "notify check is switched off", "by": "off"}


def test_a_finished_research_objective_is_judged_other_objectives_are_not(s, monkeypatch):
    monkeypatch.setattr(objectives.quota, "can_escalate", lambda m, st: (True, ""))
    monkeypatch.setattr(objectives, "kick", lambda oid, user=True: None)
    monkeypatch.setattr(objectives.claude_cli, "ask", lambda *a, **k: {"text": "Found two internships."})
    from waggle import notify
    plan = lambda agent_name: (lambda *a, **k: {"json": {"title": "T", "steps": [  # noqa: E731
        {"title": "s", "detail": "d", "agent": agent_name, "done_when": "x", "depends_on": []}]}})
    ok_run = lambda *a, **k: types.SimpleNamespace(text="done", ok=True, error="", approvals=[], deferred=0)  # noqa: E731
    verify = lambda *a, **k: (True, "")  # noqa: E731

    monkeypatch.setattr(notifycheck, "_ask", lambda m, st, t: {"decision": "no", "reason": "nothing new"})
    oid = objectives.create("find internships", asker=plan("researcher"), settings=s)
    before = len(notify.PHONE_LOG)
    assert objectives.work(oid, settings=s, run_fn=ok_run, verify_fn=verify) == "done"
    assert len(notify.PHONE_LOG) == before  # not told: the evening note has it
    assert any("Objective done" in x["title"] for x in initiative.evening_items(time.time(), s))

    oid2 = objectives.create("write a note", asker=plan("general"), settings=s)  # not research: always told
    assert objectives.work(oid2, settings=s, run_fn=ok_run, verify_fn=verify) == "done"
    assert len(notify.PHONE_LOG) == before + 1

    def down(m, st, t):
        raise OSError("down")

    monkeypatch.setattr(notifycheck, "_ask", down)  # the model fails: evening note, never a push
    oid3 = objectives.create("find more", asker=plan("researcher"), settings=s)
    assert objectives.work(oid3, settings=s, run_fn=ok_run, verify_fn=verify) == "done"
    assert len(notify.PHONE_LOG) == before + 1
    monkeypatch.setattr(notifycheck, "_ask", lambda m, st, t: {"decision": "yes"})
    oid4 = objectives.create("find yet more", asker=plan("researcher"), settings=s)
    assert objectives.work(oid4, settings=s, run_fn=ok_run, verify_fn=verify) == "done"
    assert len(notify.PHONE_LOG) == before + 2


# ---- cooldowns per error kind ------------------------------------------------------------------------------------

NOW = at(12)


@pytest.mark.parametrize("kind, minutes", [("limit", 5), ("credit", 60), ("busy", 2), ("auth", 10), ("missing", 30),
                                           ("down", 2)])
def test_each_kind_rests_for_its_own_time_and_then_expires(s, kind, minutes):
    until = cooldowns.note("claude", kind, "x", now=NOW)
    assert until == NOW + minutes * 60
    assert cooldowns.cooling("claude", NOW + minutes * 60 - 1)[:2] == (True, kind)
    assert cooldowns.cooling("claude", NOW + minutes * 60)[0] is False  # expired
    assert cooldowns.cooling("ollama", NOW)[0] is False  # per engine
    assert cooldowns.first_free(["claude", "ollama"], NOW) == "ollama"
    assert cooldowns.first_free(["claude", "ollama"], NOW + minutes * 60) == "claude"


def test_the_reset_time_in_the_error_text_is_used(s):
    assert cooldowns.reset_at("resets in 2h", NOW) == NOW + 7200
    assert cooldowns.reset_at("Please try again in 45 minutes.", NOW) == NOW + 2700
    assert cooldowns.reset_at("resets in 1 hour 30 minutes", NOW) == NOW + 5400
    assert cooldowns.reset_at("You've hit your limit · resets 3pm", NOW) == at(15)
    assert cooldowns.reset_at("limit reached, resets at 11:30 AM", NOW) == at(11, 30, day=20)  # next morning
    assert cooldowns.reset_at("something else broke", NOW) is None
    assert cooldowns.note("claude", "limit", "You've hit your limit · resets 3pm", now=NOW) == at(15)
    cooldowns.clear()
    # absurd or past times fall back: capped at six hours, never shorter than 30 s
    assert cooldowns.note("claude", "limit", "resets in 48h", now=NOW) == NOW + 6 * 3600
    cooldowns.clear()
    assert cooldowns.note("claude", "limit", "limit", now=NOW, reset_in_hours=0.5) == NOW + 1800  # from the wrapper
    cooldowns.clear()
    assert cooldowns.note("claude", "limit", "limit reached", now=NOW) == NOW + 300  # nothing said: 5 min


@pytest.mark.parametrize("kind, text, exc, expect", [
    ("limit", "You've hit your limit", None, "limit"),
    ("error", "Claude AI usage limit reached|1763600000", None, "limit"),
    ("error", "Your credit balance is too low", None, "credit"),
    ("error", "Overloaded", None, "busy"),
    ("limit", "Overloaded (429)", None, "busy"),
    ("auth", "Claude CLI sign-in expired", None, "auth"),
    ("missing", "Claude Code CLI not found", None, "missing"),
    ("error", "Unreadable CLI output: Connection refused", None, "down"),
    ("", "", ConnectionRefusedError("refused"), "down"),
    ("", "HTTP 503 service unavailable", None, "down"),
    ("", "", FileNotFoundError("claude"), "missing"),
    ("busy", "", None, "busy"),
    # not about the engine itself: nothing rests
    ("error", "Prompt is too long", None, None),
    ("timeout", "Claude CLI timed out after 300s", None, None),
    ("model_mismatch", "Asked for haiku but sonnet answered", None, None),
    ("budget", "background budget: used", None, None),
    ("", "", TimeoutError("slow"), None),
])
def test_errors_map_to_a_kind_or_to_nothing(kind, text, exc, expect):
    assert cooldowns.classify(kind, text, exc=exc) == expect


class Eng(engines.Engine):
    def __init__(self, name, reply=None, exc=None):
        super().__init__(name)
        self.reply, self.exc, self.calls = reply, exc, 0

    def models(self, timeout=5):
        return ["m"]

    def chat(self, messages, model, timeout=120, **opts):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.reply


def test_the_fallback_chain_skips_a_resting_engine_and_goes_to_the_local_model(s):
    claude = Eng("claude", exc=claude_cli.ClaudeError("You've hit your limit · resets 3pm", "limit", 3))
    local = Eng("ollama", reply="pong")
    chain = [("claude", "haiku"), ("ollama", "x")]
    msgs = [{"role": "user", "content": "ping"}]
    assert engines.chat(msgs, chain=chain, engines={"claude": claude, "ollama": local}, settings=s)[1] == "ollama"
    assert claude.calls == 1 and cooldowns.cooling("claude")[:2] == (True, "limit")
    for _ in range(3):  # the next calls never reach Claude
        assert engines.chat(msgs, chain=chain, engines={"claude": claude, "ollama": local}, settings=s)[1] == "ollama"
    assert claude.calls == 1 and local.calls == 4
    assert any(e["engine"] == "claude" and e["error_kind"] == "limit" for e in events.read("cooldown"))


def test_every_engine_resting_is_an_honest_failure_and_the_switch_turns_it_all_off(s):
    claude = Eng("claude", exc=claude_cli.ClaudeError("Your credit balance is too low", "error"))
    local = Eng("ollama", exc=ConnectionError("Ollama not reachable"))
    chain = [("claude", "haiku"), ("ollama", "x")]
    msgs = [{"role": "user", "content": "ping"}]
    with pytest.raises(RuntimeError, match="all engines failed") as e:
        engines.chat(msgs, chain=chain, engines={"claude": claude, "ollama": local}, settings=s)
    assert "credit" in str(e.value) and cooldowns.cooling("claude")[1] == "credit" \
        and cooldowns.cooling("ollama")[1] == "down"
    with pytest.raises(RuntimeError, match="resting"):
        engines.chat(msgs, chain=chain, engines={"claude": claude, "ollama": local}, settings=s)
    assert (claude.calls, local.calls) == (1, 1)
    s.engine_cooldowns_enabled = False
    with pytest.raises(RuntimeError):
        engines.chat(msgs, chain=chain, engines={"claude": claude, "ollama": local}, settings=s)
    assert (claude.calls, local.calls) == (2, 2)  # off: every call tries the engine again


def test_an_error_that_is_not_about_the_engine_does_not_rest_it(s):
    claude = Eng("claude", exc=claude_cli.ClaudeError("Prompt is too long", "error"))
    local = Eng("ollama", reply="pong")
    for _ in range(2):
        engines.chat([], chain=[("claude", "haiku"), ("ollama", "x")], engines={"claude": claude, "ollama": local},
                     settings=s)
    assert claude.calls == 2 and cooldowns.cooling("claude")[0] is False


def _fake_claude(monkeypatch, result, calls):
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "claude")

    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(stdout=json.dumps({"is_error": True, "result": result}), stderr="",
                                     returncode=1)
    monkeypatch.setattr(subprocess, "run", run)


def test_claude_cli_starts_no_process_while_resting(s, monkeypatch):
    calls = []
    _fake_claude(monkeypatch, "You've hit your limit · resets 3pm", calls)
    with pytest.raises(claude_cli.ClaudeError) as first:
        claude_cli.ask("hi", model="haiku", purpose="t")
    assert first.value.kind == "limit" and len(calls) == 1
    assert cooldowns.cooling("claude")[:2] == (True, "limit")
    with pytest.raises(claude_cli.ClaudeError) as second:
        claude_cli.ask("hi again", model="haiku", purpose="t")
    assert second.value.kind == "limit" and "resting" in str(second.value) and len(calls) == 1  # no new process
    with pytest.raises(claude_cli.ClaudeError):
        claude_cli.stream("hi", model="haiku", purpose="t")
    assert len(calls) == 1
    cooldowns.clear("claude")  # a login ends the rest
    with pytest.raises(claude_cli.ClaudeError):
        claude_cli.ask("hi", model="haiku", purpose="t")
    assert len(calls) == 2


def test_the_resting_error_keeps_the_kinds_callers_already_handle(s):
    for kind, expect in (("credit", "limit"), ("busy", "timeout"), ("down", "timeout"), ("auth", "auth")):
        cooldowns.clear()
        cooldowns.note("claude", kind, "x")
        err = claude_cli._resting()
        assert err.kind == expect and err.resting
    cooldowns.clear()
    assert claude_cli._resting() is None
    # a resting error never re-starts the rest it is about
    cooldowns.note("claude", "auth", "x", now=NOW)
    before = cooldowns._load()["claude"]["until"]
    claude_cli._rest_after(types.SimpleNamespace(resting=True, kind="auth", reset_in_hours=None))
    assert cooldowns._load()["claude"]["until"] == before


def test_the_owners_chat_goes_straight_to_the_local_model_while_claude_rests(s):
    from waggle import chat_engine
    assert chat_engine.claude_available(s)[0] is True
    cooldowns.note("claude", "busy", "Overloaded")
    ok, why = chat_engine.claude_available(s)
    assert ok is False and why.startswith("busy:") and "resting" in why
    cooldowns.clear("claude")
    assert chat_engine.claude_available(s)[0] is True


def test_a_damaged_cooldown_file_means_nothing_rests(s):
    from waggle.config import data_path
    data_path(cooldowns.FILE).write_text("{not json", encoding="utf-8")
    assert cooldowns.cooling("claude")[0] is False
    assert cooldowns.note("claude", "limit", "x", now=NOW) == NOW + 300  # and it heals itself
    data_path(cooldowns.FILE).write_text(json.dumps({"claude": {"until": "soon"}}), encoding="utf-8")
    assert cooldowns.cooling("claude")[0] is False
