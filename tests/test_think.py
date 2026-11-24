"""The thinking pass (waggle/think.py). The model is always a fake here; tools/think_eval.py runs the real one."""
import datetime as dt
import json

import pytest

from waggle import initiative, notify, think, tick, vault
from waggle.config import Settings
from waggle.core import timers


def at(h, m=0, day=18):
    return dt.datetime(2026, 11, day, h, m).timestamp()


@pytest.fixture
def s(settings):
    settings.quiet_hours = [23, 7]
    settings.watch_roots = []
    settings.home_location = ""
    settings.save()
    return Settings.load()


def answer(thoughts=(), lookups=(), questions=()):
    return {"json": {"thoughts": list(thoughts), "lookups": list(lookups), "questions": list(questions)}}


def thought(topic="mens-day-dad", action="text", message="Tomorrow is International Men's Day. Text your dad?",
            draft="Happy Men's Day, Dad.", why="the date and a father in the picture"):
    return {"topic": topic, "action": action, "message": message, "draft": draft, "why": why}


class Asker:
    def __init__(self, *answers):
        self.answers, self.prompts, self.kw = list(answers), [], []

    def __call__(self, prompt, **kw):
        self.prompts.append(prompt)
        self.kw.append(kw)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def texts():
    return [r["text"] for r in notify.rows(since=0.0)]


# ---- the picture ---------------------------------------------------------------------------------------------

def test_picture_names_the_date_and_the_days_ahead(s):
    p = think.picture(s, at(16, 15), activity=False, weather=False)
    assert "Wednesday 2026-11-18" in p["Now"] and "Thu 2026-11-19" in p["Now"]


def test_picture_lists_people_from_waggles_own_notes(s):
    vault.write("People/Dad.md", "# Dad\n- Father, lives at home\n- Birthday 3 March\n", s)
    p = think.picture(s, at(16, 15), activity=False, weather=False)
    people = next(v for k, v in p.items() if k.startswith("People"))
    assert "Dad" in people and "Birthday 3 March" in people


def test_a_section_that_breaks_is_left_out(s, monkeypatch):
    monkeypatch.setattr(think, "_people_section", lambda *_: 1 / 0)
    p = think.picture(s, at(16, 15), activity=False, weather=False)
    assert "Now" in p and not any(k.startswith("People") for k in p)


def test_picture_shows_earlier_thoughts_and_questions(s):
    think.run(s, at(8), asker=Asker(answer([thought()], questions=[{"question": "Who are you closest to?",
                                                                    "why": "no people notes"}])),
              sections={"Now": "x"})
    p = think.render(think.picture(s, at(16, 15), activity=False, weather=False))
    assert "topic mens-day-dad" in p and "Who are you closest to?" in p


# ---- reasoning -----------------------------------------------------------------------------------------------

def test_reason_uses_the_think_purpose_and_the_schema(s):
    ask = Asker(answer([thought()]))
    out = think.reason("picture", s, asker=ask)
    assert ask.kw[0]["purpose"] == "think" and ask.kw[0]["schema"] is think.SCHEMA
    assert out["calls"] == 1 and out["thoughts"][0]["topic"] == "mens-day-dad"


def test_lookups_run_once_and_their_results_go_into_a_second_call(s):
    ask = Asker(answer(lookups=[{"kind": "vault", "query": "dad birthday"}, {"kind": "shell", "query": "rm"}]),
                answer([thought()], lookups=[{"kind": "vault", "query": "again"}]))
    ran = []
    out = think.reason("picture", s, asker=ask,
                       lookup_runners={"vault": lambda q, st: ran.append(q["query"]) or "Dad: birthday 3 March"})
    assert ran == ["dad birthday"] and out["calls"] == 2 and len(ask.prompts) == 2
    assert "Dad: birthday 3 March" in ask.prompts[1] and out["thoughts"]


def test_at_most_three_lookups_and_a_failing_one_does_not_raise(s):
    wanted = [{"kind": "vault", "query": str(i)} for i in range(6)]
    got = think.run_lookups(wanted, s, {"vault": lambda q, st: 1 / 0})
    assert len(got) == think.MAX_LOOKUPS and all("look-up failed" in g["result"] for g in got)


def test_the_only_lookup_kinds_are_read_only(s):
    assert set(think.LOOKUPS) == {"vault", "weather", "web"}


