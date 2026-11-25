"""Live progress of a running agent (waggle/progress.py): status answers can say what a job is doing."""
import json
import subprocess
import sys
import time

from waggle import agent, chat_jobs, objectives, progress
from waggle.core import jobs


def tool_line(name, **inp):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": name,
                                                                     "input": inp}]}})


RESULT = json.dumps({"type": "result", "result": "done", "num_turns": 3})


def test_tracker_keeps_recent_steps_and_written_files(settings):
    t = progress.Tracker("k1", now=1000.0)
    t.feed(tool_line("WebFetch", url="https://example.com/terms"), now=1060.0)
    t.feed(tool_line("Write", file_path="C:/w/research.md", content="x" * 5000), now=1120.0)
    t.feed(tool_line("Bash", command="py -m pytest -q"), now=1180.0)
    t.feed("not json")
    got = progress.read("k1", now=1300.0)
    assert got["steps"] == 3 and got["running_min"] == 5.0 and got["last_step_min_ago"] == 2.0
    assert got["files_written"] == ["C:/w/research.md"] and not got["finished"]
    assert got["recent_steps"][0] == "WebFetch: https://example.com/terms"
    assert all(len(s) < 120 for s in got["recent_steps"])          # never the full content
    assert progress.read("nothing-here") is None


def test_an_agent_run_records_progress_under_its_key(settings):
    def runner(cmd, prompt, env, cwd, timeout):
        return [tool_line("Write", file_path="C:/x/app.js"), RESULT]
    res = agent.run("build it", settings=settings, runner=runner, progress_key="bg-abc")
    got = progress.read("bg-abc")
    assert res.ok and got["finished"] and got["files_written"] == ["C:/x/app.js"]


def test_the_real_process_reader_streams_lines_and_times_out(settings, tmp_path):
    seen = []
    code = "import sys; print('{\"a\":1}'); sys.stdout.flush(); print('{\"type\":\"result\",\"result\":\"ok\"}')"
    out = agent._run_process([sys.executable, "-c", code], "", None, str(tmp_path), 30, on_line=seen.append)
    assert len(seen) == 2 and out[-1].startswith('{"type":"result"')
    t0 = time.time()
    try:
        agent._run_process([sys.executable, "-c", "import time; time.sleep(30)"], "", None, str(tmp_path), 1)
        raise AssertionError("no timeout")
    except subprocess.TimeoutExpired:
        assert time.time() - t0 < 15


def test_background_status_shows_a_running_jobs_progress(settings, monkeypatch):
    jid, key = chat_jobs.start("chat-1", "Build the stoichiometry app and publish it", "coder", now=1000.0)
    t = progress.Tracker(chat_jobs.progress_key({"key": key}), now=1000.0)
    t.feed(tool_line("Write", file_path="C:/Users/x/stoich/index.html"), now=1200.0)
    real = jobs.list
    monkeypatch.setattr(jobs, "list", lambda **k: [dict(j, state="running", started=1000.0) for j in real(**k)])
    row = chat_jobs.status("chat-1", now=1660.0)[0]
    assert row["running_min"] == 11.0
    assert row["progress"]["files_written"] == ["C:/Users/x/stoich/index.html"]
    assert row["progress"]["last_step_min_ago"] == 7.7


def test_objective_status_shows_the_running_steps_progress(settings):
    con = objectives._db()
    con.execute("INSERT INTO objectives (id, title, goal, status, created, updated, origin) VALUES (?,?,?,?,?,?,?)",
                ("o1", "Earn first money", "g", "active", 1.0, 1.0, "telegram"))
    con.execute("INSERT INTO steps (objective_id, idx, title, detail, status) VALUES (?,?,?,?,?)",
                ("o1", 1, "Research", "d", "running"))
    con.commit()
    con.close()
    progress.Tracker("objective-o1").feed(tool_line("WebFetch", url="https://itch.io/docs"))
    st = objectives.status("o1")["steps"][0]
    assert st["progress"]["recent_steps"] == ["WebFetch: https://itch.io/docs"]
