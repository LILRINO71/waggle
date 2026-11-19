"""Mission 3: background budget, objective reliability under crashes/duplicates, and approval safety."""
import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import pytest

from waggle import agent, budget, notify, objectives, policy, quota


def _planner(steps):
    return lambda *a, **k: {"json": {"title": "Trust test", "steps": steps}}


STEP = {"title": "Do it", "detail": "x", "agent": "general", "done_when": "done", "depends_on": []}
DEEP = {"title": "Research", "detail": "x", "agent": "researcher", "done_when": "done", "depends_on": []}


def _ok_runner(log, text="did it", **kw):
    def run(agent_name, prompt, **k):
        log.append((agent_name, prompt, k.get("model")))
        return agent.AgentResult(text=text, ok=True, usage={"input_tokens": 1000, "output_tokens": 200}, **kw)
    return run


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(objectives, "kick", lambda oid: None)
    monkeypatch.setattr(objectives.claude_cli, "ask", lambda *a, **k: {"text": "Report: all done."})


# ---- budget ----------------------------------------------------------------------------------------

def test_background_and_interactive_are_counted_separately(settings):
    quota.record("claude-haiku-4-5", {"input_tokens": 10000, "output_tokens": 1000}, "route")
    with budget.background("objective", objective="ab12", step=1):
        quota.record("claude-sonnet-5-5", {"input_tokens": 20000, "output_tokens": 2000}, "objective")
    st = budget.status(settings)
    assert st["interactive_units"] > 0 and st["background_units"] > 0
    assert st["by_objective"][0]["objective"] == "ab12"
    rows = quota.ledger()
    assert [r["kind"] for r in rows] == ["interactive", "background"] and rows[1]["objective"] == "ab12"


def test_unmeasured_failure_is_an_estimate_and_auth_errors_cost_nothing(settings):
    with budget.background("objective"):
        budget.record("claude-sonnet-5-5", {}, "objective", ok=False, error_kind="timeout")
        budget.record("claude-sonnet-5-5", {}, "objective", ok=False, error_kind="auth")
    con = budget._db()
    rows = con.execute("SELECT units, measured FROM usage ORDER BY time").fetchall()
    assert rows[0]["units"] > 0 and rows[0]["measured"] == 0  # flagged as an estimate
    assert rows[1]["units"] == 0
    assert budget.status(settings)["estimated_rows"] == 1


def test_exhausted_budget_defers_until_reset_and_logs_why(settings):
    settings.background_daily_units = 0.1
    settings.save()
    with budget.background("objective"):
        budget.record("claude-sonnet-5-5", {"input_tokens": 50000, "output_tokens": 5000}, "objective")
    with pytest.raises(budget.Deferred) as e:
        budget.reserve("sonnet", "objective", "ob1", 1, settings, window_used=0)
    assert "background budget used" in e.value.reason
    assert e.value.until == budget.next_reset(settings=settings)
    assert budget.status(settings)["decisions"][0]["action"] == "defer"
    # next budget day: spent resets to zero, work may start again
    tomorrow = time.time() + 86400 + 3600
    ok, why, _ = budget.check("haiku", "objective", settings, now=tomorrow, window_used=0)
    assert ok, why


def test_reset_hour_defines_the_budget_day(settings):
    import datetime as dt
    settings.background_reset_hour = 4
    at_3am = dt.datetime(2026, 11, 20, 3, 0).timestamp()
    at_5am = dt.datetime(2026, 11, 20, 5, 0).timestamp()
    assert budget.day_key(at_3am, settings) == "2026-11-19" and budget.day_key(at_5am, settings) == "2026-11-20"
    assert dt.datetime.fromtimestamp(budget.next_reset(at_3am, settings)).hour == 4


def test_concurrent_workers_cannot_both_take_the_last_slot(settings):
    settings.background_daily_units = 0.25  # room for one Sonnet agent step (~0.2), not two
    settings.background_max_workers = 5
    settings.save()
    got, deferred = [], []

    def worker():
        try:
            got.append(budget.reserve("sonnet", "objective", None, None, settings, window_used=0))
        except budget.Deferred as e:
            deferred.append(e.reason)
    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(got) == 1 and len(deferred) == 5


