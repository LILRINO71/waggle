"""Why a look pass decided nothing: every pass records the gate that stopped it, a watcher that fails does not take the
others' findings with it, and a model that was unavailable is retried after 30 minutes, not after the two-hour cadence."""
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

import pytest

from waggle import followups, initiative, tick, vault
from waggle.config import Settings
from waggle.core import jobs, outbox, timers


def at(h, m=0, day=20):
    return dt.datetime(2026, 11, day, h, m).timestamp()


def texts():
    return [r["body"] for r in sorted(outbox.list(limit=1000), key=lambda r: r["id"])]


def say(action, message="Heads up."):
    def asker(prompt, **k):
        asker.calls += 1
        return {"json": {"decisions": [{"key": x, "action": action, "message": message, "reason": "r"}
                                       for x in re.findall(r'"key": "([^"]+)"', prompt)]}}
    asker.calls = 0
    return asker


def down(prompt, **k):
    down.calls += 1
    raise RuntimeError("background budget: 5-hour window at 218%")


down.calls = 0


@pytest.fixture
def s(tmp_path, settings):
    vault.init(settings)
    settings.quiet_hours = [23, 7]
    settings.watch_roots = []
    settings.save()
    s = Settings.load()
    note = "# Upcoming\n- 2026-11-20 14:30 FTC team meeting\n## Mail to answer\n- Coach asked about the roster\n"
    (Path(s.vault) / "Upcoming.md").write_text(note, encoding="utf-8")
    return s


def decide_of(report):
    return report["decide"]


def test_a_pass_with_real_looking_findings_decides_and_says_which_gate_let_it_through(s):
    rep = tick.look(settings=s, now=at(12), activity={}, asker=say("text"))
    w, d = rep["watchers"], decide_of(rep)
    assert w["new"] == 2 and w["ran"]["upcoming"] == 2 and w["found"] == 2
    assert d["ran"] and d["gate"].startswith("1 urgent signal(s)") and d["model"] == "ok"
    assert {a for _, a in d["decisions"]} == {"text"} and len(texts()) == 2


def test_a_pass_that_decides_nothing_names_the_gate(s):
    quiet = tick.look(settings=s, now=at(12), activity={}, asker=say("none"), steps=("watchers",))
    assert quiet["watchers"]["new"] == 2
    tick.look(settings=s, now=at(12, 1), activity={}, asker=say("none"))
    again = tick.look(settings=s, now=at(12, 30), activity={}, asker=say("none"))
    assert decide_of(again) == {"gate": "no pending signals", "pending": 0, "urgent": 0, "ran": False}
    assert again["watchers"]["new"] == 0


def test_quiet_hours_and_cadence_are_named(s):
    (Path(s.vault) / "Upcoming.md").write_text("## Mail to answer\n- Coach asked about the roster\n", encoding="utf-8")
    night = tick.look(settings=s, now=at(23, 30), activity={}, asker=say("text"))
    g = decide_of(night)
    assert not g["ran"] and g["gate"].startswith("quiet hours: 1 non-urgent signal(s) held") and g["pending"] == 1
    assert texts() == []
    morning = tick.look(settings=s, now=at(8, day=21), activity={}, asker=say("none"))
    assert decide_of(morning)["ran"] and "cadence" in decide_of(morning)["gate"]
    initiative.record([{"key": "x", "kind": "mail", "title": "x", "topic": "x", "urgent": False}], at(8, 5, day=21))
    soon = tick.look(settings=s, now=at(8, 10, day=21), activity={}, asker=say("none"))
    assert not decide_of(soon)["ran"] and "cadence: last decision 10 min ago, next in 111 min" == decide_of(soon)["gate"]


def test_the_model_being_unavailable_is_retried_in_thirty_minutes_not_two_hours(s):
    (Path(s.vault) / "Upcoming.md").write_text("## Mail to answer\n- Coach asked about the roster\n", encoding="utf-8")
    down.calls = 0
    first = tick.look(settings=s, now=at(10), activity={}, asker=down)
    d = decide_of(first)
    assert d["ran"] and d["model"].startswith("unavailable: background budget") and d["held"] == 1
    assert d["decisions"] == []
    wait = tick.look(settings=s, now=at(10, 10), activity={}, asker=down)
    assert not decide_of(wait)["ran"] and decide_of(wait)["gate"].startswith("model unavailable (background budget")
    assert "next try in 21 min" in decide_of(wait)["gate"] and down.calls == 1
    retry = tick.look(settings=s, now=at(10, 31), activity={}, asker=say("evening"))
    assert decide_of(retry)["ran"] and decide_of(retry)["model"] == "ok"
    assert [a for _, a in decide_of(retry)["decisions"]] == ["evening"]


def test_with_the_model_down_a_failure_on_todays_project_still_goes_out_and_other_findings_wait(s):
    initiative.record([
        {"key": "ci1", "kind": "github_ci", "title": "robot-code: 'tests' failed on main", "topic": "ci1", "urgent": False},
        {"key": "ci2", "kind": "github_ci", "title": "old-game: 'build' failed on main", "topic": "ci2", "urgent": False},
        {"key": "m1", "kind": "mail", "title": "Newsletter", "topic": "m1", "urgent": False}], at(10))
    rep = tick.look(settings=s, now=at(10, 5), activity={"robot-code": ["Fix the hopper"]}, asker=down,
                    steps=("decide",))
    d = decide_of(rep)
    assert d["decisions"] == [("ci1", "text")] and d["held"] == 2
    assert "robot-code" in texts()[0]
    assert {p["key"] for p in initiative.pending()} == {"ci2", "m1"}


def test_one_failing_watcher_does_not_take_the_others_findings_with_it(s, monkeypatch):
    from waggle import watchers

    def boom(settings, now=None):
        raise OSError("objectives db locked")
    monkeypatch.setattr(watchers, "work_watch", boom)
    rep = tick.look(settings=s, now=at(12), activity={}, asker=say("none"), steps=("watchers",))
    w = rep["watchers"]
    assert w["ran"]["work"].startswith("error: OSError") and w["ran"]["upcoming"] == 2 and w["new"] == 2
    assert {p["kind"] for p in initiative.pending()} == {"upcoming", "mail"}


def test_a_checkin_is_decided_when_its_time_comes_not_at_the_next_two_hourly_run(s):
    tick.register_jobs()
    tick.look(settings=s, now=at(10), activity={}, asker=say("none"))  # uses up the cadence slot
    said = dt.datetime(2026, 11, 19, 16, 0)
    rows = [{"id": 1, "time": said.timestamp(), "platform": "telegram", "request": "I'll test the shooter tomorrow"}]
    followups.capture(s, said.timestamp(), rows=rows)
    timers.fire_due(at(19, 1))
    assert jobs.run_one(kinds=["outreach.checkin"], now=at(19, 1))["state"] == "done"
    g = initiative.gate(s, at(19, 2))
    assert g["due"] and g["gate"] == "a check-in's time has come: decided at once"
