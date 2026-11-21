"""Type-check warning for proposal builds: which pyright errors did the change ADD in the files it touched?

Runs pyright (pinned, installed under ~/.waggle-tools/pyright with `npm install --prefix ... pyright@1.1.409`) on the
touched .py files at the new commit and at the base commit, and reports the errors that exist only after the change
(matched by file, rule and message, so moved lines do not count). It is a warning for the owner's merge message and
never stops a proposal. pyright has no telemetry; nothing here uses the network.

Off switch: config `proposals_pyright` false. Removal: delete this file, tests/test_pyright_check.py and the two
`pyright` lines in waggle/proposals.py.
"""
import json
import os
import shutil
import subprocess
import tarfile
from collections import Counter
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TIMEOUT_S = 300


def entry():
    p = Path(os.environ.get("WAGGLE_PYRIGHT") or Path.home() / ".waggle-tools" / "pyright" / "node_modules" / "pyright" / "index.js")
    return p if p.exists() else None


def available():
    return entry() is not None and shutil.which("node") is not None


def run(root, files, python=None, timeout=TIMEOUT_S):
    """[(file, rule, message, line)] of the ERROR diagnostics for `files` (relative to root). Raises on a pyright crash."""
    import sys
    cmd = [shutil.which("node"), str(entry()), "--outputjson", "--pythonpath", python or sys.executable, *files]
    p = subprocess.run(cmd, cwd=str(root), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                       creationflags=NO_WINDOW)
    data = json.loads(p.stdout.decode("utf-8", "replace"))  # exit 1 just means "errors found"
    out = []
    for d in data.get("generalDiagnostics", []):
        if d.get("severity") != "error":
            continue
        rel = os.path.relpath(d.get("file", ""), str(root)).replace("\\", "/")
        out.append((rel, d.get("rule", ""), d.get("message", ""), d.get("range", {}).get("start", {}).get("line", 0) + 1))
    return out


def new_errors(head, base):
    """The head diagnostics not matched by a base one (same file, rule, message), as [(file, rule, message, line)]."""
    left = Counter((f, r, m) for f, r, m, _l in base)
    out = []
    for f, r, m, line in head:
        if left[(f, r, m)] > 0:
            left[(f, r, m)] -= 1
        else:
            out.append((f, r, m, line))
    return out


def export_base(git, wt, base, dest):
    """Unpack the base commit's tree into `dest` (git archive, no checkout, the worktree is left alone)."""
    dest = Path(dest)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    tar = dest.parent / (dest.name + ".tar")
    rc, out = git(["archive", "--format=tar", "-o", str(tar), base], wt)
    if rc:
        raise RuntimeError("git archive failed: " + out[:120])
    with tarfile.open(tar) as t:
        t.extractall(dest, filter="data")
    tar.unlink(missing_ok=True)


def report(git, wt, base, files, scratch, runner=run):
    """{"checked": [files], "new": [(file, rule, message, line)], "before": n} or None when nothing could be checked.
    `git(args, cwd)` is the Env's git; the scratch folder is removed afterwards."""
    wt = Path(wt)
    py = [f for f in files if f.endswith(".py") and (wt / f).exists()]
    if not py:
        return None
    try:
        head = runner(wt, py)
        export_base(git, wt, base, scratch)
        base_py = [f for f in py if (Path(scratch) / f).exists()]
        before = runner(scratch, base_py) if base_py else []
        return {"checked": py, "new": new_errors(head, before), "before": len(before), "after": len(head)}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def summary(rep):
    """One or two plain lines for the owner's merge message."""
    if not rep:
        return ""
    new = rep["new"]
    if not new:
        return f"Type check (pyright, warning only): no new errors in the {len(rep['checked'])} .py file(s) it touched."
    shown = "; ".join(f"{f}:{ln} {m.splitlines()[0][:90]}" for f, _r, m, ln in new[:3])
    more = f" and {len(new) - 3} more" if len(new) > 3 else ""
    return (f"Type check (pyright, warning only): {len(new)} new error{'s' if len(new) != 1 else ''} in the files it "
            f"touched ({rep['before']} before, {rep['after']} after): {shown}{more}.")
