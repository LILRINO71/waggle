"""Night shift (Step 3): one task end to end on a real local git repository (sandbox, worker, code checks, second
opinion, delivery), the night window and the week cap, the morning report and the owner's one-word answers."""
import datetime as dt
import json
import subprocess
import types

import pytest

from waggle import nightshift as ns
from waggle.config import data_path
from waggle.core import store


@pytest.fixture(autouse=True)
def _close():
    yield
    store.close_all()


def at(h, m=0, day=24):
    return dt.datetime(2026, 11, day, h, m).timestamp()


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path):
    """A bare 'GitHub' repository with main (README, AGENTS.md) and a clone under the night folder."""
    src = tmp_path / "src"
    src.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=src)
    sh("git", "config", "user.email", "t@example.com", cwd=src)
    sh("git", "config", "user.name", "t", cwd=src)
    (src / "README.md").write_text("hello\n")
    (src / "AGENTS.md").write_text("rules\n")
    sh("git", "add", ".", cwd=src)
    sh("git", "commit", "-q", "-m", "init", cwd=src)
    bare = tmp_path / "remote.git"
    sh("git", "clone", "-q", "--bare", str(src), str(bare))
    return bare


@pytest.fixture
def night(remote, monkeypatch, tmp_path):
    objs = [{"id": "OX", "title": "Test objective", "repo": "me/repo", "test": "python -c \"raise SystemExit(0)\"",
             "tasks": [{"id": "OX-1", "title": "Add a line", "base": "main", "branch": "waggle/add-line",
                        "checks": ["contains_main"], "brief": "Add a line to README.md and commit."}]}]
    d = data_path("night")
    d.mkdir(parents=True, exist_ok=True)
    (d / "objectives.json").write_text(json.dumps(objs))

    def fake_clone(name):
        c = d / "repos" / "me__repo"
        if not c.exists():
            sh("git", "clone", "-q", str(remote), str(c))
            sh("git", "config", "user.email", "w@example.com", cwd=c)
            sh("git", "config", "user.name", "w", cwd=c)
        sh("git", "remote", "set-url", "--push", "origin", ns.NO_PUSH, cwd=c)
        sh("git", "fetch", "-q", "origin", cwd=c)
        return c
    monkeypatch.setattr(ns, "clone", fake_clone)
    delivered = []
    monkeypatch.setattr(ns, "deliver", lambda obj, t, wt, note: delivered.append((t["branch"], str(wt))) or
                        "https://github.com/me/repo/pull/7")
    monkeypatch.setattr(ns, "reading", lambda now=None: None)
    return types.SimpleNamespace(delivered=delivered, dir=d)


def worker(commit=True, note=True, approvals=(), text="Added the line."):
    def run(brief, **kw):
        wt = kw["cwd"]
        note_path = brief.split("NOTE_PATH = ")[1].split("\n")[0].strip()
        if commit:
            with open(f"{wt}/README.md", "a") as f:
                f.write("night line\n")
            sh("git", "-c", "user.email=w@example.com", "-c", "user.name=w", "commit", "-qam", "add line", cwd=wt)
        if note:
            with open(note_path, "w") as f:
                f.write("## What changed\n" + "Added one line to README.md as the brief asked. " * 6 +
                        "\n\nVerdict: ready to merge\n")
        return types.SimpleNamespace(text=text, error="", approvals=list(approvals))
    return run


def passes(*a, **k):
    return {"json": {"pass": True, "problems": []}}


def test_a_task_runs_end_to_end_and_is_delivered(night):
    ns.sync()
    row = ns.work("OX-1", agent_run=worker(), asker=passes)
    assert row["status"] == "delivered", row
    assert row["pr_url"].endswith("/pull/7")
    checks = {c[0]: c[1] for c in json.loads(row["checks"])}
    assert all(checks.values()), checks
    assert night.delivered == [("waggle/add-line", row["worktree"])]


def test_the_worker_cannot_push_from_its_sandbox(night):
    ns.sync()
    ns.work("OX-1", agent_run=worker(), asker=passes)
    wt = ns.task("OX-1")["worktree"]
    r = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=wt, capture_output=True, text=True)
    assert r.returncode != 0


