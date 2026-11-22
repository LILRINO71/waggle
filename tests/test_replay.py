"""The replay suite (tools/replay): dataset, judge, base-against-head compare, throwaway home, worker harness, runner.

The engine and the judge are mocked everywhere; nothing here reaches Ollama, Claude or the owner's files. One test runs
the real Inspect task in its venv with a fake worker and a fake judge, and is skipped when that venv is not installed.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPLAY = Path(__file__).resolve().parents[1] / "tools" / "replay"
sys.path.insert(0, str(REPLAY))

import compare  # noqa: E402
import dataset  # noqa: E402
import judge  # noqa: E402
import run as replay_run  # noqa: E402

FAKE_KEY = "sk-ant-" + "Ab3dE6gH" * 5


# ---- helpers ----------------------------------------------------------------------------------------------------

def make_traces(home, rows, steps=()):
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "traces.db")
    con.execute("""CREATE TABLE traces (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, request TEXT, reply TEXT,
                   route TEXT, feedback INTEGER DEFAULT 0, correction TEXT, origin TEXT, turn_outcome TEXT)""")
    con.execute("CREATE TABLE steps (trace_id INTEGER, idx INTEGER, tool TEXT)")
    for r in rows:
        con.execute("INSERT INTO traces (id, time, request, reply, route, feedback, correction, origin, turn_outcome) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["id"], r.get("time", 1000.0 + r["id"]), r["request"], r.get("reply", "an answer"),
                     r.get("route", "chat"), r.get("feedback", 0), r.get("correction"), r.get("origin"),
                     r.get("outcome", "ok")))
    for tid, tool in steps:
        con.execute("INSERT INTO steps VALUES (?,?,?)", (tid, 0, tool))
    con.commit()
    con.close()


@pytest.fixture
def home(tmp_path):
    return tmp_path / "realhome"


# ---- dataset ----------------------------------------------------------------------------------------------------

def test_the_dataset_takes_ok_turns_and_corrected_ones_and_only_the_owners_own(home):
    make_traces(home, [
        {"id": 1, "request": "what is my plan today", "reply": "You have the FTC meeting."},
        {"id": 2, "request": "canary ping", "origin": "canary"},
        {"id": 3, "request": "bench question one", "origin": "bench"},
        {"id": 4, "request": "how long is a mile in km", "origin": "user"},
        {"id": 5, "request": "convert 5 miles", "reply": "8.04 km", "feedback": -1, "correction": "no, I meant 3 miles",
         "outcome": "degraded"},
        {"id": 6, "request": "/waggle status", "route": "command"},
        {"id": 7, "request": "yes", "route": "approval"},
        {"id": 8, "request": "an old turn with no recorded outcome", "outcome": None},
        {"id": 9, "request": "something that failed", "outcome": "failed"},
    ])
    cases = dataset.build(n_ok=10, n_hard=10, home=home)
    ids = {c["id"]: c for c in cases}
    assert set(ids) == {"t1", "t4", "t5"}
    assert ids["t5"]["kind"] == "corrected" and ids["t5"]["category"] == "corrected"
    assert ids["t5"]["correction"] == "no, I meant 3 miles"
    assert ids["t1"]["reference"] == "You have the FTC meeting."


def test_turns_that_acted_are_left_out_because_the_replay_has_no_tools(home):
    make_traces(home, [{"id": 1, "request": "make a doc called plan"}, {"id": 2, "request": "what is a capacitor"},
                       {"id": 3, "request": "look this up for me"}],
                steps=[(1, "mcp__docs__create"), (3, "WebSearch"), (3, "Read")])
    assert {c["id"] for c in dataset.build(home=home)} == {"t2", "t3"}
    with_acts = {c["id"]: c for c in dataset.build(home=home, include_acts=True)}
    assert with_acts["t1"]["category"] == "acts" and with_acts["t3"]["category"] == "chat"


def test_the_newest_turns_come_first_and_n_limits_each_kind(home):
    make_traces(home, [{"id": i, "request": f"question number {i}"} for i in range(1, 8)]
                + [{"id": 20 + i, "request": f"wrong answer {i}", "feedback": -1, "correction": "no"} for i in range(3)])
    cases = dataset.build(n_ok=3, n_hard=2, home=home)
    assert [c["id"] for c in cases if c["kind"] == "ok"] == ["t7", "t6", "t5"]
    assert [c["id"] for c in cases if c["kind"] == "corrected"] == ["t22", "t21"]


def test_a_correction_in_the_learning_ledger_marks_a_hard_case(home):
    make_traces(home, [{"id": 1, "request": "what time is it in Tokyo"}])
    con = sqlite3.connect(home / "knowledge.db")
    con.execute("CREATE TABLE improvements (id INTEGER PRIMARY KEY, trace_id INTEGER, kind TEXT, source TEXT)")
    con.execute("INSERT INTO improvements (trace_id, kind, source) VALUES (1, 'correction', 'trace')")
    con.commit()
    con.close()
    assert [c["kind"] for c in dataset.build(home=home)] == ["corrected"]


def test_secrets_are_redacted_in_every_field_and_the_file_lands_in_the_replay_dir_not_the_repo(home, tmp_path):
    make_traces(home, [{"id": 1, "request": f"is {FAKE_KEY} still valid", "reply": f"use {FAKE_KEY} here",
                        "feedback": -1, "correction": f"no, {FAKE_KEY} was wrong"}])
    cases = dataset.build(home=home)
    path = dataset.save(cases, tmp_path / "replay" / "datasets", stamp="x")
    text = path.read_text(encoding="utf-8")
    assert FAKE_KEY not in text and "[REDACTED" in text
    assert path.parent == tmp_path / "replay" / "datasets"
    repo = Path(__file__).resolve().parents[1]
    assert repo not in path.parents
    assert dataset.load(path)[0]["id"] == "t1"


def test_an_empty_or_missing_history_gives_no_cases(home, tmp_path):
    assert dataset.build(home=tmp_path / "nothing") == []
    make_traces(home, [])
    assert dataset.build(home=home) == []


def test_old_datasets_are_pruned(tmp_path):
    d = tmp_path / "ds"
    for i in range(9):
        dataset.save([{"id": "t1"}], d, stamp=f"{i:02d}")
    dataset.prune(d, keep=3)
    assert len(list(d.glob("dataset-*.jsonl"))) == 3


# ---- the judge --------------------------------------------------------------------------------------------------

CASE = {"request": "how many km is 5 miles", "reference": "About 8 km.", "correction": ""}


def test_the_judge_prompt_carries_request_reference_candidate_and_a_correction():
    m = judge.build_messages(dict(CASE, correction="no, 3 miles"), "That is 4.8 km")
    text = m[1]["content"]
    assert "how many km is 5 miles" in text and "About 8 km." in text and "That is 4.8 km" in text
    assert "CORRECTED" in text and "no, 3 miles" in text
    assert "CORRECTED" not in judge.build_messages(CASE, "x")[1]["content"]


def test_judge_verdicts_and_outages():
    assert judge.judge(CASE, "About 8 km.", chat=lambda m, s: {"verdict": "pass", "reason": "ok"}) == (True, "ok")
    assert judge.judge(CASE, "x", chat=lambda m, s: {"verdict": "fail", "reason": "wrong"}) == (False, "wrong")
    assert judge.judge(CASE, "   ")[0] is False  # an empty reply fails without a model call
    assert judge.judge(CASE, "x", chat=lambda m, s: {"verdict": "maybe"})[0] is None

    def down(m, s):
        raise OSError("Ollama down")
    v, why = judge.judge(CASE, "x", chat=down)
    assert v is None and "unavailable" in why


def test_the_judge_uses_the_live_model_and_the_live_options_never_a_num_ctx(monkeypatch):
    from waggle import ollama
    from waggle.config import Settings
    seen = {}

    def post(path, payload, timeout=300, settings=None):
        seen.update(path=path, payload=payload)
        return {"message": {"content": '{"verdict": "pass", "reason": "fine"}'}}
    monkeypatch.setattr(ollama, "_post", post)
    out = judge.default_chat(judge.build_messages(CASE, "x"), judge.SCHEMA)
    assert out["verdict"] == "pass"
    payload = seen["payload"]
    assert payload["model"] == Settings().local_model
    assert payload["options"] == {"temperature": 0}  # what every schema caller sends; no num_ctx
    assert "num_ctx" not in json.dumps(payload) and payload["format"] == judge.SCHEMA and payload["think"] is False


# ---- compare ----------------------------------------------------------------------------------------------------

def res(i, status, tier="model", cat="replay:chat"):
    return {"id": i, "category": cat, "tier": tier, "status": status}


def sides(base_pass, head_pass, n=10, tier="model"):
    b = [res(f"c{i}", "pass" if i < base_pass else "fail", tier) for i in range(n)]
    h = [res(f"c{i}", "pass" if i < head_pass else "fail", tier) for i in range(n)]
    return b, h


def test_the_margin_is_two_cases_or_five_percent_whichever_is_larger():
    assert [compare.margin_for(n) for n in (0, 10, 40, 41, 100)] == [2, 2, 2, 3, 5]


@pytest.mark.parametrize("lost,blocked", [(0, False), (1, False), (2, False), (3, True)])
def test_head_is_worse_only_when_the_net_loss_is_more_than_the_margin(lost, blocked):
    b, h = sides(10, 10 - lost)
    assert compare.compare(b, h)["blocked"] is blocked


def test_gains_offset_losses_in_the_model_tier():
    b = [res("a", "pass"), res("b", "pass"), res("c", "pass"), res("d", "pass"), res("e", "fail"), res("f", "fail")]
    h = [res("a", "fail"), res("b", "fail"), res("c", "fail"), res("d", "pass"), res("e", "pass"), res("f", "pass")]
    c = compare.compare(b, h)
    assert c["net_model_loss"] == 1 and not c["blocked"]


def test_one_lost_deterministic_case_blocks_even_with_gains():
    b = [res("x", "pass", "code", "golden:guard"), res("y", "fail"), res("z", "fail")]
    h = [res("x", "fail", "code", "golden:guard"), res("y", "pass"), res("z", "pass")]
    c = compare.compare(b, h)
    assert c["blocked"] and c["code_lost"] == ["x"] and "deterministic" in c["reasons"][0]


def test_a_skip_on_either_side_drops_the_case_so_an_outage_is_not_a_regression():
    b = [res("a", "pass"), res("b", "pass"), res("c", "pass")]
    h = [res("a", "skip"), res("b", "skip"), res("c", "skip")]
    c = compare.compare(b, h)
    assert c["paired"] == 0 and not c["blocked"]


def test_flips_and_majority_and_the_table():
    b, h = sides(5, 4, n=6)
    assert compare.flips(b, h) == ["c4"]
    assert compare.majority(["pass", "fail", "fail"]) == "fail" and compare.majority(["pass", "pass", "fail"]) == "pass"
    assert compare.majority(["skip"]) == "skip" and compare.majority([]) == "skip"
    lines = compare.table(compare.compare(b, h))
    assert lines[0].startswith("category") and "replay:chat" in lines[1] and "-1" in lines[1]


# ---- the throwaway home -----------------------------------------------------------------------------------------

def make_real_home(tmp_path):
    real = tmp_path / "real"
    vault = tmp_path / "vault"
    (vault / "Me").mkdir(parents=True)
    (vault / "Me" / "Profile.md").write_text("# me\n", encoding="utf-8")
    (vault / ".git").mkdir()
    (vault / ".git" / "HEAD").write_text("x")
    real.mkdir()
    (real / "config.json").write_text(json.dumps({
        "vault": str(vault), "local_model": "qwen-test", "mailwatch_local_model": "gemma-test", "ntfy_topic": "secret-topic",
        "engine_chats": ["8273865620"], "deadman_url": "https://example.invalid/ping", "google_gog": True,
        "calls_enabled": True, "github_watch": True, "vault_git": True}), encoding="utf-8")
    con = sqlite3.connect(real / "memory.db")
    con.execute("CREATE TABLE notes (p TEXT)")
    con.execute("INSERT INTO notes VALUES ('a')")
    con.commit()
    con.close()
    make_traces(real, [{"id": 1, "request": "private question"}])
    (real / "lessons").mkdir()
    (real / "lessons" / "l.json").write_text("{}")
    return real


def test_the_throwaway_home_has_nothing_that_reaches_outside_and_no_traces(tmp_path):
    real = make_real_home(tmp_path)
    root = replay_run.prepare_home(tmp_path / "run" / "home-base", real_home=real)
    cfg = json.loads((root / "home" / "config.json").read_text(encoding="utf-8"))
    assert cfg["local_model"] == "qwen-test" and cfg["mailwatch_local_model"] == "gemma-test"
    assert cfg["ntfy_topic"] == "" and cfg["engine_chats"] == [] and cfg["deadman_url"] == ""
    for k in ("google_gog", "calls_enabled", "github_watch", "vault_git"):
        assert cfg[k] is False
    assert Path(cfg["vault"]) == root / "userhome" / "WaggleVault"
    assert (root / "home" / replay_run.MARK).exists()
    assert (root / "userhome" / "WaggleVault" / "Me" / "Profile.md").exists()
    assert not (root / "userhome" / "WaggleVault" / ".git").exists()
    assert (root / "home" / "memory.db").exists() and (root / "home" / "lessons" / "l.json").exists()
    assert not (root / "home" / "traces.db").exists()  # the replay must not find the answer it is compared with
    assert str(real) not in json.dumps(cfg)


# ---- the worker harness (a fake Waggle tree stands in for the engine) --------------------------------------------

FAKE_TREE = {
    "waggle/__init__.py": "",
    "waggle/config.py": "class Settings:\n    local_model = 'fake'\n    @classmethod\n    def load(cls):\n        return cls()\n",
    "waggle/delivery.py": "class CollectDelivery:\n    messages = []\n",
    "waggle/claude_cli.py": "def find_claude():\n    return 'claude.exe'\ndef ask(*a, **k):\n    raise RuntimeError('spent')\n",
    "waggle/chat_engine.py": (
        "import socket, os\n"
        "from types import SimpleNamespace\n"
        "def claude_available(s):\n    return True, ''\n"
        "def local_answer(chat, text, ctx, s):\n    return 'local says: ' + text\n"
        "def handle_inbound(chat, text, **kw):\n"
        "    assert kw['origin'] == 'bench' and kw['platform'] == 'test'\n"
        "    ok, why = claude_available(kw['settings'])\n"
        "    reply = kw['local'](chat, text, '', kw['settings']) if not ok else 'claude was reachable'\n"
        "    for host, port in (('192.0.2.1', 80), ('api.telegram.org', 443), ('127.0.0.1', int(os.environ.get('LOOP_PORT', '9')))):\n"
        "        try:\n"
        "            socket.create_connection((host, port), timeout=2).close()\n"
        "            reply += ' | OPEN ' + host\n"
        "        except OSError as e:\n"
        "            reply += (' | REFUSED ' if 'replay sandbox' in str(e) else ' | ERR ') + host\n"
        "    reply += \"\\n\\n(Answered by Waggle's small local model because Claude is unavailable right now: x.)\"\n"
        "    return SimpleNamespace(reply=reply, answered_by='local', failure_notice=False, fallback_reason='off', context={})\n"),
}


def make_tree(tmp_path):
    tree = tmp_path / "tree"
    for rel, body in FAKE_TREE.items():
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tree


def run_worker(tmp_path, cmd, req, *, marker=True, tree=None, real=None, extra=None):
    root = tmp_path / "wk"
    (root / "home").mkdir(parents=True, exist_ok=True)
    (root / "userhome").mkdir(exist_ok=True)
    if marker:
        (root / "home" / ".replay-throwaway").write_text("x")
    tree = tree or make_tree(tmp_path)
    env = dict(os.environ, REPLAY_TREE=str(tree), WAGGLE_HOME=str(root / "home"), USERPROFILE=str(root / "userhome"),
               PYTHONPATH=str(tree), PYTHONIOENCODING="utf-8", PYTHONUTF8="1", **(extra or {}))
    if real:
        env["REPLAY_REAL_HOME"] = str(real)
    p = subprocess.run([sys.executable, str(REPLAY / "worker.py"), cmd], input=json.dumps(req), capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60)
    line = next((x for x in p.stdout.splitlines() if x.startswith("REPLAY_RESULT ")), "")
    return json.loads(line[len("REPLAY_RESULT "):]) if line else {"raw": p.stdout + p.stderr}, p.returncode


def test_the_worker_runs_the_engine_with_claude_hidden_origin_bench_and_the_label_stripped(tmp_path):
    out, rc = run_worker(tmp_path, "answer", {"text": "hello there", "chat": "replay:t1"})
    assert rc == 0 and out["reply"].startswith("local says: hello there") and "(Answered by" not in out["reply"]
    assert out["answered_by"] == "local" and out["failure"] is False


def test_the_worker_walls_off_every_address_that_is_not_loopback(tmp_path):
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    try:
        out, _ = run_worker(tmp_path, "answer", {"text": "hi", "chat": "c"}, extra={"LOOP_PORT": str(srv.getsockname()[1])})
    finally:
        srv.close()
    assert "REFUSED 192.0.2.1" in out["reply"] and "REFUSED api.telegram.org" in out["reply"]
    assert "OPEN 127.0.0.1" in out["reply"]  # Ollama lives on loopback: that stays open


def test_the_worker_refuses_a_home_without_the_marker_and_the_real_home(tmp_path):
    out, rc = run_worker(tmp_path, "answer", {"text": "hi"}, marker=False)
    assert rc == 3 and "not a replay throwaway home" in out["error"]
    root = tmp_path / "wk"
    out, rc = run_worker(tmp_path, "answer", {"text": "hi"}, real=root / "home")
    assert rc == 3 and "real Waggle home" in out["error"]


# ---- the runner with a fake runner -------------------------------------------------------------------------------

class FakeTrees:
    def __init__(self, *a, **k):
        self.trees = {"base": "BASE", "head": "HEAD"}

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False


class FakeRunner:
    """replay(): {id: [status]*epochs} from a script; golden(): the fixed cases from a script."""

    def __init__(self, replay_pass, golden_pass=None, noise=None):
        self.replay_pass, self.golden_pass, self.noise = replay_pass, golden_pass or {}, noise or {}
        self.calls = []

    def replay(self, label, tree, ds, ids=None, epochs=1, claude=False):
        self.calls.append(("replay", label, ids, epochs, claude))
        rows = [json.loads(x) for x in Path(ds).read_text(encoding="utf-8").splitlines()]
        out = {}
        for c in rows:
            if ids and c["id"] not in ids:
                continue
            ok = self.replay_pass[label](c["id"], len(self.calls))
            out[c["id"]] = ["pass" if ok else "fail"] * epochs
        return out, {}

    def golden(self, label, tree, path, ids=None, quick=False):
        self.calls.append(("golden", label, ids, quick))
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = []
        for c in data["cases"]:
            if c["needs"] == "claude" or (ids and c["id"] not in ids):
                continue
            ok = self.golden_pass.get(label, lambda i: True)(c["id"])
            rows.append({"id": c["id"], "category": c["category"], "status": "pass" if ok else "fail", "detail": "",
                         "ms": 1})
        return rows


def cases_list(n):
    return [{"id": f"t{i}", "trace_id": i, "kind": "ok", "category": "chat", "request": f"q{i}", "reference": "r",
             "correction": "", "tools": [], "time": 1000.0 + i} for i in range(n)]


def go(tmp_path, runner, n=10, golden="none", **kw):
    return replay_run.run_replay("BASEREF", "HEADREF", cases=n, golden=golden, runner=runner, trees=FakeTrees(),
                                 cases_list=cases_list(n), out_dir=tmp_path / "out", log=lambda *a: None, **kw)


def test_identical_code_is_not_worse_and_the_report_and_summary_are_written(tmp_path):
    r = go(tmp_path, FakeRunner({"base": lambda i, n: True, "head": lambda i, n: True}), golden="full")
    assert r["status"] == "ok" and not r["blocked"] and r["pass_rate"]["base"] == r["pass_rate"]["head"]
    assert "not worse" in r["summary"] and Path(r["report_path"]).exists()
    assert any(line.startswith("golden:guard") for line in r["table"]) and any("replay:chat" in x for x in r["table"])


def test_answers_that_got_worse_by_more_than_the_margin_block_and_say_which(tmp_path):
    bad = {"t0", "t1", "t2", "t3"}
    r = go(tmp_path, FakeRunner({"base": lambda i, n: True, "head": lambda i, n: i not in bad}))
    assert r["blocked"] and r["status"] == "ok" and "worse by 4 cases net" in r["why"]
    assert sorted(r["lost"]) == sorted("r:" + i for i in bad)
    text = replay_run.owner_text(r)
    assert "Why it is not offered" in text and "r:t0" in text


def test_a_lost_deterministic_case_blocks(tmp_path):
    r = go(tmp_path, FakeRunner({"base": lambda i, n: True, "head": lambda i, n: True},
                                {"head": lambda i: i != "saf-1"}), golden="full")
    assert r["blocked"] and "deterministic" in r["why"] and "g:saf-1" in r["why"]


def test_flipped_cases_are_re_run_twice_on_both_sides_and_the_majority_wins(tmp_path):
    state = {"calls": 0}

    def head(i, n):  # t0 fails the first time only: noise
        if i == "t0":
            state["calls"] += 1
            return state["calls"] > 1
        return True
    runner = FakeRunner({"base": lambda i, n: True, "head": head})
    r = go(tmp_path, runner)
    assert r["flipped_first_pass"] == ["r:t0"]
    assert ("replay", "head", ["t0"], 2, False) in runner.calls and ("replay", "base", ["t0"], 2, False) in runner.calls
    assert r["lost"] == []  # two of three votes passed


def test_no_history_means_only_the_fixed_cases_run(tmp_path):
    r = replay_run.run_replay("b", "h", golden="full", runner=FakeRunner({}), trees=FakeTrees(), cases_list=[],
                              out_dir=tmp_path / "out", log=lambda *a: None)
    assert r["status"] == "ok" and r["dataset"]["cases"] == 0 and any("no replayable turns" in n for n in r["notes"])
    assert all(line.split()[0].startswith(("golden", "category")) for line in r["table"])


def test_a_runner_that_cannot_run_is_a_skip_never_a_block_and_cleans_up(tmp_path):
    class Dead(FakeRunner):
        def replay(self, *a, **k):
            raise replay_run.ReplayError("the Inspect venv is missing")
    r = go(tmp_path, Dead({}))
    assert r["status"] == "skipped" and not r["blocked"] and "venv is missing" in r["why"]
    assert r["summary"].startswith("Replay skipped")
    assert not (tmp_path / "out" / "runs" / r["stamp"]).exists()  # the throwaway homes are gone

    class Slow(FakeRunner):
        def replay(self, *a, **k):
            raise replay_run.ReplayTimeout("the replay ran out of time")
    assert go(tmp_path, Slow({}))["status"] == "skipped"

    class Crash(FakeRunner):
        def replay(self, *a, **k):
            raise ZeroDivisionError("boom")
    r = go(tmp_path, Crash({}))
    assert r["status"] == "skipped" and "ZeroDivisionError" in r["why"]


def test_claude_answers_need_the_gate_and_are_capped_to_the_newest(tmp_path):
    runner = FakeRunner({"base": lambda i, n: True, "head": lambda i, n: True})
    r = go(tmp_path, runner, claude=True, claude_max=3, claude_gate=lambda: (False, "the 5-hour window is at 71%"))
    assert not r["claude"] and any("71%" in n for n in r["notes"]) and not any(c[-1] is True for c in runner.calls if c[0] == "replay")
    runner = FakeRunner({"base": lambda i, n: True, "head": lambda i, n: True})
    r = go(tmp_path, runner, claude=True, claude_max=3, claude_gate=lambda: (True, "ok"))
    claude_calls = [c for c in runner.calls if c[0] == "replay" and c[4]]
    assert r["claude"] and len(claude_calls) == 2 and set(claude_calls[0][2]) == {"t9", "t8", "t7"}
    assert any(line.startswith("claude:chat") for line in r["table"])


def test_default_is_local_only(tmp_path):
    runner = FakeRunner({"base": lambda i, n: True, "head": lambda i, n: True})
    go(tmp_path, runner)
    assert not any(c[0] == "replay" and c[4] for c in runner.calls)


def test_folder_refs_are_used_in_place_and_git_refs_get_detached_worktrees(tmp_path):
    folder = tmp_path / "tree"
    (folder / "waggle").mkdir(parents=True)
    (folder / "waggle" / "__init__.py").write_text("")
    with replay_run.Trees(tmp_path, str(folder), str(folder), tmp_path / "r") as t:
        assert t.trees["base"] == str(folder.resolve()) and not t.made
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "overnight").mkdir()  # a plain folder named like a branch must not be taken for a code tree

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one")
    old = os.getcwd()
    os.chdir(repo)
    try:
        git("branch", "overnight")
        with replay_run.Trees(repo, "overnight", "HEAD", tmp_path / "r2") as t:
            assert Path(t.trees["base"]).name == "tree-base" and len(t.made) == 2
    finally:
        os.chdir(old)
    with replay_run.Trees(repo, "HEAD", "HEAD", tmp_path / "r2") as t:
        assert Path(t.trees["base"]).is_dir() and Path(t.trees["head"]).is_dir() and t.trees["base"] != t.trees["head"]
        made = list(t.made)
    assert all(not m.exists() for m in made)
    with pytest.raises(replay_run.ReplayError):
        with replay_run.Trees(repo, "no-such-ref", "HEAD", tmp_path / "r3"):
            pass


def test_read_logs_maps_inspect_scores_to_pass_fail_skip(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    sample = lambda i, v, e="": {"id": i, "scores": {"local_judge": {"value": v, "explanation": e}}}  # noqa: E731
    (d / "a.json").write_text(json.dumps({"samples": [sample("t1", "C"), sample("t2", "I", "wrong"), sample("t3", "N"),
                                                      sample("t1", "I")]}))
    votes, reasons = replay_run.read_logs(d)
    assert votes == {"t1": ["pass", "fail"], "t2": ["fail"], "t3": ["skip"]} and reasons["t2"] == "wrong"


# ---- the real Inspect task, in its venv, with a fake worker and a fake judge --------------------------------------

@pytest.mark.skipif(replay_run.inspect_python() is None, reason="the Inspect venv is not installed")
def test_the_inspect_task_runs_the_solver_and_the_scorer_and_logs_pass_and_fail(tmp_path):
    ds = tmp_path / "ds.jsonl"
    ds.write_text("".join(json.dumps(c) + "\n" for c in cases_list(3)), encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPLAY)!r})\n"
        "import task, judge\n"
        "task.run_worker = lambda python, tree, home, claude, text, chat: {'reply': 'answer to ' + text}\n"
        "judge.default_chat = lambda m, s: {'verdict': 'fail' if 'answer to q1' in m[1]['content'] else 'pass', 'reason': 'r'}\n"
        f"sys.exit(task.main(['--dataset', {str(ds)!r}, '--tree', 'T', '--home', 'H', '--log-dir', {str(tmp_path / 'logs')!r}, '--epochs', '2']))\n",
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "INSPECT_TELEMETRY"}
    p = subprocess.run([str(replay_run.inspect_python()), str(driver)], capture_output=True, text=True, env=env,
                       timeout=240)
    assert p.returncode == 0, p.stdout + p.stderr
    votes, reasons = replay_run.read_logs(tmp_path / "logs")
    assert votes == {"t0": ["pass", "pass"], "t1": ["fail", "fail"], "t2": ["pass", "pass"]}
