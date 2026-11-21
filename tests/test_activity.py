"""Activity model: ranked workstreams from synthetic session logs, HEAD logs, owner turns and upcoming items."""
import json
import time
from pathlib import Path

import pytest

from waggle import activity, autonomy_store, traces

NOW = float(int(time.time()))  # logs written now must fall inside the window
HOUR = 3600.0
DAY = 86400.0


def iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def user_line(t, text, cwd, session="s1", **extra):
    return {"type": "user", "timestamp": iso(t), "sessionId": session, "cwd": cwd, "isSidechain": False,
            "userType": "external", "entrypoint": "claude-desktop", "message": {"role": "user", "content": text},
            **extra}


def tool_line(t, name, inp, cwd, session="s1", sidechain=False, entry="claude-desktop"):
    return {"type": "assistant", "timestamp": iso(t), "sessionId": session, "cwd": cwd, "isSidechain": sidechain,
            "userType": "external", "entrypoint": entry,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": inp}]}}


def result_line(t, cwd, session="s1"):
    return {"type": "user", "timestamp": iso(t), "sessionId": session, "cwd": cwd, "isSidechain": False,
            "userType": "external", "entrypoint": "claude-desktop",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "SECRET TOOL OUTPUT"}]}}


def write_log(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for d in lines:
            fh.write(json.dumps(d) + "\n")


@pytest.fixture
def world(settings, tmp_path, monkeypatch):
    # pytest's folders live in the temp folder, which the resolver otherwise ignores
    monkeypatch.setattr(activity, "SKIP_DIRS", ("/.claude/", "/.waggle", "/.hermes"))
    work = tmp_path / "work"
    for name in ("alpha", "beta", "gamma"):
        (work / name / ".git" / "logs").mkdir(parents=True)
    # a worktree of alpha outside the watch root
    wt = tmp_path / "trees" / "alpha-feature"
    wt.mkdir(parents=True)
    (work / "alpha" / ".git" / "worktrees" / "alpha-feature" / "logs").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {work / 'alpha' / '.git' / 'worktrees' / 'alpha-feature'}\n")
    settings.watch_roots = [str(work)]
    settings.save()
    return {"work": work, "wt": wt, "cc": tmp_path / "cc", "s": settings}


def repos(world):
    return {str(world["work"] / n): "" for n in ("alpha", "beta", "gamma")}


def test_command_shapes_keep_programs_and_drop_literals():
    assert activity.command_shape('cd /x/y && git add -A && git commit -m "fix: thing" && git push origin main') \
        == "git add > git commit > git push"
    assert activity.command_shape("python -m pytest -q -p no:cacheprovider tests/test_x.py") == "python -m pytest"
    assert activity.command_shape("C:/Python/python.exe tools/build.py --fast") == "python build.py"
    assert activity.command_shape('gh pr create --title "x" --body "y"') == "gh pr create"
    assert activity.command_shape("git -C C:/repo status") == "git status"
    multi_line_message = 'git commit -m "fix\n\nCo-Authored-By: X <x@y>" && git push'
    assert activity.command_shape(multi_line_message) == "git commit > git push"  # the message is one literal
    assert activity.command_shape('python -c "\nfrom x import y\nfor i in y:\n    print(i)\n"') == "python -c"
    assert activity.command_shape("for f in *.py; do wc -l $f; done") == "wc"
    assert activity.command_shape('"C:/Program Files/Py/python.exe" -m pytest -q') == "python -m pytest"
    assert activity.command_shape('git commit -m "a; b && c" && git push') == "git commit > git push"
    assert activity.command_shape("node tools/x.js 2>&1 | tail -5") == "node > tail"
    assert activity.is_read_only("git status > ls")
    assert not activity.is_read_only("git status > git push")


def test_only_the_owners_typed_turns_count_and_no_content_is_kept(world):
    work = world["work"]
    secret = "please refactor the zebra ledger module before the weekend demo"
    lines = [user_line(NOW - 2 * HOUR, secret, str(work / "alpha")),
             tool_line(NOW - 2 * HOUR + 10, "Edit", {"file_path": str(work / "alpha" / "src" / "ledger.py")},
                       str(work / "alpha")),
             result_line(NOW - 2 * HOUR + 11, str(work / "alpha")),
             user_line(NOW - HOUR, "<task-notification>agent finished</task-notification>", str(work / "alpha")),
             user_line(NOW - HOUR, "sub-agent brief", str(work / "alpha"), isSidechain=True),
             user_line(NOW - HOUR, "meta line", str(work / "alpha"), isMeta=True),
             {"type": "ai-title", "aiTitle": "Ledger refactor", "sessionId": "s1"}]
    write_log(world["cc"] / "proj" / "s1.jsonl", lines)
    # a `claude -p` run (Waggle's own model call) is machine work
    write_log(world["cc"] / "tmp" / "m1.jsonl", [
        {**user_line(NOW - HOUR, "classify this", str(work / "beta"), session="m1"), "entrypoint": "sdk-cli"}])
    snap = activity.refresh(world["s"], now=NOW, claude_dir=world["cc"], repos=repos(world))
    ws = {w["key"]: w for w in snap["workstreams"]}
    assert set(ws) == {"alpha"}
    assert ws["alpha"]["evidence"]["cc_turns_7d"] == 1 and ws["alpha"]["evidence"]["files_touched_7d"] == 1
    assert ws["alpha"]["topic"] == "Ledger refactor"
    raw = Path(autonomy_store.db().execute("PRAGMA database_list").fetchone()[2]).read_bytes()
    assert secret.encode() not in raw and b"SECRET TOOL OUTPUT" not in raw and b"ledger.py" not in raw
    assert b"sub-agent brief" not in raw


def test_ranking_prefers_recent_work_and_maps_worktrees_and_generic_folders(world):
    work, wt = world["work"], world["wt"]
    old = NOW - 9 * DAY
    write_log(world["cc"] / "p" / "old.jsonl", [user_line(old + i, f"beta task {i}", str(work / "beta"), "old")
                                                for i in range(6)])
    # owner typed in the watch root (many projects): the turn belongs to what was edited next (gamma)
    write_log(world["cc"] / "p" / "new.jsonl", [
        user_line(NOW - 3 * HOUR, "fix it", str(work), "new"),
        tool_line(NOW - 3 * HOUR + 60, "Write", {"file_path": str(work / "gamma" / "a.py")}, str(work), "new"),
        user_line(NOW - HOUR, "now the feature", str(wt), "new"),
        tool_line(NOW - HOUR + 60, "Edit", {"file_path": str(wt / "b.py")}, str(wt), "new")])
    # a sub-agent's edits count, at a lower weight
    write_log(world["cc"] / "p" / "new" / "subagents" / "agent-1.jsonl", [
        tool_line(NOW - HOUR + 90, "Edit", {"file_path": str(work / "gamma" / "c.py")}, str(work), "new", True)])
    snap = activity.refresh(world["s"], now=NOW, claude_dir=world["cc"], repos=repos(world))
    keys = [w["key"] for w in snap["workstreams"]]
    assert keys[:2] == ["alpha", "gamma"] or keys[:2] == ["gamma", "alpha"]
    assert "beta" in keys and keys.index("beta") == 2  # nine days old: still listed, ranked last
    by = {w["key"]: w for w in snap["workstreams"]}
    assert by["gamma"]["evidence"]["cc_turns_7d"] == 1  # the generic-folder turn went to gamma
    assert by["alpha"]["evidence"]["cc_turns_7d"] == 1  # the worktree turn went to alpha
    assert by["beta"]["evidence"]["cc_turns_7d"] == 0
    assert by["gamma"]["evidence"]["files_touched_7d"] == 2


def test_turns_whose_origin_is_not_user_contribute_nothing(world):
    traces.record("let's finish the beta release notes", platform="telegram", origin="test")
    traces.record("beta release notes again", platform="telegram", origin="bench")
    traces.record("beta from the cli", platform="cli")  # legacy row from a development run
    snap = activity.refresh(world["s"], now=time.time(), claude_dir=world["cc"], repos=repos(world))
    assert snap["workstreams"] == []
    traces.record("let's finish the beta release notes", platform="telegram", origin="user")
    snap = activity.refresh(world["s"], now=time.time(), claude_dir=world["cc"], repos=repos(world))
    assert [w["key"] for w in snap["workstreams"]] == ["beta"]
    assert snap["workstreams"][0]["evidence"]["chat_turns_7d"] == 1


def test_head_log_commits_and_change_check(world):
    log = world["work"] / "alpha" / ".git" / "logs" / "HEAD"
    sha = "0" * 40
    log.write_text("".join(f"{sha} {sha} Someone <x@y> {int(NOW - h * HOUR)} +0000\tcommit: change {h}\n"
                           for h in (1, 2, 30)) + f"{sha} {sha} Someone <x@y> {int(NOW)} +0000\tcheckout: moving\n")
    wt_log = world["work"] / "alpha" / ".git" / "worktrees" / "alpha-feature" / "logs" / "HEAD"
    wt_log.write_text(f"{sha} {sha} Someone <x@y> {int(NOW - HOUR)} +0000\tcommit (amend): fix\n")
    got = activity.commit_times({str(world["work"] / "alpha"): "alpha"}, now=NOW)
    assert len(got["alpha"]) == 4
    con = autonomy_store.db()
    stored = [r["data"] for r in con.execute("SELECT data FROM reflog")]
    con.close()
    assert all("Someone" not in d and "change" not in d for d in stored)
    snap = activity.build([], got, now=NOW)
    assert snap[0]["key"] == "alpha" and snap[0]["evidence"]["commits_7d"] == 4


def test_logs_are_read_incrementally(world):
    work = world["work"]
    f = world["cc"] / "p" / "s.jsonl"
    write_log(f, [user_line(NOW - 2 * HOUR, "first", str(work / "beta"), "s")])
    activity.refresh(world["s"], now=NOW, claude_dir=world["cc"], repos=repos(world))
    con = autonomy_store.db()
    off1 = con.execute("SELECT offset FROM cc_files").fetchone()[0]
    con.close()
    write_log(f, [user_line(NOW - HOUR, "second", str(work / "beta"), "s")])
    with open(f, "a", encoding="utf-8") as fh:
        fh.write('{"type":"user","partial')  # still being written: not consumed yet
    snap = activity.refresh(world["s"], now=NOW, claude_dir=world["cc"], repos=repos(world))
    con = autonomy_store.db()
    off2 = con.execute("SELECT offset FROM cc_files").fetchone()[0]
    con.close()
    assert off2 > off1 and off2 < f.stat().st_size
    assert snap["workstreams"][0]["evidence"]["cc_turns_7d"] == 2


def test_upcoming_deadlines_raise_a_workstream_and_snapshot_is_stored(world):
    import datetime as dt
    soon = (dt.date.fromtimestamp(NOW) + dt.timedelta(days=1)).isoformat()
    ranked = activity.build([], {}, [], [{"date": soon, "text": "Gamma demo day"}, {"date": soon, "text": "Exam"}],
                            now=NOW)
    keys = [w["key"] for w in ranked]
    assert "deadlines" in keys  # an item that names no workstream still counts
    snap = activity.refresh(world["s"], now=NOW, claude_dir=world["cc"], repos=repos(world))
    assert activity.workstreams()["time"] == snap["time"]
