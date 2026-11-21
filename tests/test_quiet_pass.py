"""A quiet look around costs almost nothing: no git process for a repository that did not change, GitHub asked in
one call at most every 30 minutes. Process starts are counted on the way to subprocess.Popen (what git, gh and
everything else go through) plus every call of the fake gh, which stands for one real process."""
import datetime as dt
import subprocess
import time

import pytest

from waggle import initiative, proactive, tick, watchers
from waggle.config import Settings

NO_DECISIONS = lambda *a, **k: {"json": {"decisions": []}}  # noqa: E731


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """gh as a function: one repo list (with the counts GitHub keeps), then runs, PRs and issues per repo."""

    def __init__(self, t0, repos=("me/alpha", "me/beta", "me/gamma")):
        self.calls, self.t0 = [], t0
        self.prs = {r: [] for r in repos}
        self.pushed = {r: t0 for r in repos}

    def open_pr(self, repo, number, title, at):
        self.prs[repo].append({"number": number, "title": title, "state": "OPEN", "author": {"login": "someone"},
                               "url": "u", "mergedAt": None})
        self.pushed[repo] = at

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["repo", "list"]:
            return [{"nameWithOwner": r, "pushedAt": iso(self.pushed[r]), "updatedAt": iso(self.pushed[r]),
                     "isArchived": False, "issues": {"totalCount": 0}, "pullRequests": {"totalCount": len(p)}}
                    for r, p in self.prs.items()]
        repo = args[args.index("-R") + 1] if "-R" in args else ""
        if args[0] == "pr":
            return list(self.prs.get(repo, []))
        return []


@pytest.fixture
def world(tmp_path, settings, monkeypatch):
    """Twelve checkouts under one watch root, a fake GitHub, a fake clock, and a counter of process starts."""
    run = lambda *a, cwd=None: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd,
                                              check=True, capture_output=True, text=True).stdout.strip()
    root = tmp_path / "code"
    for i in range(12):
        run("init", "-q", str(root / f"r{i}"))
        run("remote", "add", "origin", str(tmp_path / f"nowhere{i}.git"), cwd=root / f"r{i}")
        run("commit", "--allow-empty", "-qm", "a", cwd=root / f"r{i}")
        run("update-ref", f"refs/remotes/origin/{run('branch', '--show-current', cwd=root / f'r{i}')}", "HEAD",
            cwd=root / f"r{i}")
    settings.watch_roots = [str(root)]
    settings.github_repos = []
    settings.save()
    proactive._GIT_REFS.clear()
    t0 = time.time() + 3 * 3600  # three hours on: a fresh commit would count as "sitting unpushed"
    gh = FakeGitHub(t0 - 3600)
    started = []
    real = subprocess.Popen.__init__
    monkeypatch.setattr(subprocess.Popen, "__init__",
                        lambda self, args, *a, **k: (started.append(list(args)), real(self, args, *a, **k))[1])

    class World:
        pass
    w = World()
    w.run, w.root, w.gh, w.t0, w.started, w.s = run, root, gh, t0, started, Settings.load()

    def look(minutes, steps=("calls", "followups", "watchers", "responses")):
        n = len(started) + len(gh.calls)
        out = tick.look(settings=w.s, now=t0 + minutes * 60, activity={}, gh=gh, asker=NO_DECISIONS, steps=steps)
        w.last_cost = len(started) + len(gh.calls) - n
        return out
    w.look = look
    return w


def test_an_idle_pass_starts_at_most_five_processes(world):
    full = ("calls", "followups", "watchers", "responses", "decide")
    world.look(0, steps=full)                       # the first look learns the baseline (git and GitHub asked once)
    assert world.gh.calls and len(world.started) > 0
    costs = []
    for minutes in (5, 10, 15):                     # three quiet passes in a row
        world.look(minutes, steps=full)
        costs.append(world.last_cost)
    assert max(costs) <= 5, costs
    assert sum(costs) == 0, "nothing changed and GitHub was asked less than 30 minutes ago: no process at all"


def test_github_is_asked_once_in_thirty_minutes_and_by_one_call_when_idle(world):
    world.look(0)
    first = list(world.gh.calls)
    assert [c[:2] for c in first].count(["repo", "list"]) == 1
    for minutes in range(5, 31, 5):
        world.look(minutes)
    second = world.gh.calls[len(first):]
    assert second == [first[0]] or [c[:2] for c in second] == [["repo", "list"]], \
        "at minute 30 one repo-list call; no run, PR or issue call for repos that did not move"
    assert len(world.gh.calls) == len(first) + 1


def test_a_new_commit_is_seen_on_the_next_pass(world):
    world.look(0)
    world.look(5)
    assert initiative.pending() == []
    repo = world.root / "r3"
    world.run("commit", "--allow-empty", "-qm", "b", cwd=repo)
    before = len(world.started)
    world.look(10)
    assert len(world.started) > before, "git was asked about the repository that changed"
    assert len(world.started) - before <= 5
    keys = [x["title"] for x in initiative.pending()]
    assert any("r3" in k and "1 commit(s) not pushed" in k for k in keys), keys


def test_a_new_pull_request_is_seen_at_the_next_github_look(world):
    world.look(0)
    world.look(30)                                   # nothing moved
    assert initiative.pending() == []
    world.gh.open_pr("me/beta", 7, "Add the intake", world.t0 + 35 * 60)
    world.look(35)                                   # GitHub is not asked yet: it was asked five minutes ago
    assert initiative.pending() == []
    calls = len(world.gh.calls)
    world.look(60)                                   # the next look at GitHub
    new = world.gh.calls[calls:]
    assert [c[:2] for c in new].count(["repo", "list"]) == 1
    assert any(c[0] == "pr" and "me/beta" in c for c in new) and not any(c[0] == "pr" and "me/alpha" in c for c in new)
    assert any("new PR #7" in x["title"] for x in initiative.pending())


def test_gh_is_started_without_the_time_zone_helper(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("watchers_real", watchers.__file__.replace("\\", "/"),
                                                  submodule_search_locations=None)
    seen = {}

    class R:
        returncode, stdout = 0, "[]"
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: seen.update(k) or R())
    real = importlib.util.module_from_spec(spec)
    real.__package__ = "waggle"
    spec.loader.exec_module(real)                    # the conftest replaces watchers.run_gh: load an untouched copy
    monkeypatch.setattr(real, "gh_path", lambda: "gh")
    assert real.run_gh(["repo", "list"]) == []
    assert seen["env"]["TZ"] == "UTC"