def test_max_workers_and_reservation_survive_restart(settings, monkeypatch):
    settings.background_max_workers = 1
    settings.save()
    t1 = budget.reserve("haiku", "objective", None, None, settings, window_used=0)
    con = budget._db()
    con.execute("UPDATE reservations SET pid=? WHERE id=?", (424242, t1))  # held by another (live) worker
    con.close()
    monkeypatch.setattr(budget, "_alive", lambda pid: True)
    with pytest.raises(budget.Deferred, match="already running"):
        budget.reserve("haiku", "objective", None, None, settings, window_used=0)
    monkeypatch.setattr(budget, "_alive", lambda pid: False)  # that worker died (crash / reboot)
    assert budget.reserve("haiku", "objective", None, None, settings, window_used=0)


def test_background_yields_to_chat_and_to_the_window(settings):
    budget.note_interactive()
    ok, why, until = budget.check("haiku", "routine", settings, window_used=0)
    assert not ok and "chatting" in why and until > time.time()
    budget.note_interactive(time.time() - 3600)
    ok, why, _ = budget.check("haiku", "routine", settings, window_used=0.7 * settings.window_units_estimate)
    assert not ok and "chat keeps the rest" in why


def test_interactive_calls_are_never_blocked_by_the_background_budget(settings):
    settings.background_daily_units = 0
    settings.save()
    budget.guard("claude-sonnet-5-5")  # interactive context: no exception
    with budget.background("routine"):
        with pytest.raises(budget.Deferred, match="switched off"):
            budget.guard("claude-sonnet-5-5")


def test_backstop_in_claude_cli_and_agent_runner(settings, monkeypatch):
    from waggle import claude_cli
    settings.background_daily_units = 0
    settings.save()
    monkeypatch.setattr(claude_cli, "find_claude", lambda: "claude.exe")
    with budget.background("proactive"):
        with pytest.raises(claude_cli.ClaudeError) as e:
            claude_cli.ask("hi", model="haiku")
        assert e.value.kind == "budget"
        res = agent.run("check repos", model="haiku", runner=lambda *a: pytest.fail("must not start"))
        assert res.deferred and "background budget" in res.error


def test_low_budget_downgrades_non_coding_steps_and_logs_it(settings):
    settings.background_daily_units = 1.0
    settings.save()
    with budget.background("objective"):
        budget.record("claude-sonnet-5-5", {"input_tokens": 100000, "output_tokens": 140000}, "objective")
    assert budget.remaining(settings) < 0.25
    assert budget.choose_model("sonnet", "researcher", settings) == settings.agent_model
    assert budget.choose_model("sonnet", "coder", settings) == "sonnet"  # coding defers instead of degrading
    assert budget.status(settings)["decisions"][0]["action"] == "downgrade"


def test_limit_is_configurable_without_code(settings):
    from waggle import cli
    cli.main(["budget", "set", "7.5"])
    assert budget.status()["limit_units"] == 7.5


# ---- objectives under budget ------------------------------------------------------------------------

def test_objective_parks_on_exhausted_budget_and_resumes_after_reset(settings, quiet, monkeypatch):
    settings.background_daily_units = 0.01
    settings.save()
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    ran = []
    assert objectives.run_step(oid, settings=settings, run_fn=_ok_runner(ran)) == "waiting_budget"
    st = objectives.status(oid)
    assert ran == [] and st["status"] == "active" and st["deferred_until"] > time.time()
    assert st["steps"][0]["status"] == "pending" and st["steps"][0]["attempts"] == 0
    kicked = []
    assert objectives.supervise(kick_fn=kicked.append) == []  # parked: no worker spawned every 5 minutes
    assert objectives.supervise(kick_fn=kicked.append, now=st["deferred_until"] + 1) == [oid]  # after the reset
    settings.background_daily_units = 5
    settings.save()
    con = objectives._db()
    objectives._set(con, oid, deferred_until=None)
    con.commit()
    out = objectives.work(oid, settings=settings, run_fn=_ok_runner(ran), verify_fn=lambda *a, **k: (True, ""))
    assert out == "done" and objectives.status(oid)["status"] == "done"
    assert budget.status(settings)["running_calls"] == 0  # reservation released


