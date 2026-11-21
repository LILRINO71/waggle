"""waggle/session_records.py: one row per autonomous run, and the evening line only after 4 useless runs in a row."""
import time

from waggle import autowork, brief, objectives, session_records as sr
from waggle.config import Settings

NOW = time.time()


def add(outcome, n=1, kind="autonomy", **kw):
    for i in range(n):
        sr.record(kind, "g", outcome, now=NOW - 100 + i, **kw)


def test_real_only_when_proven_or_done():
    add("proven", commits=2)
    add("done", kind="objective")
    add("gated")
    rows = sr.recent(3)
    assert [r["work"] for r in rows] == ["noop", "real", "real"] and rows[2]["commits"] == 2


def test_evening_line_needs_four_noops_in_a_row_and_none_real():
    add("gated", 3)
    assert sr.evening_line(NOW) == ""  # only 3 runs
    add("refused")
    line = sr.evening_line(NOW)
    assert "did nothing useful in its last 4 runs" in line and "3 held back by the gate" in line and "1 refused" in line
    add("proven")  # a real run in the last 4 clears it
    assert sr.evening_line(NOW) == ""


def test_stale_streak_is_not_reported():
    add("failed", 4)
    assert sr.evening_line(NOW + 4 * 86400) == ""


def test_off_switch_records_nothing():
    s = Settings()
    s.session_records_enabled = False
    assert sr.record("autonomy", "g", "proven", settings=s) is None
    assert sr.recent() == [] and sr.evening_line(NOW, s) == ""


def test_autowork_run_records_only_when_a_goal_was_picked(monkeypatch):
    results = iter([{"skipped": "autonomy is off"}, {"goal": "money", "outcome": "gated", "skipped": "idle"},
                    {"goal": "money", "outcome": "proven", "why": "file ok", "commits": 1}])
    monkeypatch.setattr(autowork, "_run_once", lambda *a, **k: next(results))
    for _ in range(3):
        autowork.run_once()
    rows = sr.recent(5)
    assert [(r["outcome"], r["work"], r["commits"]) for r in rows] == [("proven", "real", 1), ("gated", "noop", 0)]


def test_objective_step_recorded_but_autonomys_own_are_not(monkeypatch):
    monkeypatch.setattr(objectives, "_run_step", lambda *a, **k: "done")
    monkeypatch.setattr(objectives, "_origin", lambda oid: "autowork" if oid == "mine" else "user")
    objectives.run_step("mine")
    objectives.run_step("o1")
    monkeypatch.setattr(objectives, "_run_step", lambda *a, **k: "waiting_budget")
    objectives.run_step("o2")
    rows = sr.recent(5)
    assert [(r["kind"], r["ref"], r["work"]) for r in rows] == [("objective", "o1", "real")]


def test_evening_note_carries_the_line(settings):
    add("gated", 4)
    text = brief.evening_note(settings, now=NOW, activity={})
    assert "Autonomy did nothing useful in its last 4 runs" in text
