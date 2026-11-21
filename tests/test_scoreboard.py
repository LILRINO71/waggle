"""Scoreboard: weekly counts per goal from the step records and indicator readings."""
import datetime as dt
import time

from waggle import autonomy_store, autowork, goals, scoreboard

FUTURE = (dt.date.today() + dt.timedelta(days=30)).isoformat()
NOW = time.time()


def reading(goal_id, name, t, value):
    con = autonomy_store.db()
    con.execute("INSERT INTO readings VALUES (?,?,?,?,?)", (goal_id, name, t, value, ""))
    con.commit()
    con.close()


def test_weekly_counts_outcomes_units_and_indicator_change(settings, capsys):
    goals.add("g1", "Fix the tests", indicators=[{"name": "failing tests", "measure": {"type": "proven_steps"}}],
              charter={"actions": ["research"], "write_roots": [], "channels": [], "money": 0, "expires": FUTURE})
    for outcome, units, age in (("proven", 0.1, 1), ("unproven", 0.2, 2), ("failed", 0.05, 3),
                                ("charter_refused", 0.01, 3), ("refused", 0, 3), ("proven", 0.3, 10)):
        autowork.record_step("g1", outcome, units=units, now=NOW - age * 86400, settings=settings)
    reading("g1", "failing tests", NOW - 9 * 86400, 5)
    reading("g1", "failing tests", NOW - 2 * 86400, 3)
    reading("g1", "failing tests", NOW - 1 * 86400, 1)
    data = scoreboard.weekly(NOW)
    g = data["goals"][0]
    assert (g["attempted"], g["proven"], g["unproven"], g["failed"], g["charter_refused"], g["refused"]) == \
        (4, 1, 1, 1, 1, 1)
    assert abs(g["units"] - 0.36) < 1e-9  # the 10-day-old step is outside the week
    ind = g["indicators"][0]
    assert (ind["start"], ind["latest"], ind["change"]) == (5, 1, -4)
    assert scoreboard.main([]) == 0
    out = capsys.readouterr().out
    assert "Fix the tests" in out and "5 -> 1" in out and "(-4)" in out


def test_empty_scoreboard(settings, capsys):
    assert scoreboard.weekly()["goals"] == []
    scoreboard.main([])
    assert "no goals yet" in capsys.readouterr().out
