"""The flake guard in the proposal pipeline's verification (waggle/proposals.py): when the FULL suite fails, only the
failed tests are re-run once; failures that pass alone, or that also fail on the base commit, are not the change's
fault and are reported in the owner's text instead of failing the proposal. Fakes only (FakeEnv from test_proposals),
plus one real pytest run on a throwaway project."""
from pathlib import Path

import pytest
from test_proposals import (_clean, drive, env, file_one, final, ok_tests,  # noqa: F401  (fixtures)
                            to_awaiting)
from waggle import proposals
from waggle.proposals import Env

IDS = ["tests/test_a.py::test_one", "tests/test_b.py::test_two[x - y]", "tests/test_c.py::test_three"]


def failing(ids, passed=1197):
    return {"rc": 1, "passed": passed, "failed": len(ids), "errors": 0, "tail": "", "failed_ids": list(ids)}


def scripted(env):
    """A FakeEnv whose test runner tells five kinds of run apart: the targeted tests, the same tests on the base
    commit, the full suite, the re-run of just the failed ids in the worktree, and those ids on the base commit."""
    def run(cwd, files=None, timeout=0):
        name = Path(cwd).name
        by_id = bool(files) and "::" in files[0]
        kind = ("full" if not files else
                ("base_ids" if name.endswith("-base") else "rerun") if by_id else
                ("base" if name.endswith("-base") else "targeted"))
        env.test_calls.append((kind, name, list(files or [])))
        return dict(env.results[kind])
    env.run_tests = run
    env.results.update({"rerun": ok_tests(3), "base_ids": {"rc": 1, "passed": 0, "failed": 0, "errors": 0, "tail": "",
                                                           "failed_ids": []}})
    return env


def kinds(env):
    return [c[0] for c in env.test_calls]


def test_failures_that_pass_on_a_quiet_rerun_are_flaky_and_do_not_stop_the_proposal(env):
    scripted(env)
    env.results["full"] = failing(IDS)
    p = to_awaiting(env)
    assert p["tests"]["flaky"]["verdict"] == "flaky" and p["tests"]["flaky"]["flaky"] == IDS
    assert p["tests"]["full"] == {"passed": 1197, "failed": 3}
    assert kinds(env) == ["targeted", "base", "full", "rerun"]  # the failed ids, once; no base run needed
    assert env.test_calls[-1][2] == IDS
    assert env.cmds("push", "origin", f"proposals/{p['id']}:proposals/{p['id']}")
    (msg,) = env.sent
    assert "3 failed but flaky/pre-existing, not caused by this change" in msg["text"]
    assert "they passed on a quiet re-run" in msg["text"] and "test_one" in msg["text"]
    assert any("not caused by the change (flaky)" in str(h) for h in proposals.history(p["id"]))


def test_failures_that_also_fail_on_the_base_commit_are_pre_existing(env):
    scripted(env)
    env.results["full"] = failing(IDS[:2])
    env.results["rerun"] = failing(IDS[:2], passed=0)
    env.results["base_ids"] = failing(IDS[:2], passed=0)
    p = to_awaiting(env)
    assert p["tests"]["flaky"]["verdict"] == "pre-existing" and p["tests"]["flaky"]["pre_existing"] == IDS[:2]
    assert kinds(env) == ["targeted", "base", "full", "rerun", "base_ids"]
    assert env.test_calls[-1][1].endswith("-base") and env.test_calls[-1][2] == IDS[:2]
    assert "they fail the same way without this change" in env.sent[0]["text"]


def test_some_flaky_and_some_pre_existing_are_both_named(env):
    scripted(env)
    env.results["full"] = failing(IDS)
    env.results["rerun"] = failing(IDS[:1], passed=2)
    env.results["base_ids"] = failing(IDS[:1], passed=0)
    p = to_awaiting(env)
    fl = p["tests"]["flaky"]
    assert fl["verdict"] == "flaky and pre-existing" and fl["pre_existing"] == IDS[:1] and fl["flaky"] == IDS[1:]
    assert "some passed on a quiet re-run and the rest fail the same way" in env.sent[0]["text"]


