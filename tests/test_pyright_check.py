"""waggle/pyright_check.py: only errors the change ADDED are reported; a warning, never a failure."""
import shutil
import subprocess
from pathlib import Path

import pytest
from waggle import proposals, pyright_check as pc


def test_new_errors_ignores_moved_lines_and_old_errors():
    base = [("a.py", "reportAttributeAccessIssue", "x has no y", 3), ("a.py", "reportCallIssue", "bad call", 9)]
    head = [("a.py", "reportAttributeAccessIssue", "x has no y", 30), ("a.py", "reportCallIssue", "bad call", 9),
            ("a.py", "reportCallIssue", "bad call", 12), ("b.py", "reportIndexIssue", "no index", 1)]
    assert pc.new_errors(head, base) == [("a.py", "reportCallIssue", "bad call", 12), ("b.py", "reportIndexIssue", "no index", 1)]


def test_summary_wording():
    assert pc.summary(None) == ""
    clean = {"checked": ["a.py"], "new": [], "before": 1, "after": 1}
    assert "no new errors in the 1 .py file" in pc.summary(clean)
    bad = {"checked": ["a.py"], "new": [("a.py", "r", f"msg {i}\nmore", i) for i in range(5)], "before": 0, "after": 5}
    s = pc.summary(bad)
    assert "5 new errors" in s and "a.py:0 msg 0" in s and "and 2 more" in s and "warning only" in s


def test_report_runs_head_and_base_and_cleans_up(tmp_path):
    wt = tmp_path / "wt"
    (wt / "waggle").mkdir(parents=True)
    (wt / "waggle" / "a.py").write_text("x = 1\n")
    (wt / "README.md").write_text("x")
    calls = []

    def git(args, cwd=None):
        base = Path(args[args.index("-o") + 1])
        import tarfile
        src = tmp_path / "srcbase"
        (src / "waggle").mkdir(parents=True, exist_ok=True)
        (src / "waggle" / "a.py").write_text("x = 0\n")
        with tarfile.open(base, "w") as t:
            t.add(src / "waggle", arcname="waggle")
        return 0, ""

    def runner(root, files):
        calls.append((Path(root).name, list(files)))
        return [("waggle/a.py", "r", "m", 1)] if Path(root).name == "wt" else []
    scratch = tmp_path / "scratch"
    rep = pc.report(git, wt, "b" * 40, ["waggle/a.py", "README.md", "waggle/gone.py"], scratch, runner=runner)
    assert [c[0] for c in calls] == ["wt", "scratch"] and calls[0][1] == ["waggle/a.py"]
    assert len(rep["new"]) == 1 and not scratch.exists()
    assert pc.report(git, wt, "b" * 40, ["README.md"], scratch, runner=runner) is None


def test_env_report_is_empty_when_off_or_missing(settings, tmp_path, monkeypatch):
    e = proposals.Env(settings=settings, repo=tmp_path, write_root=tmp_path / "w")
    assert e.pyright_report(tmp_path, "b" * 40, ["a.py"]) == ""  # conftest: pyright "not installed"
    monkeypatch.setattr(pc, "available", lambda: True)
    settings.proposals_pyright = False
    assert e.pyright_report(tmp_path, "b" * 40, ["a.py"]) == ""
    settings.proposals_pyright = True
    monkeypatch.setattr(pc, "report", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert "could not run" in e.pyright_report(tmp_path, "b" * 40, ["a.py"])


@pytest.mark.skipif(not (Path.home() / ".waggle-tools/pyright/node_modules/pyright/index.js").exists() or not shutil.which("node"),
                    reason="pyright is not installed")
def test_real_pyright_finds_only_the_added_error(tmp_path, monkeypatch):
    monkeypatch.undo()  # the conftest stub is not wanted here
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "m.py").write_text("def f(a: int) -> int:\n    return a\n\nf(1)\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    base = run("rev-parse", "HEAD").strip()
    (repo / "m.py").write_text("def f(a: int) -> int:\n    return a\n\nf(1)\nf('x')\n")
    assert pc.available()
    rep = pc.report(lambda a, c=None: (lambda p: (p.returncode, p.stdout))(
        subprocess.run(["git", *a], cwd=c or repo, capture_output=True, text=True)), repo, base, ["m.py"], tmp_path / "s")
    assert len(rep["new"]) == 1 and rep["before"] == 0 and "warning only" in pc.summary(rep)
