"""Persistent objectives: plan, dependency order, verification, retry/block, approvals, crash recovery, report."""
import json

import pytest

from waggle import agent, notify, notifycheck, objectives, policy, vault


def _planner(steps):
    return lambda *a, **k: {"json": {"title": "Internship hunt", "steps": steps}}


STEPS = [
    {"title": "Find internships", "detail": "search", "agent": "researcher", "done_when": "3 listed", "depends_on": []},
    {"title": "Check deadlines", "detail": "look up", "agent": "general", "done_when": "dates", "depends_on": [1]},
    {"title": "Write summary", "detail": "vault note", "agent": "planner", "done_when": "note", "depends_on": [1, 2]},
]


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(objectives.quota, "can_escalate", lambda m, s: (True, ""))
    monkeypatch.setattr(objectives, "kick", lambda oid: None)
    monkeypatch.setattr(objectives.claude_cli, "ask", lambda *a, **k: {"text": "All three found; summary saved."})
    # a finished research objective asks a local yes/no before it messages (waggle/notifycheck.py): yes here
    monkeypatch.setattr(notifycheck, "_ask", lambda m, s, t: {"decision": "yes", "reason": "test"})


def _runner(log, text="did it", approvals=None, ok=True):
    def run(agent_name, prompt, **k):
        log.append((agent_name, prompt))
        return agent.AgentResult(text=text, ok=ok, approvals=approvals or [])
    return run


def test_plan_and_run_in_dependency_order(settings, quiet):
    oid = objectives.create("Find 3 AI internships with deadlines", asker=_planner(STEPS), settings=settings)
    st = objectives.status(oid)
    assert st["status"] == "active" and st["title"] == "Internship hunt" and len(st["steps"]) == 3
    ran = []
    out = objectives.work(oid, settings=settings, run_fn=_runner(ran), verify_fn=lambda *a, **k: (True, ""))
    assert out == "done"
    assert [a for a, _ in ran] == ["researcher", "general", "planner"]
    assert "RESULTS YOU BUILD ON" in ran[2][1] and "Result of step 2" in ran[2][1]  # dependency results passed on
    final = objectives.status(oid)
    assert final["status"] == "done" and final["report"].startswith("All three")
    assert "Internship hunt" in vault.read(final["note_path"])
    assert any("Objective done" in m for m in notify.PHONE_LOG)


def test_bad_dependencies_are_sanitised(settings, quiet):
    steps = [dict(STEPS[0], depends_on=[5, 1]), dict(STEPS[1], depends_on=[2, 7])]  # self/forward/unknown refs
    oid = objectives.create("x" * 20, asker=_planner(steps), settings=settings)
    deps = [json.loads(s["depends"]) for s in objectives._db().execute(
        "SELECT depends FROM steps WHERE objective_id=? ORDER BY idx", (oid,))]
    assert deps == [[], []]


def test_incomplete_step_retries_then_blocks(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner(STEPS[:1]), settings=settings)
    ran = []
    assert objectives.run_step(oid, settings=settings, run_fn=_runner(ran),
                               verify_fn=lambda *a, **k: (False, "only 1 of 3 listed")) == "retry"
    assert objectives.run_step(oid, settings=settings, run_fn=_runner(ran),
                               verify_fn=lambda *a, **k: (False, "still 1")) == "blocked"
    assert "only 1 of 3 listed" in ran[1][1]  # the retry was told what was missing
    st = objectives.status(oid)
    assert st["status"] == "blocked" and "step 1 failed" in st["error"]
    objectives.set_status(oid, "active")  # user says retry
    assert objectives.status(oid)["steps"][0]["status"] == "pending"


def test_approval_pauses_and_yes_resumes(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner(STEPS[:1]), settings=settings)
    appr = [{"reason": "deleting files", "id": "abc", "session": f"objective-{oid}"}]
    assert objectives.run_step(oid, settings=settings, run_fn=_runner([], approvals=appr)) == "waiting_approval"
    assert objectives.status(oid)["status"] == "waiting_approval"
    assert objectives.resume_after_approval(f"objective-{oid}") == oid
    assert objectives.status(oid)["status"] == "active"


def test_crash_recovery_requeues_running_step(settings, quiet, monkeypatch):
    oid = objectives.create("goal goal goal", asker=_planner(STEPS[:2]), settings=settings)
    con = objectives._db()
    objectives._set_step(con, oid, 1, status="running", started=1.0)
    objectives._set(con, oid, worker_pid=999999)
    con.commit()
    con.close()
    monkeypatch.setattr(objectives, "_alive", lambda pid: False)
    kicked = []
    assert objectives.supervise(kick_fn=kicked.append) == [oid]
    assert objectives.status(oid)["steps"][0]["status"] == "pending"


def test_agent_failure_and_run_budget(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner(STEPS[:1]), settings=settings)
    assert objectives.run_step(oid, settings=settings, run_fn=_runner([], text="", ok=False)) == "retry"
    con = objectives._db()
    objectives._set(con, oid, runs=20)
    con.commit()
    con.close()
    assert objectives.run_step(oid, settings=settings, run_fn=_runner([])) == "blocked"
    assert "step runs" in objectives.status(oid)["error"]


def test_planning_failure_and_budget_wait(settings, monkeypatch):
    monkeypatch.setattr(objectives.quota, "can_escalate", lambda m, s: (False, "budget used"))
    oid = objectives.create("goal goal goal", asker=lambda *a, **k: 1 / 0, settings=settings)
    assert objectives.status(oid)["status"] == "planning" and "budget" in objectives.status(oid)["error"]
    monkeypatch.setattr(objectives.quota, "can_escalate", lambda m, s: (True, ""))
    assert not objectives.plan(oid, asker=lambda *a, **k: {"json": {"steps": []}}, settings=settings)
    assert objectives.status(oid)["status"] == "failed"


def test_listing_find_and_cancel(settings, quiet):
    oid = objectives.create("Plan the robot team fundraiser", asker=_planner(STEPS[:1]), settings=settings)
    assert objectives.find("Internship") == oid  # title words
    assert objectives.status()[0]["id"] == oid
    objectives.set_status(oid, "cancelled")
    assert objectives.run_step(oid, settings=settings, run_fn=_runner([])) is None


def test_background_approvals_live_longer():
    d = policy.check("Bash", {"command": "rm x"}, session="objective-abc")
    item = policy.get(d.approval_id)
    assert item["ttl"] == policy.BACKGROUND_TTL and len(item["code"]) == 4