def test_no_commit_is_retried_once_with_the_gap_then_fails(night):
    ns.sync()
    row = ns.work("OX-1", agent_run=worker(commit=False), asker=passes)
    assert row["status"] == "queued" and "new commits" in row["gap"]
    seen = {}

    def second(brief, **kw):
        seen["brief"] = brief
        return worker(commit=False)(brief, **kw)
    row = ns.work("OX-1", agent_run=second, asker=passes)
    assert "Last time it fell short: new commits" in seen["brief"]
    assert row["status"] == "failed" and not night.delivered


def test_a_permission_ask_parks_the_task_with_the_question(night):
    ns.sync()
    ask = {"code": "AB12", "what": "installing third-party code: left-pad"}
    row = ns.work("OX-1", agent_run=worker(approvals=[ask]), asker=passes)
    assert row["status"] == "parked"
    assert "left-pad" in json.loads(row["questions"])[0] and not night.delivered


def test_the_second_opinion_can_stop_a_delivery(night):
    ns.sync()
    no = lambda *a, **k: {"json": {"pass": False, "problems": ["the note claims tests it did not run"]}}
    row = ns.work("OX-1", agent_run=worker(), asker=no)
    assert row["status"] == "queued" and "did not run" in row["gap"] and not night.delivered


def test_a_failing_review_call_passes_nothing():
    def boom(*a, **k):
        raise RuntimeError("no claude")
    assert ns.review("b", "n", "s", [], asker=boom)["pass"] is False


def test_changing_a_protected_file_fails_the_checks(night):
    ns.sync()

    def sneaky(brief, **kw):
        out = worker()(brief, **kw)
        with open(f"{kw['cwd']}/AGENTS.md", "w") as f:
            f.write("no rules\n")
        sh("git", "-c", "user.email=w@e.com", "-c", "user.name=w", "commit", "-qam", "x", cwd=kw["cwd"])
        return out
    row = ns.work("OX-1", agent_run=sneaky, asker=passes)
    assert row["status"] == "queued" and "protected files unchanged" in row["gap"]


def test_deliver_refuses_anything_but_a_waggle_branch(tmp_path):
    with pytest.raises(RuntimeError, match="only waggle"):
        ns.deliver({"repo": "me/repo"}, {"branch": "main", "id": "x"}, tmp_path, tmp_path / "n.md")


def test_tick_only_queues_at_night_with_room(night, monkeypatch, settings):
    from waggle import quota
    monkeypatch.setattr(quota, "seven_day_block", lambda s, now=None: (False, "", 0))
    assert ns.tick_job(settings=settings, now=at(15))["why"] == "outside night hours"
    assert "too late" in ns.tick_job(settings=settings, now=at(5, 30))["why"]
    out = ns.tick_job(settings=settings, now=at(23, 10))
    assert out["queued"] and out["task"] == "OX-1"
    monkeypatch.setattr(quota, "seven_day_block", lambda s, now=None: (True, "the 7-day limit is at 90%", 0))
    assert "90%" in ns.tick_job(settings=settings, now=at(23, 20))["why"]


def test_the_week_cap_stops_the_night_shift(night, monkeypatch, settings):
    from waggle import quota
    monkeypatch.setattr(quota, "seven_day_block", lambda s, now=None: (False, "", 0))
    ns.sync()
    ns._set("OX-1", week_used=0.36, started=at(23, 0, 23))
    ok, why = ns.room(settings, now=at(23, 30, 23))
    assert not ok and "35%" in why


def test_a_running_task_blocks_a_second_one_and_a_dead_one_is_requeued(night, monkeypatch, settings):
    from waggle import quota
    monkeypatch.setattr(quota, "seven_day_block", lambda s, now=None: (False, "", 0))
    from waggle.core import jobs
    jobs.register(ns.TASK, lambda p=None, **k: None, detached=True)
    ns.sync()
    ns._set("OX-1", status="running", started=at(23, 0), attempt=1)
    jid = jobs.enqueue(ns.TASK, {"task": "OX-1"}, now=at(23, 0))
    assert jobs.claim("w", kinds=[ns.TASK], now=at(23, 0))
    assert ns.tick_job(settings=settings, now=at(23, 30))["why"] == "a task is running"
    jobs.fail(jid, "w", "killed by a deploy", retry=False, now=at(23, 40))  # the worker died with the core
    out = ns.tick_job(settings=settings, now=at(23, 50))
    assert out["queued"] and "interrupted" in ns.task("OX-1")["gap"]