def test_unknown_action_and_duplicate_topics_are_cleaned(s):
    out = think._clean_thoughts([thought(action="shout"), thought(), {"message": ""}, "junk"])
    assert len(out) == 1 and out[0]["action"] == "none"


# ---- acting --------------------------------------------------------------------------------------------------

def test_a_thought_goes_out_once_with_its_draft_through_the_outbox(s):
    rep = think.run(s, at(16, 15), asker=Asker(answer([thought()])), sections={"Now": "x"})
    assert rep["ok"] and rep["acted"]["thoughts"][0]["action"] == "text"
    out = texts()
    assert len(out) == 1 and "Men's Day" in out[0] and "Draft: Happy Men's Day, Dad." in out[0]
    d = initiative.decisions()[-1]
    assert d["kind"] == "thought" and d["decided_by"] == "think"


def test_the_same_topic_is_not_sent_again_the_same_day_or_week(s):
    ask = Asker(answer([thought()]))
    think.run(s, at(8), asker=ask, sections={"Now": "x"})
    think.run(s, at(16, 15), asker=ask, sections={"Now": "x"})
    think.run(s, at(16, 15, day=20), asker=ask, sections={"Now": "x"})
    assert len(texts()) == 1


def test_action_none_and_an_empty_answer_send_nothing(s):
    rep = think.run(s, at(16, 15), asker=Asker(answer([thought(action="none")])), sections={"Now": "x"})
    assert rep["ok"] and texts() == []
    rep = think.run(s, at(17), asker=Asker(answer()), sections={"Now": "x"})
    assert rep["ok"] and rep["thoughts"] == [] and texts() == []


def test_evening_thoughts_wait_for_the_evening_note(s):
    think.run(s, at(16, 15), asker=Asker(answer([thought(action="evening")])), sections={"Now": "x"})
    assert texts() == []
    assert any(r["kind"] == "thought" for r in initiative.evening_items(at(20)))


def test_quiet_hours_hold_a_thought(s):
    rep = think.run(s, at(23, 30), asker=Asker(answer([thought()])), sections={"Now": "x"})
    assert rep["acted"]["thoughts"][0]["action"] == "evening" and texts() == []


def test_the_daily_cap_counts_thoughts(s):
    s.daily_text_cap = 1
    s.save()
    s = Settings.load()
    think.run(s, at(16, 15), asker=Asker(answer([thought("a"), thought("b", message="Another thing.")])),
              sections={"Now": "x"})
    assert len(texts()) == 1


def test_at_most_three_thoughts_a_pass(s):
    many = [thought(f"t{i}", message=f"Thing {i}.") for i in range(6)]
    rep = think.run(s, at(16, 15), asker=Asker(answer(many)), sections={"Now": "x"})
    assert len(rep["acted"]["thoughts"]) == think.MAX_THOUGHTS


def test_dry_run_sends_nothing_but_is_recorded(s):
    rep = think.run(s, at(16, 15), asker=Asker(answer([thought()])), dry=True, sections={"Now": "x"})
    assert rep["ok"] and "acted" not in rep and texts() == []
    assert think.passes(1)[0]["dry"] == 1


def test_a_model_that_is_unavailable_is_reported_not_raised(s):
    def broken(prompt, **kw):
        raise RuntimeError("background budget: the 7-day Claude limit is at 87%")
    rep = think.run(s, at(16, 15), asker=broken, sections={"Now": "x"})
    assert not rep["ok"] and "7-day" in rep["note"] and think.passes(1)[0]["ok"] == 0


def test_switched_off_does_nothing(s):
    s.think_enabled = False
    s.save()
    rep = think.run(Settings.load(), at(16, 15), asker=Asker(answer([thought()])), sections={"Now": "x"})
    assert rep["note"].startswith("switched off") and texts() == []


# ---- questions -----------------------------------------------------------------------------------------------

Q = {"question": "Who are the people closest to you, and any birthdays I should know?", "why": "no people notes"}


def test_one_question_a_day_and_never_the_same_twice(s):
    ask = Asker(answer(questions=[Q]))
    think.run(s, at(16, 15), asker=ask, sections={"Now": "x"})
    think.run(s, at(19, 45), asker=Asker(answer(questions=[{"question": "What time is practice?", "why": "x"}])),
              sections={"Now": "x"})
    think.run(s, at(16, 15, day=19), asker=ask, sections={"Now": "x"})
    out = texts()
    assert len(out) == 1 and "closest to you" in out[0] and out[0].startswith("🐝 A question")


