"""Standing goals: store, charter limits, seeds (disabled), the switch and the command line."""
import datetime as dt

import pytest

from waggle import goals

FUTURE = (dt.date.today() + dt.timedelta(days=30)).isoformat()
PAST = (dt.date.today() - dt.timedelta(days=1)).isoformat()


def charter(**kw):
    return {"actions": ["research", "write_note"], "write_roots": ["{vault}/Goals/{goal}"], "channels": [],
            "money": 0, "expires": FUTURE, **kw}


def test_seed_creates_four_disabled_goals_once(settings):
    assert sorted(goals.seed()) == ["ftc-robot", "portfolio", "schoolwork", "waggle"]
    assert goals.seed() == []
    assert {g["status"] for g in goals.all_goals()} == {"disabled"}
    assert goals.enabled_goals() == []
    for g in goals.all_goals():
        assert g["charter"]["money"] == 0 and g["charter"]["channels"] == []
        assert 1 <= len(g["indicators"]) <= 3


def test_autonomy_switch_defaults_off(settings):
    assert goals.autonomy_enabled() is False
    assert goals.set_autonomy(True) is True
    assert goals.set_autonomy(False) is False
    assert goals.max_steps_per_day() == goals.DEFAULT_MAX_STEPS
    assert goals.set_max_steps(5) == 5


def test_charter_refuses_money_unknown_kinds_and_missing_expiry(settings):
    with pytest.raises(goals.CharterError):
        goals.check_charter(charter(money=5))
    with pytest.raises(goals.CharterError):
        goals.check_charter(charter(actions=["buy_things"]))
    with pytest.raises(goals.CharterError):
        goals.check_charter(charter(channels=["carrier_pigeon"]))
    with pytest.raises(goals.CharterError):
        goals.check_charter({**charter(), "expires": None})
    with pytest.raises(goals.CharterError):
        goals.check_charter(charter(write_roots=[]))


def test_charter_allows_checks_action_path_channel_money_and_expiry(settings, tmp_path):
    c = charter()
    inside = tmp_path / "vault" / "Goals" / "g1" / "note.md"
    outside = tmp_path / "elsewhere" / "x.md"
    assert goals.charter_allows(c, action="write_note", path=inside, goal_id="g1", settings=settings)[0]
    assert not goals.charter_allows(c, action="edit_code", goal_id="g1", settings=settings)[0]
    assert not goals.charter_allows(c, path=outside, goal_id="g1", settings=settings)[0]
    assert not goals.charter_allows(c, path=tmp_path / "vault" / "Goals" / "g1" / ".." / ".." / "x.md",
                                    goal_id="g1", settings=settings)[0]
    assert not goals.charter_allows(c, channel="telegram")[0]
    assert not goals.charter_allows(c, money=1)[0]
    assert not goals.charter_allows(charter(expires=PAST), action="research")[0]


def test_enable_disable_and_expiry(settings):
    goals.add("g1", "Goal one", indicators=[{"name": "done", "measure": {"type": "proven_steps"}}],
              charter=charter())
    assert goals.get("g1")["status"] == "disabled"
    goals.enable("g1")
    assert [g["id"] for g in goals.enabled_goals()] == ["g1"]
    goals.disable("g1")
    assert goals.enabled_goals() == []
    goals.add("g2", "Old goal", charter=charter(expires=PAST))
    with pytest.raises(goals.CharterError):
        goals.enable("g2")
    goals.renew("g2", FUTURE)
    goals.enable("g2")
    goals.renew("g2", PAST)
    assert goals.enabled_goals() == [] and goals.get("g2")["status"] == "expired"


def test_indicator_validation(settings):
    with pytest.raises(ValueError):
        goals.add("g3", "Bad", indicators=[{"name": "x", "measure": {"type": "vibes"}}], charter=charter())
    with pytest.raises(ValueError):
        goals.add("g4", "Too many", indicators=[{"name": f"i{i}", "measure": {"type": "proven_steps"}}
                                                for i in range(4)], charter=charter())


def test_command_line(settings, capsys):
    assert goals.main(["seed"]) == 0
    assert goals.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "autonomy: off" in out and "schoolwork" in out and "[disabled]" in out
    assert goals.main(["enable", "schoolwork"]) == 0
    assert goals.get("schoolwork")["status"] == "enabled"
    assert goals.main(["show", "schoolwork"]) == 0
    assert "money=0" in capsys.readouterr().out
    assert goals.main(["disable", "schoolwork"]) == 0
    assert goals.main(["add", "reading", "--title", "Reading", "--action", "research", "--indicator",
                       "done=proven_steps"]) == 0
    assert goals.get("reading")["status"] == "disabled"
    assert goals.main(["autonomy", "on"]) == 0 and goals.autonomy_enabled()
    assert goals.main(["limit", "3"]) == 0 and goals.max_steps_per_day() == 3
    assert goals.main(["enable", "nope"]) == 1
