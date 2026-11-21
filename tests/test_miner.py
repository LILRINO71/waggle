"""Repeated-work miner on synthetic derived logs: thresholds, request groups, the proof step, the shadow-run
counter, and that nothing outward (or writing) happens in shadow mode."""
import subprocess

import pytest

from waggle import miner

DAY = 86400.0
T0 = 1_790_000_000.0
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def session(sid, day, cmds=(), turns=(), ws="alpha", agent=False):
    base = T0 + day * DAY
    return {"session": sid, "agent": agent, "machine": False, "title": "", "first": base, "last": base + 3600,
            "turns": [[base + i * 60, ws, kw, 40] for i, kw in enumerate(turns)], "edits": [], "files": {},
            "cmds": [[base + i * 60, shape, ws] for i, shape in enumerate(cmds)]}


SHIP = ["python -m pytest", "git add > git commit", "git push"]


def logs(n_sessions=3, days=(0, 1, 2)):
    return [session(f"s{i}", days[i % len(days)], cmds=["git status", *SHIP, "ls"]) for i in range(n_sessions)]


def test_a_sequence_repeated_in_three_sessions_on_two_days_is_a_candidate():
    found = miner.mine(logs(3, (0, 1)), chat=[], now=T0 + 3 * DAY)
    labels = {c["label"]: c for c in found}
    assert "git add > git commit > git push" in labels
    c = labels["git add > git commit > git push"]
    assert c["evidence"]["count"] == 3 and c["evidence"]["sessions"] == 3 and c["evidence"]["days"] == 2
    assert c["evidence"]["workstreams"] == ["alpha"]
    assert [st["class"] for st in c["recipe"]] == ["LOCAL_WRITE", "LOCAL_WRITE", "OUTWARD"]
    assert all(not miner.activity.is_read_only(lab) for lab in labels)  # status/ls chains are never candidates


def test_below_the_thresholds_nothing_is_proposed():
    assert miner.mine(logs(2, (0, 1)), chat=[], now=T0 + 3 * DAY) == []
    assert miner.mine(logs(3, (0,)), chat=[], now=T0 + 3 * DAY) == []  # three sessions, one day
    far_apart = [session(f"s{i}", i, cmds=["git add", "sleep", "sleep"]) for i in range(3)]
    for d in far_apart:  # the second command comes an hour later: not one sequence
        d["cmds"].append([d["cmds"][0][0] + 3600, "git push", "alpha"])
    assert all("git add > git push" != c["label"] for c in miner.mine(far_apart, chat=[], now=T0 + 4 * DAY))


def test_repeated_requests_are_grouped_by_keywords():
    kw = ["inbox", "emails", "summarize", "unread"]
    sess = [session(f"s{i}", i, turns=[kw[:3] + (["today"] if i else [])]) for i in range(2)]
    chat = [{"time": T0 + 2 * DAY + 10, "request": "summarize my unread inbox emails"}]
    found = miner.mine(sess, chat=chat, now=T0 + 3 * DAY)
    req = [c for c in found if c["kind"] == "request"]
    assert len(req) == 1 and req[0]["evidence"]["count"] == 3 and req[0]["evidence"]["days"] == 3
    assert set(req[0]["label"].split()) >= {"inbox", "emails", "summarize"}
    assert req[0]["evidence"]["sources"] == ["chat", "claude_code"]
    assert req[0]["recipe"] is None


def ship_candidate(tmp_path):
    miner.save(miner.mine(logs(3, (0, 1)), chat=[], now=T0 + 3 * DAY), now=T0 + 3 * DAY)
    key = next(c["key"] for c in miner.candidates() if c["label"] == "git add > git commit > git push")
    folder = tmp_path / "alpha"
    folder.mkdir()
    return key, folder


