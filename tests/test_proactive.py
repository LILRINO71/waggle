import subprocess
import time

import pytest

from waggle import events, proactive, projects, vault
from waggle.config import Settings


@pytest.fixture
def proj(settings):
    vault.init(settings)
    projects.create("BIOBUZZ Robot", settings, aliases=["biobuzz"], next_actions=["Research transfers"])
    settings.watch_roots = []
    settings.save()
    return Settings.load()


def test_stale_project_watch(proj):
    import datetime as dt
    p = vault.safe_path("Projects/BIOBUZZ Robot", proj)
    p.write_text(p.read_text(encoding="utf-8").replace(f"updated: {dt.date.today():%Y-%m-%d}",
                                                       "updated: 2026-01-01"), encoding="utf-8")
    found = proactive.stale_watch(proj, today=dt.date(2026, 2, 1))
    assert found and "Research transfers" in found[0]["detail"]


def test_git_watch_unpushed_and_uncommitted(tmp_path, settings):
    vault.init(settings)
    remote = tmp_path / "remote.git"
    work = tmp_path / "code" / "myrepo"
    run = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    run("init", "--bare", "-q", str(remote))
    run("clone", "-q", str(remote), str(work))
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "a", cwd=work)
    run("push", "-q", "origin", "HEAD", cwd=work)
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "b", cwd=work)
    (work / "wip.txt").write_text("x")
    settings.watch_roots = [str(tmp_path / "code")]
    found = proactive.git_watch(settings, now=time.time() + 5 * 86400)
    titles = " | ".join(f["title"] for f in found)
    assert "1 commit(s) not pushed" in titles and "uncommitted" in titles


def test_feedback_counts_in_summary(settings):
    events.log("feedback", finding="abc", useful=True)
    events.log("feedback", finding="def", useful=False)
    assert events.summary()["useful_rate"] == 0.5


def _objective(status, hours_ago, error=None, log_kind=None):
    from waggle import objectives
    oid = objectives.create("Write the scouting sheet", title="Scouting sheet", plan_now=False)
    con = objectives._db()
    t = time.time() - hours_ago * 3600
    con.execute("UPDATE objectives SET status=?, updated=?, error=? WHERE id=?", (status, t, error, oid))
    con.execute("DELETE FROM log WHERE objective_id=?", (oid,))
    if log_kind:
        con.execute("INSERT INTO log VALUES (?,?,?,?)", (oid, t, log_kind, "budget: window"))
    con.commit()
    con.close()
    return oid


def test_objective_watch_flags_what_needs_the_user(settings):
    _objective("blocked", 13, error="step 2 failed: no shooter data")
    _objective("waiting_approval", 4)
    _objective("active", 1)  # fresh: fine
    titles = [f["title"] for f in proactive.objective_watch()]
    assert any("still blocked" in t for t in titles) and any("waiting for your yes" in t for t in titles)
    assert len(titles) == 2


def test_objective_watch_stalled_vs_waiting_for_budget(settings):
    _objective("active", 5, log_kind="waiting")
    assert proactive.objective_watch() == []  # parked for the Claude window: expected
    _objective("active", 3)
    found = proactive.objective_watch()
    assert len(found) == 1 and found[0]["title"].endswith("has stalled") and "worker" in found[0]["detail"]
