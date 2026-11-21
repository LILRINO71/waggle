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


# ---- refs-only git questions are asked once per change, not once per look ------------------------------------------

@pytest.fixture
def git_repos(tmp_path, settings, monkeypatch):
    """A clone with one pushed commit, a worktree of it, and a counter of the git processes the watchers start."""
    vault.init(settings)
    remote, work = tmp_path / "remote.git", tmp_path / "code" / "myrepo"
    run = lambda *a, cwd=None: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd,
                                              check=True, capture_output=True, text=True).stdout.strip()
    run("init", "--bare", "-q", str(remote))
    run("clone", "-q", str(remote), str(work))
    run("commit", "--allow-empty", "-qm", "a", cwd=work)
    run("push", "-q", "-u", "origin", "HEAD", cwd=work)
    settings.watch_roots = [str(tmp_path / "code")]
    proactive._GIT_REFS.clear()
    asked = []
    real = proactive._git
    monkeypatch.setattr(proactive, "_git", lambda repo, *a: (asked.append((str(repo), a)), real(repo, *a))[1])
    return {"run": run, "work": work, "asked": asked, "settings": settings, "tmp": tmp_path}


def test_a_quiet_repo_is_asked_once(git_repos):
    from waggle import watchers
    g, s = git_repos, git_repos["settings"]
    now = time.time() + 3 * 3600
    assert watchers.unpushed_watch(s, now) == []
    first = len(g["asked"])
    assert first == 2, "has it a remote, and how many commits are on none (the last commit's time only when some are)"
    for _ in range(5):                                   # five more looks: nothing has changed
        assert watchers.unpushed_watch(s, now) == []
        watchers.repo_activity(s, since=0, now=now)
    assert len(g["asked"]) == first + 1, "only the day's log was new; the five-minute questions started no process"


def test_a_commit_and_a_push_are_seen_at_once(git_repos):
    from waggle import watchers
    g, s = git_repos, git_repos["settings"]
    now = time.time() + 3 * 3600
    assert watchers.unpushed_watch(s, now) == []
    g["run"]("commit", "--allow-empty", "-qm", "b", cwd=g["work"])
    found = watchers.unpushed_watch(s, now)
    assert len(found) == 1 and "1 commit(s) not pushed" in found[0]["title"]
    assert watchers.repo_activity(s, since=0, now=now)["myrepo"] == ["b", "a"]
    before = len(g["asked"])
    assert len(watchers.unpushed_watch(s, now)) == 1 and len(g["asked"]) == before, "the same answer, no process"
    g["run"]("push", "-q", "origin", "HEAD", cwd=g["work"])
    assert watchers.unpushed_watch(s, now) == [], "pushed: the remote's ref moved, so git was asked again"
    assert len(g["asked"]) > before


def test_a_worktree_has_its_own_head_and_shares_the_refs(git_repos):
    g, s = git_repos, git_repos["settings"]
    wt = g["tmp"] / "code" / "myrepo-wt"
    g["run"]("worktree", "add", "-q", "-b", "side", str(wt), cwd=g["work"])
    assert (wt / ".git").is_file()
    sig_wt, sig_main = proactive.git_signature(wt), proactive.git_signature(g["work"])
    assert sig_wt and sig_main and sig_wt != sig_main
    assert proactive._git_refs(wt, "log", "-1", "--format=%s") == "a"
    n = len(g["asked"])
    assert proactive._git_refs(wt, "log", "-1", "--format=%s") == "a" and len(g["asked"]) == n
    g["run"]("commit", "--allow-empty", "-qm", "c", cwd=wt)
    assert proactive._git_refs(wt, "log", "-1", "--format=%s") == "c"
    assert proactive.git_signature(g["work"]) != sig_main, "the branch tips are shared: the main checkout sees it too"


def test_no_upstream_is_remembered_and_setting_one_is_seen(git_repos):
    g = git_repos
    g["run"]("checkout", "-q", "-b", "local-only", cwd=g["work"])
    assert proactive._git_refs(g["work"], "rev-list", "--count", "@{u}..HEAD") is None
    n = len(g["asked"])
    assert proactive._git_refs(g["work"], "rev-list", "--count", "@{u}..HEAD") is None and len(g["asked"]) == n
    g["run"]("push", "-q", "-u", "origin", "local-only", cwd=g["work"])
    assert proactive._git_refs(g["work"], "rev-list", "--count", "@{u}..HEAD") == "0"


def test_unpushed_means_on_no_remote_at_all(git_repos):
    """A branch cut from origin's default branch tracks it: pushed under its own name it is not "unpushed" (four such
    branches were reported with 5 to 19 commits each); a branch with no upstream and never pushed is."""
    from waggle import watchers
    g, s = git_repos, git_repos["settings"]
    run, work, late = g["run"], g["work"], time.time() + 3 * 3600
    default = run("rev-parse", "--abbrev-ref", "HEAD", cwd=work)
    run("checkout", "-q", "-b", "feature", "--track", f"origin/{default}", cwd=work)
    run("commit", "--allow-empty", "-qm", "f1", cwd=work)
    run("commit", "--allow-empty", "-qm", "f2", cwd=work)
    assert run("rev-list", "--count", "@{u}..HEAD", cwd=work) == "2", "what the old check counted"
    assert proactive.unpushed_count(work) == 2 and len(watchers.unpushed_watch(s, late)) == 1, "really not pushed yet"
    run("push", "-q", "origin", "feature", cwd=work)                      # pushed under its own name, upstream unchanged
    assert run("rev-list", "--count", "@{u}..HEAD", cwd=work) == "2", "the old check still says 2"
    assert proactive.unpushed_count(work) == 0 and watchers.unpushed_watch(s, late) == []
    assert not [f for f in proactive.git_watch(s, now=late) if "not pushed" in f["title"]]
    # a branch that is nowhere else: no upstream, never pushed. The old check could not see it at all
    run("checkout", "-q", "-b", "scratch", cwd=work)
    run("commit", "--allow-empty", "-qm", "s1", cwd=work)
    found = watchers.unpushed_watch(s, late)
    assert proactive.unpushed_count(work) == 1 and len(found) == 1 and "1 commit(s) not pushed" in found[0]["title"]


def test_a_repository_with_no_remote_is_not_called_unpushed(git_repos):
    from waggle import watchers
    g, s = git_repos, git_repos["settings"]
    alone = g["tmp"] / "code" / "alone"
    g["run"]("init", "-q", str(alone))
    g["run"]("commit", "--allow-empty", "-qm", "only here", cwd=alone)
    assert proactive.unpushed_count(alone) is None
    assert [x["title"] for x in watchers.unpushed_watch(s, time.time() + 3 * 3600)] == []


def test_what_is_remembered_is_bounded_and_a_folder_that_is_no_repo_is_asked(git_repos, tmp_path, monkeypatch):
    g = git_repos
    assert proactive.git_signature(tmp_path / "nowhere") is None
    assert proactive._git_refs(tmp_path / "nowhere", "log", "-1") is None
    assert not any(k[0].endswith("nowhere") for k in proactive._GIT_REFS), "nothing is kept without a signature"
    monkeypatch.setattr(proactive, "_GIT_REFS_MAX", 3)
    for i in range(10):
        proactive._git_refs(g["work"], "log", "-1", f"--format=%s{i}")
    assert len(proactive._GIT_REFS) <= 3