def test_proof_then_three_shadow_runs_then_the_owner_switches_it_on(settings, tmp_path):
    key, folder = ship_candidate(tmp_path)
    with pytest.raises(ValueError):
        miner.enable(key)  # found, not proposed
    ok, checks = miner.prove(key, target=folder)
    assert ok and miner.get(key)["status"] == "shadow"
    ran = []

    def spy(step, where):
        ran.append(step["shape"])
        return 0
    for i in range(1, 4):
        run = miner.shadow_run(key, executor=spy, target=folder)
        assert run["ok"] and run["shadow_runs"] == i
        assert {s["shape"] for s in run["steps"] if s["would_run"]} == {"git add", "git commit", "git push"}
    assert ran == []  # every step of this recipe writes or acts outward: none was executed
    assert miner.get(key)["status"] == "proposed"
    assert "skipped" in miner.shadow_run(key, executor=spy)  # proposed: waits for the owner, no more runs
    with pytest.raises(KeyError):
        miner.enable("commands:nope")
    miner.enable(key)
    assert miner.get(key)["status"] == "enabled"


def test_shadow_mode_runs_only_read_only_steps_for_real(settings, tmp_path):
    repo = tmp_path / "beta"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, creationflags=NO_WINDOW)
    sess = [session(f"s{i}", i % 2, cmds=["git status > git push"], ws="beta") for i in range(3)]
    miner.save(miner.mine(sess, chat=[], now=T0 + 3 * DAY))
    key = miner.candidates()[0]["key"]
    assert miner.prove(key, target=repo)[0]
    run = miner.shadow_run(key, target=repo)  # the real executor
    by = {s["shape"]: s for s in run["steps"]}
    assert by["git status"]["ran"] and by["git status"]["exit"] == 0
    assert not by["git push"]["ran"] and by["git push"]["would_run"]
    assert run["ok"] and run["shadow_runs"] == 1


def test_a_failing_shadow_run_does_not_count_and_proof_needs_a_folder(settings, tmp_path):
    key, folder = ship_candidate(tmp_path)
    assert not miner.prove(key, target=tmp_path / "missing")[0]
    assert miner.get(key)["status"] == "found"
    assert miner.prove(key, target=folder)[0]
    c = miner.get(key)
    c_recipe = [{"shape": "git status", "class": "SAFE"}, *c["recipe"]]
    miner._set(key, recipe=c_recipe)
    run = miner.shadow_run(key, executor=lambda st, f: 1, target=folder)
    assert not run["ok"] and run["shadow_runs"] == 0 and miner.get(key)["status"] == "shadow"


def test_the_pass_shadow_runs_only_after_a_new_occurrence(settings, tmp_path, monkeypatch):
    key, folder = ship_candidate(tmp_path)
    monkeypatch.setattr(miner, "target_folder", lambda c, settings=None: folder)
    calls = []
    out = miner.run(now=T0 + 3 * DAY, executor=lambda st, f: calls.append(st) or 0,
                    sessions=logs(3, (0, 1)), chat=[])
    assert key in out["new_proofs"] and (key, True) in out["shadow_runs"]
    out = miner.run(now=T0 + 3 * DAY + 60, executor=lambda st, f: 0, sessions=logs(3, (0, 1)), chat=[])
    assert out["shadow_runs"] == []  # nothing new since the last shadow run
    more = logs(3, (0, 1)) + [session("s9", 10, cmds=SHIP)]
    out = miner.run(now=T0 + 11 * DAY, executor=lambda st, f: 0, sessions=more, chat=[])
    again = [k for k, _ in out["shadow_runs"]]
    assert key in again and len(again) == len(set(again))  # one run per candidate seen again, never two
    assert all(miner.get(k)["evidence"]["last"] >= T0 + 10 * DAY for k in again)
    assert miner.get(key)["shadow_runs"] == 2


def test_classification_fails_closed():
    assert miner.classify("git push") == "OUTWARD"
    assert miner.classify("gh pr create") == "OUTWARD"
    assert miner.classify("gh run rerun") == "OUTWARD"
    assert miner.classify("gh pr view") == "SAFE" or miner.classify("gh pr view") == "LOCAL_WRITE"
    assert miner.classify("python -m pytest") == "TEST"
    assert miner.classify("git status") == "SAFE"
    assert miner.classify("some-unknown-tool") == "LOCAL_WRITE"