def test_step_usage_is_tagged_with_the_objective(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)

    def run(agent_name, prompt, **k):
        quota.record("claude-haiku-4-5", {"input_tokens": 5000, "output_tokens": 500}, "objective")
        return agent.AgentResult(text="ok", ok=True)
    objectives.run_step(oid, settings=settings, run_fn=run, verify_fn=lambda *a, **k: (True, ""))
    by = budget.status(settings)["by_objective"]
    assert by and by[0]["objective"] == oid


# ---- objective reliability --------------------------------------------------------------------------

def test_only_one_worker_can_hold_an_objective(settings, quiet, monkeypatch):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    con = objectives._db()
    objectives._set(con, oid, worker_pid=999999, lease_until=time.time() + 600)
    con.commit()
    monkeypatch.setattr(objectives, "_alive", lambda pid: True)
    assert objectives.work(oid, settings=settings, run_fn=_ok_runner([])) == "busy"
    assert objectives.supervise(kick_fn=lambda o: pytest.fail("duplicate worker")) == []
    # an expired lease (worker hung, or the PID was reused after a reboot) can be taken over
    objectives._set(con, oid, lease_until=time.time() - 1)
    con.commit()
    con.close()
    assert objectives.claim(oid)


def test_a_step_is_claimed_once(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    con = objectives._db()
    objectives._set_step(con, oid, 1, status="running", started=time.time())  # another worker just took it
    con.commit()
    con.close()
    assert objectives.run_step(oid, settings=settings, run_fn=_ok_runner([])) is None


def test_crash_after_work_never_reruns_it(settings, quiet, monkeypatch):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    ran = []

    def crash_in_verify(*a, **k):
        raise KeyboardInterrupt("worker killed during verification")
    with pytest.raises(KeyboardInterrupt):
        objectives.run_step(oid, settings=settings, run_fn=_ok_runner(ran, text="sent the report"),
                            verify_fn=crash_in_verify)
    assert objectives.status(oid)["steps"][0]["status"] == "verifying"
    monkeypatch.setattr(objectives, "_alive", lambda pid: False)
    objectives.supervise(kick_fn=lambda o: None)
    assert objectives.status(oid)["steps"][0]["status"] == "verifying"  # not re-queued: the work is saved
    assert objectives.run_step(oid, settings=settings, run_fn=_ok_runner(ran),
                               verify_fn=lambda *a, **k: (True, "")) == "done"
    assert len(ran) == 1  # the agent ran exactly once
    assert objectives.status(oid)["status"] == "done"


def test_unavailable_check_waits_instead_of_passing(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    assert objectives.run_step(oid, settings=settings, run_fn=_ok_runner([]),
                               verify_fn=lambda *a, **k: (None, "Claude limit")) == "waiting_budget"
    st = objectives.status(oid)
    assert st["steps"][0]["status"] == "verifying" and st["status"] == "active" and "verification" in st["defer_reason"]


def test_strict_verify_never_passes_on_error(settings):
    from waggle import agents
    assert agents.verify("t", "r", asker=lambda *a, **k: 1 / 0, strict=True)[0] is None
    assert agents.verify("t", "r", asker=lambda *a, **k: {"json": {}}, strict=True)[0] is None
    assert agents.verify("t", "r", asker=lambda *a, **k: 1 / 0)[0] is True  # lenient mode kept for routines


def test_interrupted_step_is_told_and_eventually_blocks(settings, quiet, monkeypatch):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    monkeypatch.setattr(objectives, "_alive", lambda pid: False)
    for i in range(objectives.MAX_INTERRUPTS - 1):
        con = objectives._db()
        objectives._set_step(con, oid, 1, status="running", started=1.0, attempts=1)
        con.commit()
        con.close()
        objectives.supervise(kick_fn=lambda o: None)
        step = objectives.status(oid)["steps"][0]
        assert step["status"] == "pending" and step["attempts"] == 0  # an interruption is not a failed attempt
    ran = []
    objectives.run_step(oid, settings=settings, run_fn=_ok_runner(ran), verify_fn=lambda *a, **k: (False, "no"))
    assert "interrupted before it finished" in ran[0][1]
    con = objectives._db()
    objectives._set_step(con, oid, 1, status="running", started=1.0)
    con.commit()
    con.close()
    objectives.supervise(kick_fn=lambda o: None)
    st = objectives.status(oid)
    assert st["status"] == "blocked" and "interrupted" in st["error"]


def test_finish_reports_exactly_once(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    objectives.work(oid, settings=settings, run_fn=_ok_runner([]), verify_fn=lambda *a, **k: (True, ""))
    sent = len([m for m in notify.PHONE_LOG if "Objective done" in m])
    objectives.finish(oid, settings=settings)
    assert len([m for m in notify.PHONE_LOG if "Objective done" in m]) == sent == 1


def test_done_and_cancelled_objectives_cannot_run_again(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    objectives.work(oid, settings=settings, run_fn=_ok_runner([]), verify_fn=lambda *a, **k: (True, ""))
    assert objectives.set_status(oid, "active") is None and objectives.status(oid)["status"] == "done"
    oid2 = objectives.create("other goal here", asker=_planner([STEP]), settings=settings)
    objectives.set_status(oid2, "cancelled")
    assert objectives.set_status(oid2, "active") is None
    assert objectives.run_step(oid2, settings=settings, run_fn=_ok_runner([])) is None


def test_failed_planning_replans_on_retry(settings, quiet):
    oid = objectives.create("goal goal goal", asker=lambda *a, **k: {"json": {"steps": []}}, settings=settings)
    assert objectives.status(oid)["status"] == "failed"
    assert objectives.set_status(oid, "active") == oid and objectives.status(oid)["status"] == "planning"


def test_budget_error_during_planning_waits_instead_of_failing(settings, quiet):
    from waggle import claude_cli

    def asker(*a, **k):
        raise claude_cli.ClaudeError("background budget: used", "budget")
    oid = objectives.create("goal goal goal", asker=asker, settings=settings)
    st = objectives.status(oid)
    assert st["status"] == "planning" and st["deferred_until"] > time.time()


def test_old_database_is_migrated(settings):
    import sqlite3
    from waggle.config import data_path
    con = sqlite3.connect(data_path("objectives.db"))
    con.executescript("""CREATE TABLE objectives (id TEXT PRIMARY KEY, title TEXT, goal TEXT, project TEXT,
        status TEXT, created REAL, updated REAL, report TEXT, error TEXT, runs INTEGER DEFAULT 0,
        max_runs INTEGER DEFAULT 20, origin TEXT, worker_pid INTEGER, note_path TEXT);
        CREATE TABLE steps (objective_id TEXT, idx INTEGER, title TEXT, detail TEXT, agent TEXT, depends TEXT,
        done_when TEXT, status TEXT, attempts INTEGER DEFAULT 0, result TEXT, verify TEXT, started REAL,
        finished REAL, PRIMARY KEY (objective_id, idx));""")
    con.close()
    cols = {r[1] for r in objectives._db().execute("PRAGMA table_info(steps)")}
    assert {"interrupts", "approved"} <= cols


def test_vault_writes_are_atomic(settings, monkeypatch):
    from waggle import vault
    vault.write("Objectives/x", "first")
    real = os.replace

    def boom(a, b):
        raise OSError("disk gone")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        vault.write("Objectives/x", "second")
    monkeypatch.setattr(os, "replace", real)
    assert vault.read("Objectives/x") == "first"


# ---- approvals --------------------------------------------------------------------------------------

def test_approval_is_bound_to_its_session_and_used_once():
    d = policy.check("Bash", {"command": "rm a.txt"}, session="objective-aaa")
    other = policy.check("Bash", {"command": "rm a.txt"}, session="objective-bbb")
    assert d.approval_id != other.approval_id
    code = policy.get(d.approval_id)["code"]
    assert policy.grant(code=code)
    assert policy.check("Bash", {"command": "rm a.txt"}, session="objective-bbb").action == "ask"  # not unlocked
    assert policy.check("Bash", {"command": "rm a.txt"}, session="objective-aaa").allowed
    assert policy.check("Bash", {"command": "rm a.txt"}, session="objective-aaa").action == "ask"  # used up
    assert policy.grant(code=code) == []  # a used code can't be granted again


def test_expired_and_denied_approvals_cannot_be_granted(monkeypatch):
    d = policy.check("Bash", {"command": "rm z"}, session="chat-1")
    item = policy.get(d.approval_id)
    real = time.time
    monkeypatch.setattr(policy.time, "time", lambda: real() + policy.APPROVAL_TTL + 5)
    assert policy.pending() == [] and policy.grant(code=item["code"]) == []
    monkeypatch.setattr(policy.time, "time", real)
    d2 = policy.check("Bash", {"command": "rm y"}, session="chat-1")
    c2 = policy.get(d2.approval_id)["code"]
    assert policy.deny(code=c2) and policy.grant(code=c2) == []


def test_session_wide_grant_needs_a_session():
    policy.check("Bash", {"command": "rm q"}, session="objective-x")
    assert policy.grant() == []  # never "grant everything everywhere"
    assert len(policy.pending()) == 1


def test_repeat_request_keeps_its_code_and_old_store_is_migrated(settings):
    from waggle.config import data_path
    a = policy.check("Bash", {"command": "rm k"}, session="s9")
    b = policy.check("Bash", {"command": "rm k"}, session="s9")
    assert policy.get(a.approval_id)["code"] == policy.get(b.approval_id)["code"]
    data_path("approvals.db").unlink()
    data_path("approvals.json").write_text(json.dumps({"old1": {"tool": "Bash", "summary": "rm old", "reason": "r",
                                                                "created": time.time(), "session": "objective-o",
                                                                "granted": False, "ttl": 3600}}))
    assert [p["id"] for p in policy.pending()] == ["old1"]
    assert not data_path("approvals.json").exists()


def test_approval_message_names_objective_step_action_and_code(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    d = policy.check("Bash", {"command": "rm report.txt"}, session=f"objective-{oid}")
    appr = policy.pending(f"objective-{oid}")
    assert objectives.run_step(oid, settings=settings, run_fn=_ok_runner([], approvals=appr)) == "waiting_approval"
    msg = notify.PHONE_LOG[-1]
    code = policy.get(d.approval_id)["code"]
    assert oid in msg and "step 1" in msg and f"approve {code}" in msg and "rm report.txt" in msg


def test_two_objectives_waiting_and_denial_and_expiry(settings, quiet, monkeypatch):
    o1 = objectives.create("first goal here", asker=_planner([STEP]), settings=settings)
    o2 = objectives.create("second goal here", asker=_planner([STEP]), settings=settings)
    for o, cmd in ((o1, "rm one"), (o2, "rm two")):
        policy.check("Bash", {"command": cmd}, session=f"objective-{o}")
        objectives.run_step(o, settings=settings, run_fn=_ok_runner([], approvals=policy.pending(f"objective-{o}")))
    assert {objectives.status(o)["status"] for o in (o1, o2)} == {"waiting_approval"}
    c2 = policy.pending(f"objective-{o2}")[0]["code"]
    policy.deny(code=c2)
    objectives.settle_approvals()
    assert objectives.status(o2)["status"] == "blocked" and "said no" in objectives.status(o2)["error"]
    assert objectives.status(o1)["status"] == "waiting_approval"  # the other one is untouched
    real = time.time
    later = real() + policy.BACKGROUND_TTL + 10
    monkeypatch.setattr(policy.time, "time", lambda: later)
    objectives.settle_approvals(now=later)
    assert objectives.status(o1)["status"] == "blocked" and "expired" in objectives.status(o1)["error"]


def test_granted_but_lost_resume_is_healed(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    policy.check("Bash", {"command": "rm w"}, session=f"objective-{oid}")
    objectives.run_step(oid, settings=settings, run_fn=_ok_runner([], approvals=policy.pending(f"objective-{oid}")))
    policy.grant(code=policy.pending(f"objective-{oid}")[0]["code"])  # gateway died before resume_after_approval
    objectives.supervise(kick_fn=lambda o: None)
    assert objectives.status(oid)["status"] == "active"


def test_resume_tells_the_rerun_what_was_approved(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    policy.check("Bash", {"command": "rm v"}, session=f"objective-{oid}")
    objectives.run_step(oid, settings=settings, run_fn=_ok_runner([], approvals=policy.pending(f"objective-{oid}")))
    granted = policy.grant(code=policy.pending(f"objective-{oid}")[0]["code"])
    assert objectives.resume_after_approval(f"objective-{oid}", granted) == oid
    ran = []
    objectives.run_step(oid, settings=settings, run_fn=_ok_runner(ran), verify_fn=lambda *a, **k: (True, ""))
    assert "approved exactly these actions" in ran[0][1] and "rm v" in ran[0][1]


# ---- the chat side (Hermes plugin) ------------------------------------------------------------------

@pytest.fixture
def plug(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "waggle_plugin_trust", Path(__file__).parents[1] / "hermes_plugin" / "waggle" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bare_yes_never_approves_background_work(settings, quiet, plug):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    policy.check("Bash", {"command": "rm u"}, session=f"objective-{oid}")
    reply, meta = plug._approval_turn("tg-1", "yes", "telegram", prev_msg_at=0.0)
    code = policy.pending()[0]["code"]
    assert meta["route"] == "approval" and f"approve {code}" in reply and "Trust test" in reply
    assert policy.pending()[0]["state"] == "pending"  # nothing granted
    # a yes to an ordinary question (no new request since the user's last message) is left alone
    assert plug._approval_turn("tg-1", "yes", "telegram", prev_msg_at=time.time()) is None
    # "ok then" / "yes sure" are not codes
    assert plug._approval_turn("tg-1", "ok then", "telegram", prev_msg_at=time.time()) is None


def test_approve_code_grants_exactly_one_and_resumes(settings, quiet, plug):
    o1 = objectives.create("first goal here", asker=_planner([STEP]), settings=settings)
    o2 = objectives.create("second goal here", asker=_planner([STEP]), settings=settings)
    for o, cmd in ((o1, "rm one"), (o2, "rm two")):
        policy.check("Bash", {"command": cmd}, session=f"objective-{o}")
        objectives.run_step(o, settings=settings, run_fn=_ok_runner([], approvals=policy.pending(f"objective-{o}")))
    c1 = policy.pending(f"objective-{o1}")[0]["code"]
    reply, _ = plug._approval_turn("tg-1", f"approve {c1.lower()}", "telegram", prev_msg_at=0.0)
    assert "Approved once" in reply and "rm one" in reply
    assert objectives.status(o1)["status"] == "active" and objectives.status(o2)["status"] == "waiting_approval"
    again, _ = plug._approval_turn("tg-1", f"approve {c1}", "telegram", prev_msg_at=0.0)
    assert "no longer open" in again  # can't be reused
    bogus, _ = plug._approval_turn("tg-1", "approve ZZZZ", "telegram", prev_msg_at=0.0)
    assert "Nothing was done" in bogus


def test_ops_view_for_the_command_center(settings, quiet):
    from waggle import doctor
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    policy.check("Bash", {"command": "rm t"}, session=f"objective-{oid}")
    objectives.run_step(oid, settings=settings, run_fn=_ok_runner([], approvals=policy.pending(f"objective-{oid}")))
    op = objectives.ops()
    assert op["waiting"][0]["objective"] == oid and op["waiting"][0]["title"] == "Trust test"
    assert op["active"] == [] and op["workers"] == []
    st, detail, _ = doctor.check_background(settings)
    assert "1 waiting approval" in detail


def test_work_the_user_just_started_does_not_wait_for_them_to_stop_chatting(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    budget.note_interactive()  # the message that created it
    ran = []
    assert objectives.work(oid, settings=settings, run_fn=_ok_runner(ran), verify_fn=lambda *a, **k: (True, ""),
                           user=True) == "done"
    assert len(ran) == 1


def test_supervisor_work_sleeps_off_a_short_wait_in_the_worker(settings, quiet):
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    budget.note_interactive()
    slept = []

    def fake_sleep(s):
        slept.append(s)
        budget.note_interactive(time.time() - 3600)  # the user went quiet
        con = objectives._db()
        objectives._set(con, oid, deferred_until=None)
        con.commit()
    out = objectives.work(oid, settings=settings, run_fn=_ok_runner([]), verify_fn=lambda *a, **k: (True, ""),
                          user=False, sleep=fake_sleep)
    assert out == "done" and slept and slept[0] <= objectives.SHORT_WAIT_S + 1


def test_raising_the_limit_unparks_budget_waits_at_the_next_tick(settings, quiet):
    settings.background_daily_units = 0.01
    settings.save()
    oid = objectives.create("goal goal goal", asker=_planner([STEP]), settings=settings)
    assert objectives.run_step(oid, settings=settings, run_fn=_ok_runner([])) == "waiting_budget"
    assert objectives.supervise(kick_fn=lambda o: None) == []  # still no room
    budget.set_limit(4)
    assert objectives.supervise(kick_fn=lambda o: None) == [oid]
    assert objectives.status(oid)["deferred_until"] is None


def test_rerun_after_approval_matches_the_same_command_with_a_new_description():
    """Found live: the re-run sent the same rm command with a different 'description' and was asked again."""
    a = {"command": 'rm -- "/v/Inbox/x.md"', "description": "Delete test note"}
    d = policy.check("Bash", a, session="objective-live")
    item = policy.get(d.approval_id)
    assert item["summary"] == 'Bash: $ rm -- "/v/Inbox/x.md"'
    policy.grant(code=item["code"])
    b = {"command": 'rm --  "/v/Inbox/x.md"', "description": "Remove the test note file", "timeout": 30000}
    assert policy.check("Bash", b, session="objective-live").allowed
    assert policy.check("Bash", b, session="objective-live").action == "ask"  # still one use only
    assert policy.check("Bash", {"command": 'rm -- "/v/Inbox/y.md"'}, session="objective-live").action == "ask"


def test_a_promised_file_must_really_exist_in_the_vault(settings, quiet):
    """Found live: the agent wrote Inbox/x.md into its working folder and the report-only check passed it."""
    from waggle import vault
    step = dict(STEP, detail="Write the note Inbox/M3 check.md", done_when="Inbox/M3 check.md exists with the line ok")
    oid = objectives.create("goal goal goal", asker=_planner([step]), settings=settings)
    ran = []
    out = objectives.run_step(oid, settings=settings, run_fn=_ok_runner(ran, text="Created Inbox/M3 check.md"),
                              verify_fn=lambda *a, **k: (True, ""))
    assert out == "retry" and "does not exist" in objectives.status(oid)["steps"][0]["verify"]
    assert str(vault.root(settings)) in ran[0][1]  # the step was told where the vault is
    seen = []
    vault.write("Inbox/M3 check", "ok")
    out = objectives.run_step(oid, settings=settings, run_fn=_ok_runner(ran),
                              verify_fn=lambda task, report, **k: seen.append(report) or (True, ""))
    assert out == "done" and "M3 check.md exists" in seen[0]


def test_a_delete_step_is_not_failed_for_the_file_being_gone(settings, quiet):
    step = dict(STEP, detail="Delete Inbox/old.md", done_when="Inbox/old.md no longer exists")
    oid = objectives.create("goal goal goal", asker=_planner([step]), settings=settings)
    assert objectives.run_step(oid, settings=settings, run_fn=_ok_runner([]),
                               verify_fn=lambda *a, **k: (True, "")) == "done"