def test_a_failure_the_change_causes_still_fails_the_proposal(env):
    scripted(env)
    env.results["full"] = failing(IDS[:2])
    env.results["rerun"] = failing(IDS[:2], passed=0)  # still fails alone...
    env.results["base_ids"] = failing(IDS[:1], passed=0)  # ...and only the first fails on the base commit
    p = file_one(env)
    drive(env)
    env.jobs["job1"]["status"] = "done"
    r = final(env)
    assert r["did"] == "failed" and "full suite fails (2 failed" in r["why"] and env.sent == []
    assert not env.cmds("push", "origin", f"proposals/{p['id']}:proposals/{p['id']}")


@pytest.mark.parametrize("full", [
    {"rc": 124, "passed": 5, "failed": 0, "errors": 0, "tail": "[killed: timed out]", "failed_ids": []},  # timeout
    {"rc": 1, "passed": 100, "failed": 2, "errors": 0, "tail": "", "failed_ids": []},  # failures it cannot name
    {"rc": 1, "passed": 100, "failed": 1, "errors": 0, "tail": "", "failed_ids": IDS},  # more names than failures
    {"rc": 1, "passed": 100, "failed": 400, "errors": 0, "tail": "",
     "failed_ids": [f"tests/t.py::t{i}" for i in range(400)]},  # a mass failure is a real break
])
def test_a_failure_that_cannot_be_explained_is_never_called_flaky(env, full):
    scripted(env)
    env.results["full"] = full
    file_one(env)
    drive(env)
    env.jobs["job1"]["status"] = "done"
    r = final(env)
    assert r["did"] == "failed" and "full suite fails" in r["why"]
    assert "rerun" not in kinds(env)  # nothing was re-run and nothing was waved through


def test_a_rerun_that_crashes_or_a_base_that_cannot_be_checked_is_not_an_excuse(env):
    scripted(env)
    env.results["full"] = failing(IDS[:1])
    env.results["rerun"] = {"rc": 124, "passed": 0, "failed": 0, "errors": 0, "tail": "", "failed_ids": []}
    p = file_one(env)
    drive(env)
    env.jobs["job1"]["status"] = "done"
    assert final(env)["did"] == "failed"
    row = proposals.get(p["id"])
    assert proposals.failing_on_base(env, row, IDS[:1]) == set()  # a clean base run: nothing fails there
    env.fail["worktree add --detach"] = (1, "fatal")  # the base checkout fails
    assert proposals.failing_on_base(env, row, IDS[:1]) is None


def test_the_pytest_summary_lines_become_test_ids():
    text = ("....F.\n=== short test summary info ===\n"
            "FAILED tests/test_a.py::test_one - assert 1 == 2\n"
            "FAILED tests/test_b.py::test_two[x - y] - AssertionError: nope\n"
            "FAILED tests/test_b.py::test_param[a b] \n"
            "ERROR tests/test_c.py::test_fix - RuntimeError\n"
            "ERROR tests/test_c.py::test_fix - RuntimeError\n"
            "ERROR tests/test_broken.py\n"
            "== 3 failed, 2 errors, 90 passed in 4.0s ==\n")
    assert proposals.parse_failed_ids(text) == ["tests/test_a.py::test_one", "tests/test_b.py::test_two[x - y]",
                                                "tests/test_b.py::test_param[a b]", "tests/test_c.py::test_fix",
                                                "tests/test_broken.py"]
    assert proposals.parse_failed_ids("") == [] and proposals.parse_failed_ids("5 passed in 1s") == []


def test_the_real_runner_reports_the_failed_ids_the_guard_re_runs(settings, tmp_path):
    proj = tmp_path / "proj2"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_mix.py").write_text("def test_ok():\n    assert True\n\n\ndef test_bad():\n    assert False\n")
    e = Env(settings=settings)
    res = e.run_tests(proj, None, 120)
    assert res["failed"] == 1 and res["failed_ids"] == ["tests/test_mix.py::test_bad"]
    again = e.run_tests(proj, res["failed_ids"], 120)  # exactly what the guard does with them
    assert again["rc"] == 1 and again["failed"] == 1 and again["passed"] == 0
