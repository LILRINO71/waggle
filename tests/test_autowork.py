"""The work loop on a toy repository in a temp home, with a fake model and a fake agent: a proven step moves the
indicator; a step that claims success without evidence does not count; the allowance and the charter stop it."""
import datetime as dt
import subprocess
import sys

import pytest

from waggle import agent, autowork, budget, goals, initiative, objectives

FUTURE = (dt.date.today() + dt.timedelta(days=30)).isoformat()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, creationflags=NO_WINDOW)


@pytest.fixture
def toy(settings, tmp_path, monkeypatch):
    repo = tmp_path / "toy"
    repo.mkdir()
    (repo / "calc.py").write_text(BUGGY)
    (repo / "test_calc.py").write_text(TEST)
    git(repo, "init", "-q")
    git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A")
    git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "start")
    monkeypatch.setattr(objectives.quota, "can_escalate", lambda m, s: (True, ""))
    monkeypatch.setattr(objectives, "kick", lambda oid, user=True: None)
    goals.add("toy", "Make the failing test pass", "the toy repo's tests must pass",
              indicators=[{"name": "failing tests", "measure": {"type": "tests_failing", "cmd": PYTEST,
                                                               "cwd": str(repo)}},
                          {"name": "proven steps", "measure": {"type": "proven_steps"}}],
              charter={"actions": ["edit_code", "run_tests", "commit_local"], "write_roots": [str(repo)],
                       "channels": [], "money": 0, "expires": FUTURE})
    goals.enable("toy")
    goals.set_autonomy(True)
    return repo


def model(repo, **over):
    prop = {"action": "edit_code", "title": "Fix add()", "instructions": "Make test_calc pass.",
            "workdir": str(repo),
            "evidence": [{"type": "tests_pass", "cmd": PYTEST, "cwd": str(repo)},
                         {"type": "file_exists", "path": str(repo / "calc.py"), "contains": "a + b"}], **over}
    calls = []

    def ask(prompt, **k):
        calls.append(prompt)
        return {"json": prop, "text": ""}
    ask.calls = calls
    return ask


def agent_that(effect=None, text="Fixed it: all tests pass now."):
    runs = []

    def run(name, prompt, **k):
        runs.append((name, prompt))
        if effect:
            effect()
        return agent.AgentResult(text=text, ok=True)
    run.runs = runs
    return run


def test_a_proven_step_moves_the_indicator(toy, settings):
    autowork.measure_all(goals.get("toy"), settings)  # baseline: one failing test
    run = agent_that(lambda: (toy / "calc.py").write_text(FIXED))
    out = autowork.run_once(settings, asker=model(toy), run_fn=run)
    assert out["outcome"] == "proven", out
    assert run.runs and run.runs[0][0] == "coder"
    assert out["indicators"]["failing tests"] == 0.0 and out["indicators"]["proven steps"] == 1.0
    assert objectives.status(out["objective"])["status"] == "done"
    rows = autowork.steps("toy")
    assert rows[-1]["outcome"] == "proven" and all(c["ok"] for c in rows[-1]["evidence"])
    line = autowork.report(settings)["lines"][0]
    assert "1 proven" in line and "failing tests 1->0" in line


def test_a_claim_without_evidence_does_not_count(toy, settings, monkeypatch):
    from waggle import notify
    autowork.measure_all(goals.get("toy"), settings)
    run = agent_that(None, text="Done! I fixed add() and every test passes.")  # changes nothing
    out = autowork.run_once(settings, asker=model(toy), run_fn=run)
    assert out["outcome"] == "unproven"
    assert "tests_pass: exit 1" in out["why"]
    assert out["indicators"]["failing tests"] == 1.0 and out["indicators"]["proven steps"] == 0.0
    st = objectives.status(out["objective"])
    assert st["status"] == "unproven"
    assert objectives.supervise(kick_fn=lambda o: pytest.fail("an unproven objective must not resume")) == []
    assert len(notify.PHONE_LOG) == 0  # the loop never messages the owner by itself
    assert "0 proven, 1 unproven" in autowork.report(settings)["lines"][0]


def test_a_file_written_before_the_step_is_not_evidence(toy, settings):
    import os
    import time
    os.utime(toy / "calc.py", (time.time() - 3600, time.time() - 3600))
    ask = model(toy, evidence=[{"type": "file_exists", "path": str(toy / "calc.py")}])
    out = autowork.run_once(settings, asker=ask, run_fn=agent_that(None))
    assert out["outcome"] == "unproven" and "not written during this step" in out["why"]


def test_the_allowance_refusing_stops_the_loop(toy, settings, monkeypatch):
    def no_room(*a, **k):
        raise budget.Deferred("background budget used: 4.00 of 4.00 units today", 0)
    monkeypatch.setattr(budget, "reserve", no_room)
    ask, run = model(toy), agent_that(lambda: pytest.fail("must not run"))
    out = autowork.run_once(settings, asker=ask, run_fn=run)
    assert out["outcome"] == "refused" and "4.00 of 4.00" in out["why"]
    assert ask.calls == [] and run.runs == []
    assert autowork.steps("toy")[-1]["outcome"] == "refused"
    assert autowork.steps_today(settings=settings) == 0  # a refusal does not use up a step


def test_a_proposal_outside_the_charter_never_runs(toy, settings, tmp_path):
    run = agent_that(lambda: pytest.fail("must not run"))
    out = autowork.run_once(settings, asker=model(toy, workdir=str(tmp_path / "elsewhere")), run_fn=run)
    assert out["outcome"] == "charter_refused" and "outside" in out["why"]
    out = autowork.run_once(settings, asker=model(toy, action="write_note"), run_fn=run)
    assert out["outcome"] == "charter_refused"
    bad_cmd = [{"type": "tests_pass", "cmd": ["bash", "-c", "rm -rf /"], "cwd": str(toy)}]
    goals.set_max_steps(5)
    out = autowork.run_once(settings, asker=model(toy, evidence=bad_cmd), run_fn=run)
    assert out["outcome"] == "charter_refused" and "not allowed" in out["why"]
    assert run.runs == []


def test_switch_daily_limit_and_disabled_goals(toy, settings):
    goals.set_autonomy(False)
    assert "autonomy is off" in autowork.run_once(settings, asker=model(toy))["skipped"]
    goals.set_autonomy(True)
    goals.set_max_steps(1)
    autowork.run_once(settings, asker=model(toy), run_fn=agent_that(None))
    assert "1 of 1 steps" in autowork.run_once(settings, asker=model(toy))["skipped"]
    goals.set_max_steps(3)
    goals.disable("toy")
    assert autowork.run_once(settings, asker=model(toy))["skipped"] == "no enabled goal"


def test_the_evening_note_lists_one_line_per_goal(toy, settings):
    from waggle import brief
    autowork.run_once(settings, asker=model(toy), run_fn=agent_that(lambda: (toy / "calc.py").write_text(FIXED)))
    autowork.report(settings)
    note = brief.evening_note(settings)
    assert "Goal Make the failing test pass: 1 step (1 proven)" in note


def test_measures_by_code(toy, settings):
    g = goals.get("toy")
    value, summary = autowork.measure(g, g["indicators"][0], settings)
    assert value == 1.0 and "1 failed" in summary
    (toy / "calc.py").write_text(FIXED)
    assert autowork.measure(g, g["indicators"][0], settings)[0] == 0.0
    assert autowork.run_tests(["bash", "-c", "echo hi"], toy)[0] is None
    assert autowork._allowed_test_cmd(["python", "-m", "pytest"])[0] == sys.executable