def test_no_new_question_while_two_wait_unanswered(s):
    for day, q in ((18, "Who are the people closest to you?"), (19, "When does your school day end?")):
        think.run(s, at(16, 15, day=day), asker=Asker(answer(questions=[{"question": q, "why": "x"}])),
                  sections={"Now": "x"})
    rep = think.run(s, at(16, 15, day=20), asker=Asker(answer(questions=[{"question": "Any big dates in December?",
                                                                          "why": "x"}])), sections={"Now": "x"})
    assert len(texts()) == 2 and "unanswered" in rep["acted"]["question"]["held"]


def test_no_question_in_quiet_hours(s):
    rep = think.run(s, at(23, 30), asker=Asker(answer(questions=[Q])), sections={"Now": "x"})
    assert texts() == [] and rep["acted"]["question"]["held"] == "quiet hours"


# ---- timers and the job --------------------------------------------------------------------------------------

def test_times_drop_bad_entries_and_default_when_empty(s):
    s.think_times = ["7:40", "25:00", "nope", "16:15"]
    assert think.times(s) == [(0, "07:40"), (1, "16:15")]
    s.think_times = []
    assert [t for _, t in think.times(s)] == list(think.DEFAULT_TIMES)


def test_register_core_adds_the_think_timers_and_removes_dropped_ones(s, monkeypatch):
    import sys
    import types
    mod = types.ModuleType("waggle.core.sender")
    mod.register = lambda kind, fn: None
    monkeypatch.setitem(sys.modules, "waggle.core.sender", mod)
    report = tick.register_core(now=at(6))
    assert {"outreach.think.0", "outreach.think.1", "outreach.think.2"} <= set(report["timers"])
    row = timers.get("outreach.think.1")
    assert row["job_kind"] == "outreach.think" and row["spec"] == "15 16 * * *"
    s.think_times = ["12:00"]
    s.save()
    tick.register_core(now=at(6))
    assert timers.get("outreach.think.0")["spec"] == "0 12 * * *" and timers.get("outreach.think.1") is None


def test_the_job_runs_one_pass(s, monkeypatch):
    monkeypatch.setattr(think, "picture", lambda *a, **k: {"Now": "x"})
    out = think.due({"slot": "16:15"}, settings=s, now=at(16, 15), asker=Asker(answer([thought()])))
    assert out["ok"] and out["thoughts"] == [("mens-day-dad", "text")] and len(texts()) == 1
    assert "outreach.think" in tick.HANDLERS and "outreach.think" in tick.JOB_KINDS


def test_think_has_its_own_small_budget_allowance():
    from waggle import budget
    assert budget.OWN_ALLOWANCE["think"]["daily_calls"] <= 10


def test_the_prompt_names_no_situation():
    """The rule of this module: situations are test cases, never prompt lines."""
    low = think.SYSTEM.lower()
    for word in ("men's day", "mother", "father", "birthday", "holiday", "valentine", "christmas"):
        assert word not in low


# ---- the owner's taste and the world calendar ----------------------------------------------------------------

def test_taste_note_is_the_owners_words_and_goes_into_the_picture(s):
    assert think.taste(s) == ""
    assert think.add_taste("Tell me when a day is a reason to reach out to someone.", s, now=at(9))
    assert not think.add_taste("   ", s)
    p = think.picture(s, at(16, 15), activity=False, weather=False)
    assert "reason to reach out" in p[think.TASTE_TITLE] and "(2026-11-18)" in p[think.TASTE_TITLE]


def test_picture_carries_the_calendar_and_survives_no_network(s):
    p = think.picture(s, at(16, 15), activity=False, weather=False)   # conftest: Wikipedia is unreachable
    assert think.CALENDAR_TITLE not in p
    p = think.picture(s, at(16, 15), activity=False, weather=False, calendar="Thu 2026-11-19: Some Day")
    assert p[think.CALENDAR_TITLE] == "Thu 2026-11-19: Some Day"
    assert list(p)[:2] == ["Now", think.CALENDAR_TITLE]


def test_reason_returns_the_scan(s):
    res = answer([thought()])
    res["json"]["scan"] = {"dates": [{"date": "Thu", "on_the_calendar": "x"}], "people": "", "open_loops": "",
                           "risks": ""}
    assert think.reason("p", s, asker=Asker(res))["scan"]["dates"][0]["date"] == "Thu"
