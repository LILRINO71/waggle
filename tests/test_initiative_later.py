"""The decision step can say "look again at": a one-shot core timer brings the same finding back, with a counter;
after three deferrals it is told or dropped with the reason recorded. A check-in on something the user said they
would do is never dropped silently. The model and the clock are fakes."""
import datetime as dt
import re

import pytest

from waggle import followups, initiative, tick
from waggle.config import Settings
from waggle.core import jobs, outbox, timers


def at(h, m=0, day=19):
    return dt.datetime(2026, 11, day, h, m).timestamp()


@pytest.fixture
def s(settings):
    settings.quiet_hours = [23, 7]
    settings.watch_roots = []
    settings.save()
    tick.register_jobs()
    return Settings.load()


def sig(key, kind="github_pr", urgent=False, title=None):
    return {"key": key, "kind": kind, "title": title or f"{key} happened", "detail": "", "topic": key,
            "urgent": urgent, "when": at(12), "url": "", "project": ""}


class Model:
    """Answers from a script: one dict per call, applied to every signal it is asked about."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt, **kw):
        self.prompts.append(prompt)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        keys = re.findall(r'"key": "([^"]+)"', prompt)
        return {"json": {"decisions": [{"key": k, "message": "", "reason": "because", **a} for k in keys]}}


def sent():
    return sorted(outbox.list(limit=1000), key=lambda r: r["id"])


def wake(now):
    """What the core does: fire due timers, run the recheck jobs."""
    timers.fire_due(now)
    while jobs.run_one(kinds=["outreach.recheck"], now=now):
        pass


def test_later_sets_a_one_shot_timer_and_brings_the_finding_back_with_a_counter(s):
    initiative.record([sig("pr1", title="PR waiting 3 days")], at(12))
    model = Model({"action": "later", "look_again_minutes": 90, "reason": "they are in class until 13:30"},
                  {"action": "text", "message": "Your PR has waited 3 days."})
    out = initiative.decide(s, at(12), asker=model, activity={})
    assert [(d["signal"], d["action"], d["model_action"]) for d in out] == [("pr1", "later", "later")]
    assert sent() == [] and initiative.pending() == []
    t = timers.get("recheck:pr1:1")
    assert t["kind"] == "once" and t["catch_up"] == "all" and t["job_kind"] == "outreach.recheck"
    assert abs(t["next_run"] - (at(12) + 90 * 60)) < 1
    wake(at(13, 29))
    assert initiative.pending() == [], "not yet"
    wake(at(13, 31))
    back = initiative.pending()
    assert [x["key"] for x in back] == ["pr1"]
    assert initiative.due(s, at(13, 32)), "a finding that came back is decided at once, not two hours later"
    out = initiative.decide(s, at(13, 32), asker=model, activity={})
    assert out[0]["action"] == "text"
    assert '"deferred_before": 1' in model.prompts[-1] and "they are in class" in model.prompts[-1]
    assert [r["body"] for r in sent()] == ["🐝 Your PR has waited 3 days."]
    assert not initiative.due(s, at(13, 40))


def test_a_deferral_that_fell_while_the_pc_was_off_still_comes_back(s):
    initiative.record([sig("pr1")], at(12))
    initiative.decide(s, at(12), asker=Model({"action": "later", "look_again_minutes": 60}), activity={})
    timers.catch_up(at(20))                           # the PC was off at 13:00
    while jobs.run_one(kinds=["outreach.recheck"], now=at(20)):
        pass
    assert [x["key"] for x in initiative.pending()] == ["pr1"]


def test_after_three_deferrals_it_is_told_or_dropped_with_the_reason(s):
    initiative.record([sig("pr1"), sig("news1", kind="mail", title="Newsletter")], at(8))
    later = Model({"action": "later", "look_again_minutes": 30, "reason": "not yet"})
    now = at(8)
    for n in range(3):
        out = initiative.decide(s, now, asker=later, activity={})
        assert {d["action"] for d in out} == {"later"}
        now += 31 * 60
        wake(now)
    assert initiative.deferral("pr1")["count"] == 3
    out = {d["signal"]: d for d in initiative.decide(s, now, asker=later, activity={})}
    assert '"can_defer": false' in later.prompts[-1]
    assert out["pr1"]["action"] == "none" and "deferred 3 times" in out["pr1"]["guard"]
    assert out["news1"]["action"] == "none" and "dropped" in out["news1"]["guard"]
    assert out["pr1"]["model_action"] == "later" and out["pr1"]["reason"] == "not yet"


def test_urgent_is_told_after_three_deferrals_and_never_put_off_long(s):
    initiative.record([sig("up1", kind="upcoming", urgent=True, title="Quiz in 20 minutes")], at(8))
    later = Model({"action": "later", "look_again_minutes": 600, "reason": "wait"})
    now = at(8)
    for n in range(3):
        initiative.decide(s, now, asker=later, activity={})
        t = timers.get(f"recheck:up1:{n + 1}")
        assert t["next_run"] - now <= 30 * 60 + 1, "an urgent finding is never put off by more than half an hour"
        now += 31 * 60
        wake(now)
    out = initiative.decide(s, now, asker=later, activity={})
    assert out[0]["action"] == "text" and "deferred 3 times" in out[0]["guard"]
    assert len(sent()) == 1


def _checkin(s, now=None):
    now = now or at(19, day=20)
    said = dt.datetime(2026, 11, 19, 16, 0)
    rows = [{"id": 1, "time": said.timestamp(), "platform": "telegram", "request": "I'll test the shooter tomorrow"}]
    item = followups.capture(s, said.timestamp(), rows=rows)[0]
    timers.fire_due(now)
    jobs.run_one(kinds=["outreach.checkin"], now=now)
    return item


def test_a_checkin_the_model_would_drop_is_deferred_not_dropped(s):
    item = _checkin(s)
    none = Model({"action": "none", "reason": "too early, follow up later"})
    out = initiative.decide(s, at(19, 5, day=20), asker=none, activity={}, turns=[])
    assert out[0]["action"] == "later" and out[0]["model_action"] == "none"
    assert "check-in" in out[0]["guard"]
    assert followups.get(item["id"])["status"] == "deferred"
    assert timers.get(f"recheck:followup:{item['id']}:1") is not None


def test_a_checkin_is_told_when_three_deferrals_did_not_settle_it(s):
    item = _checkin(s)
    none = Model({"action": "none", "reason": "too early"})
    now = at(19, 5, day=20)
    for n in range(3):
        out = initiative.decide(s, now, asker=none, activity={}, turns=[])
        assert out[0]["action"] == "later"
        now += 2 * 3600
        wake(now)
    out = initiative.decide(s, now, asker=none, activity={}, turns=[])
    assert out[0]["action"] in ("text", "evening") and "check-in" in out[0]["guard"]
    assert followups.get(item["id"])["status"] in ("asked", "evening")


def test_a_checkin_is_closed_when_the_records_show_it_was_done(s):
    item = _checkin(s)
    turns = [{"id": 5, "time": at(17, day=20), "platform": "telegram",
              "request": "tested the shooter today, it scored 4 of 5"}]
    none = Model({"action": "none", "reason": "they already did it"})
    out = initiative.decide(s, at(19, 5, day=20), asker=none, activity={}, turns=turns)
    assert out[0]["action"] == "none" and out[0]["guard"].startswith("closed:")
    assert "tested the shooter" in out[0]["guard"]
    got = followups.get(item["id"])
    assert got["status"] == "closed" and "tested the shooter" in got["reason"]


def test_evidence_needs_a_statement_not_a_question_and_a_commit_can_be_evidence(s):
    item = _checkin(s)
    asked = [{"id": 6, "time": at(17, day=20), "request": "did you test the shooter yet?"}]
    assert followups.done_evidence(item, at(19, day=20), s, turns=asked, activity={}) == ""
    ev = followups.done_evidence(item, at(19, day=20), s, turns=[], activity={"robot": ["Test the shooter on the field"]})
    assert "robot" in ev


def test_followup_in_quiet_hours_goes_to_the_evening_note_not_nowhere(s):
    item = _checkin(s, at(19, day=20))
    out = initiative.decide(s, at(23, 30, day=20), asker=Model({"action": "text", "message": "How did it go?"}),
                            activity={}, turns=[])
    assert out[0]["action"] == "evening"
    assert followups.get(item["id"])["status"] == "evening"