def test_a_three_night_objective_survives_restarts(night, monkeypatch, settings):
    """Step 4's gate, in miniature: tasks of one objective spread over three nights, the first night's worker
    killed by a deploy; every task ends delivered exactly once."""
    from waggle import quota
    monkeypatch.setattr(quota, "seven_day_block", lambda s, now=None: (False, "", 0))
    objs = json.loads((night.dir / "objectives.json").read_text())
    objs[0]["tasks"] = [dict(objs[0]["tasks"][0], id=f"OX-{i}", branch=f"waggle/t{i}") for i in (1, 2, 3)]
    (night.dir / "objectives.json").write_text(json.dumps(objs))
    ns.sync()
    ns._set("OX-1", status="running", started=at(23, 5), attempt=1)  # night 1: the core restarts mid-task
    out = ns.tick_job(settings=settings, now=at(23, 30))
    assert out["queued"] and out["task"] == "OX-1"
    for day, tid in ((24, "OX-1"), (25, "OX-2"), (26, "OX-3")):
        out = ns.tick_job(settings=settings, now=at(23, 40, day))
        assert out.get("task") == tid, out
        ns.work(tid, agent_run=worker(), asker=passes, now=at(23, 45, day))
    assert [t["status"] for t in ns.tasks()] == ["delivered"] * 3
    assert len(night.delivered) == 3


def test_morning_report_and_one_word_answers(night, monkeypatch, settings):
    monkeypatch.setattr(ns, "ci_state", lambda url: "green")
    ns.sync()
    ns.work("OX-1", agent_run=worker(), asker=passes)
    text, items = ns.report_text(settings=settings)
    assert items == ["OX-1"] and "1. Add a line: draft PR https://github.com/me/repo/pull/7 | CI green" in text
    assert "Verdict: ready to merge"[9:] in text
    assert ns.report_job(settings=settings)["sent"]
    assert ns.report_job(settings=settings)["sent"] is False  # once per morning
    assert ns.answer("reject 3") == "This morning's report has items 1 to 1."
    assert ns.answer("hello there") is None
    assert ns.answer("Reject one: too big") == "Recorded: 1 rejected (too big)."
    assert ns.task("OX-1")["answer"] == "rejected"
    assert ns.answer("redo 1 keep the old title") == "Recorded: 1 queued again for tonight (keep the old title)."
    row = ns.task("OX-1")
    assert row["status"] == "queued" and "keep the old title" in row["gap"] and row["attempt"] == 0
    assert ns.answer("accept all") == "Recorded: 1 accepted."
    assert ns.acceptance()["accepted"] == 1


def test_evening_line_says_what_tonight_holds(night, monkeypatch, settings):
    from waggle import quota
    monkeypatch.setattr(quota, "seven_day_block", lambda s, now=None: (False, "", 0))
    assert ns.evening_line(settings=settings).startswith("Tonight (23:00-07:00): Add a line.")
    monkeypatch.setattr(quota, "seven_day_block", lambda s, now=None: (True, "the week is at 88%", 0))
    assert "It will not start yet: the week is at 88%" in ns.evening_line(settings=settings)
    settings.night_shift = False
    assert ns.evening_line(settings=settings) == ""


def test_default_objectives_are_the_owners_choices(tmp_path):
    ids = [t["id"] for o in ns.DEFAULT_OBJECTIVES for t in o["tasks"]]
    assert ids == ["O1-pr12", "O1-pr13", "O1-pr15", "O2-churn"]
    assert all(t["branch"].startswith("waggle/") for o in ns.DEFAULT_OBJECTIVES for t in o["tasks"])


def test_night_key_groups_after_midnight_with_the_evening():
    assert ns.night_key(at(23, 30)) == ns.night_key(at(2, 0, 25)) == "2026-11-24"


def test_registered_on_the_core():
    from waggle.core import plugins
    assert "waggle.nightshift" in plugins.MODULES
    assert [t[0] for t in ns.timer_specs()] == [ns.TICK, ns.REPORT]
