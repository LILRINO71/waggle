"""Shared rules of the real-world tools (waggle/tools_common.py): inputs, outputs, processes, error shape."""
import json
import subprocess
import sys

import pytest
from waggle import policy
from waggle import tools_common as tc
from waggle.tools_common import ToolError


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Waggle may write only under tmp_path/ok; the tool-output folder is tmp_path/jobs (the isolated Settings)."""
    ok = tmp_path / "ok"
    ok.mkdir()
    monkeypatch.setattr(tc, "writable_roots", lambda settings=None: [policy.norm(ok), policy.norm(tmp_path / "jobs")])
    return ok


def test_read_path_refuses_missing_secret_and_wrong_type(tmp_path):
    f = tmp_path / "part.step"
    f.write_text("x")
    assert tc.read_path(str(f), {".step"}) == f
    with pytest.raises(ToolError, match="not found"):
        tc.read_path(str(tmp_path / "nope.step"))
    with pytest.raises(ToolError, match="not supported"):
        tc.read_path(str(f), {".stl"})
    with pytest.raises(ToolError, match="no input"):
        tc.read_path("")
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=abc")
    with pytest.raises(ToolError, match="secrets"):
        tc.read_path(str(secret))


def test_output_lands_next_to_a_writable_source_else_in_tool_output(tmp_path, roots, isolated_home):
    inside = roots / "a.step"
    inside.write_text("x")
    assert tc.out_path(inside, ".stl") == roots / "a.stl"
    outside = tmp_path / "Downloads" / "b.step"
    outside.parent.mkdir()
    outside.write_text("x")
    dst = tc.out_path(outside, ".stl", settings=isolated_home)
    assert dst.parent == tmp_path / "jobs" / "tool-output" and dst.name == "b.stl" and dst.parent.exists()


def test_never_overwrites_a_taken_name(roots):
    src = roots / "a.step"
    src.write_text("x")
    (roots / "a.stl").write_text("old")
    first = tc.out_path(src, ".stl")
    assert first.name == "a (2).stl"
    first.write_text("new")
    assert tc.out_path(src, ".stl").name == "a (3).stl"
    assert (roots / "a.stl").read_text() == "old"


def test_explicit_out_must_be_writable_and_gets_the_right_extension(tmp_path, roots, isolated_home):
    src = roots / "a.step"
    src.write_text("x")
    assert tc.out_path(src, ".pdf", str(roots / "report.txt")).name == "report.pdf"
    assert tc.out_path(src, ".pdf", "rel.pdf").parent == roots  # relative: next to the source
    with pytest.raises(ToolError, match="may not write"):
        tc.out_path(src, ".pdf", str(tmp_path / "elsewhere" / "x.pdf"))


def test_run_returns_output_and_kills_on_timeout():
    code, out, err = tc.run([sys.executable, "-c", "import sys; print('hi'); print('e', file=sys.stderr)"])
    assert (code, out.strip(), err.strip()) == (0, "hi", "e")
    with pytest.raises(subprocess.TimeoutExpired):
        tc.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)


def test_guarded_turns_every_failure_into_json():
    @tc.guarded
    def boom(args):
        raise ToolError("it broke", "try again")

    @tc.guarded
    def odd(args):
        raise ValueError("weird")

    @tc.guarded
    def slow(args):
        raise subprocess.TimeoutExpired("x", 5)

    @tc.guarded
    def fine(args, extra=1):
        return tc.ok(n=args["n"] + extra)

    assert json.loads(boom({})) == {"ok": False, "error": "it broke", "hint": "try again"}
    assert "ValueError: weird" in json.loads(odd({}))["error"]
    assert "timed out" in json.loads(slow({}))["error"]
    assert json.loads(fine({"n": 1}, extra=2)) == {"ok": True, "n": 3}
    assert json.loads(fine({"n": 0})) == {"ok": True, "n": 1}


def test_which_finds_globs_and_human_bytes(tmp_path):
    exe = tmp_path / "v2" / "tool.exe"
    exe.parent.mkdir()
    exe.write_text("x")
    (tmp_path / "v1").mkdir()
    (tmp_path / "v1" / "tool.exe").write_text("x")
    assert tc.which("definitely-not-on-path-xyz", extra=[str(tmp_path / "v*" / "tool.exe")]) == str(exe)  # newest first
    assert tc.which("definitely-not-on-path-xyz") == ""
    assert tc.human_bytes(512) == "512 B" and tc.human_bytes(1536) == "1.5 KB" and tc.human_bytes(5 * 1024 ** 2) == "5.0 MB"
