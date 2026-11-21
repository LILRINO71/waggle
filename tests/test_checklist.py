"""Judging a step on a checklist (waggle/checklist.py): code checks what it can, an evidence-only judge (the cascade)
reads the step's artifacts for the rest, files changed outside the write roots fail the step, and the per-requirement
results are kept. Fakes only: no Claude, no Ollama, a temp home."""
import json
import time

import pytest

from waggle import agent, autowork, budget, cascade, checklist, claude_cli, goals, objectives, ollama
from waggle.config import Settings

FUTURE = "2099-01-01"
CLAIM = "SENTINEL-CLAIM I completed everything perfectly and all requirements are satisfied"


@pytest.fixture
def s(settings):
    return Settings.load()


@pytest.fixture
def env(s, tmp_path, monkeypatch):
    root = tmp_path / "work" / "goal"
    root.mkdir(parents=True)
    (tmp_path / "vault").mkdir(exist_ok=True)
    goals.add("g", "Write notes", "because", indicators=[{"name": "proven steps", "measure": {"type": "proven_steps"}}],
              charter={"actions": ["write_note"], "write_roots": [str(root)], "channels": [], "money": 0,
                       "expires": FUTURE}, status="enabled")
    goals.set_autonomy(True)
    monkeypatch.setattr(objectives.quota, "can_escalate", lambda m, s: (True, ""))
    monkeypatch.setattr(objectives, "kick", lambda oid, user=True: None)
    cascade._pressure_cache.update(at=0.0, value=0.0)
    g = goals.get("g")
    return s, g, root.resolve()


class Judge:
    """A local model that answers the judge's question from a script, and keeps every prompt it was shown."""

    def __init__(self, verdicts=None, conf=0.9, quote=None):
        self.verdicts, self.conf, self.quote = verdicts or {}, conf, quote
        self.prompts = []

    def __call__(self, messages, schema=None, settings=None, timeout=None):
        text = messages[-1]["content"]
        self.prompts.append(text)  # what the user turn carried: the requirements and the evidence
        ids = [line.split(":")[0].split()[0] for line in text.split("EVIDENCE:")[0].splitlines()
               if line[:1] == "r" and ":" in line]
        out = []
        for i in ids:
            met = self.verdicts.get(i, True)
            out.append({"id": i, "met": met, "quote": (self.quote if self.quote is not None else "Deadline") if met
                        else "", "missing": "" if met else "not shown in the evidence"})
        return {"results": out, "confidence": self.conf}


def run_eval(env, _unused=None):
    """(started, the before snapshot, None): call before the step does its work."""
    s, g, root = env
    return time.time(), checklist.watch([root], s), None


def evaluate(env, reqs, judge, started, snap, **kw):
    s, g, root = env
    kw.setdefault("pressure", 0.2)
    with budget.background("test"):
        return checklist.evaluate(reqs, started=started, settings=s, goal=g, roots=[root], before_git={}, before=snap,
                                  local=judge, asker=kw.pop("asker", lambda *a, **k: pytest.fail("no Claude call")),
                                  **kw)


# ---- the requirements --------------------------------------------------------------------------------------------

def test_normalize_puts_evidence_first_drops_duplicates_and_caps():
    ev = [{"type": "file_exists", "path": "/x/n.md", "contains": "Deadline"}, {"type": "tests_pass",
          "cmd": ["python", "-m", "pytest"], "cwd": "/x"}]
    reqs = checklist.normalize(["It names the project", {"text": "It lists deadlines", "content": True},
                                "it names the project", "", {"text": "A file", "path": "/x/a.md"}], ev)
    assert [r["id"] for r in reqs] == ["r1", "r2", "r3", "r4", "r5"]
    assert reqs[0]["check"] == ev[0] and "Deadline" in reqs[0]["text"] and reqs[1]["check"] == ev[1]
    assert [r["text"] for r in reqs[2:]] == ["It names the project", "It lists deadlines", "A file"]
    assert reqs[3]["content"] is True and reqs[4]["path"] == "/x/a.md"
    assert len(checklist.normalize([f"req {i}" for i in range(20)])) == checklist.MAX_REQUIREMENTS


def test_normalize_tolerates_junk():
    assert checklist.normalize(None, None) == []
    assert checklist.normalize([None, 3, [], {"no": "text"}], [{"nottype": 1}, "x"]) == []


# ---- code and judge together -------------------------------------------------------------------------------------

def test_every_requirement_met_is_proven_and_each_result_is_recorded(env):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "n.md").write_text("Project: BIOBUZZ\nDeadline: 2026-12-01\n")
    reqs = checklist.normalize(["The note gives a deadline"], [{"type": "file_exists", "path": str(root / "n.md")}])
    out = evaluate(env, reqs, Judge(), started, snap)
    assert out["proven"] and out["missing"] == "" and out["undecided"] is None
    assert [(r["id"], r["by"], r["ok"]) for r in out["results"]] == [("r1", "code", True), ("r2", "judge", True)]
    assert out["results"][1]["why"] == '"Deadline"'            # the quote that shows it
    assert out["checks"][-1]["type"] == "side_damage" and out["checks"][-1]["ok"]
    assert out["judge"]["tier"] == "local"


def test_an_unmet_requirement_means_not_proven_and_says_what_is_missing(env):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "n.md").write_text("Project: BIOBUZZ\n")
    reqs = checklist.normalize(["The note gives a deadline", "The note names the project"],
                               [{"type": "file_exists", "path": str(root / "n.md")}])
    out = evaluate(env, reqs, Judge({"r2": False}, quote="BIOBUZZ"), started, snap)
    assert not out["proven"]
    assert [(r["id"], r["ok"]) for r in out["results"]] == [("r1", True), ("r2", False), ("r3", True)]  # partial
    assert out["missing"] == "[r2] The note gives a deadline: not shown in the evidence"
    assert checklist.summary(out["checks"]).startswith("3 of 4 requirements met; missing: The note gives a deadline")


def test_a_failed_code_check_skips_the_judge_call(env):
    s, g, root = env
    started, snap, _ = run_eval(env, None)  # nothing is written
    reqs = checklist.normalize(["It gives a deadline"], [{"type": "file_exists", "path": str(root / "n.md")}])
    judge = Judge()
    out = evaluate(env, reqs, judge, started, snap)
    assert not out["proven"] and judge.prompts == []  # no model was asked about a step code already failed
    assert [r["ok"] for r in out["results"]] == [False, False]
    assert "does not exist" in out["results"][0]["why"] and "not judged" in out["results"][1]["why"]


def test_a_file_outside_the_write_roots_cannot_satisfy_file_evidence(env, tmp_path):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (tmp_path / "elsewhere.md").write_text("x")
    reqs = checklist.normalize([], [{"type": "file_exists", "path": str(tmp_path / "elsewhere.md")}])
    out = evaluate(env, reqs, Judge(), started, snap)
    assert not out["proven"] and "outside the write roots" in out["results"][0]["why"]


def test_no_requirements_proves_nothing(env):
    started, snap, _ = run_eval(env, None)
    assert evaluate(env, [], Judge(), started, snap)["proven"] is False


def test_a_file_requirement_with_a_path_is_decided_by_code(env):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "a.md").write_text("hello")
    reqs = checklist.normalize([{"text": "a.md is written", "path": str(root / "a.md")},
                                {"text": "b.md is written", "path": str(root / "b.md")}])
    judge = Judge()
    out = evaluate(env, reqs, judge, started, snap)
    assert [r["ok"] for r in out["results"]] == [True, False] and judge.prompts == []


# ---- the judge sees evidence, never a claim ----------------------------------------------------------------------

def test_the_judge_reads_the_changed_files_and_the_code_results_and_nothing_else(env):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "n.md").write_text("Project: BIOBUZZ\nDeadline: 2026-12-01\n")
    (root / "old.md").write_text("untouched?")
    reqs = checklist.normalize(["The note gives a deadline"], [{"type": "file_exists", "path": str(root / "n.md")}])
    judge = Judge()
    evaluate(env, reqs, judge, started, snap, deliverable=CLAIM)
    seen = judge.prompts[0]
    assert "Deadline: 2026-12-01" in seen and "(added)" in seen              # the artifact
    assert "RESULTS OF CHECKS RUN BY CODE" in seen and "exists, written during this step" in seen
    assert "r2: The note gives a deadline" in seen
    assert "SENTINEL-CLAIM" not in seen and "completed everything" not in seen   # a claim is never evidence
    assert "DELIVERABLE" not in seen                                              # no requirement is marked content


def test_the_deliverable_is_shown_only_for_content_requirements_and_labelled_as_the_work(env):
    started, snap, _ = run_eval(env, None)
    reqs = checklist.normalize([{"text": "The answer names three options", "content": True}])
    judge = Judge(quote="Options")
    evaluate(env, reqs, judge, started, snap, deliverable="Options: A, B, C")
    assert "DELIVERABLE" in judge.prompts[0] and "Options: A, B, C" in judge.prompts[0]
    assert "not a report on it" in judge.prompts[0] and "r1 [content]" in judge.prompts[0]


def test_the_judge_instructions_say_it_is_not_shown_the_workers_own_account():
    assert "NOT shown what the worker said" in checklist.JUDGE_SYSTEM


def test_a_met_requirement_whose_quote_is_not_in_the_evidence_is_not_met(env):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "n.md").write_text("Project: BIOBUZZ\n")
    reqs = checklist.normalize(["The note gives a deadline"])
    liar = Judge(quote="Deadline: 2026-12-01")   # claims met with an invented quote, on every tier
    with budget.background("test"):
        sonnet_says = {"results": [{"id": "r1", "met": True, "quote": "Deadline: 2026-12-01", "missing": ""}]}
        out = checklist.evaluate(reqs, started=started, settings=s, roots=[root], before=snap, local=liar,
                                 asker=lambda *a, **k: {"json": sonnet_says, "text": "", "usage": {}}, pressure=0.2)
    assert not out["proven"]
    assert "could not be judged" in out["results"][0]["why"]    # no tier gave a usable, quoted answer


def test_a_fabricated_local_quote_is_caught_and_a_stronger_judge_asked(env):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "n.md").write_text("Project: BIOBUZZ\nDeadline: 2026-12-01\n")
    reqs = checklist.normalize(["The note gives a deadline"])
    good = {"results": [{"id": "r1", "met": True, "quote": "Deadline: 2026-12-01", "missing": ""}],
            "confidence": 0.9}
    models = []

    def haiku(prompt, model=None, **k):
        models.append(model)
        return {"json": good, "text": "", "usage": {}}
    out = evaluate(env, reqs, Judge(quote="Deadline: tomorrow"), started, snap, asker=haiku)
    assert out["proven"] and out["judge"]["tier"] == "haiku" and models == ["haiku"]


def test_a_judge_that_is_not_sure_and_cannot_ask_a_stronger_one_never_passes_a_requirement(env):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "n.md").write_text("Deadline: 2026-12-01")
    reqs = checklist.normalize(["The note gives a deadline"])
    unsure = {"results": [{"id": "r1", "met": True, "quote": "Deadline", "missing": ""}], "confidence": 0.1}
    out = evaluate(env, reqs, Judge(conf=0.1), started, snap, pressure=0.7, asker=lambda *a, **k: {"json": unsure})
    assert not out["proven"] and out["judge"]["low_confidence"] is True
    assert "not sure" in out["results"][0]["why"]


def test_the_judge_runs_as_cheaply_as_it_can(env):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "n.md").write_text("Deadline: 2026-12-01")
    out = evaluate(env, checklist.normalize(["The note gives a deadline"]), Judge(), started, snap)
    assert out["judge"]["tier"] == "local"
    assert cascade.counts()["avoided"] == 1


def test_an_unusable_judge_is_unmet_for_autonomy_and_undecided_for_objectives(env):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "n.md").write_text("x")
    reqs = checklist.normalize(["It gives a deadline"])
    down = ollama.OllamaError("down")

    def bad_local(*a, **k):
        raise down

    def no_claude(*a, **k):
        raise claude_cli.ClaudeError("limit reached", "limit")
    with budget.background("test"):
        out = checklist.evaluate(reqs, started=started, settings=s, roots=[root], before=snap, local=bad_local,
                                 asker=no_claude, pressure=0.2)
        assert not out["proven"] and out["undecided"] is None and "could not be judged" in out["results"][0]["why"]
        out = checklist.evaluate(reqs, started=started, settings=s, roots=[root], before=snap, local=bad_local,
                                 asker=no_claude, pressure=0.2, undecided_as_unmet=False)
    assert out["undecided"] and "could not be judged" in out["undecided"] and not out["proven"]


# ---- side damage -------------------------------------------------------------------------------------------------

def test_changes_between_two_snapshots():
    before = {"a": (1, 1), "b": (1, 1), "c": (1, 1)}
    after = {"a": (1, 1), "b": (2, 1), "d": (1, 1)}
    assert checklist.changes(before, after) == {"added": ["d"], "changed": ["b"], "removed": ["c"]}


def test_a_file_changed_in_the_vault_outside_the_roots_fails_the_step(env, tmp_path):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "n.md").write_text("Deadline: 2026-12-01")
    (tmp_path / "vault" / "Me").mkdir()
    (tmp_path / "vault" / "Me" / "Profile.md").write_text("oops, edited something that is not mine")
    reqs = checklist.normalize([], [{"type": "file_exists", "path": str(root / "n.md")}])
    out = evaluate(env, reqs, Judge(), started, snap)
    assert not out["proven"] and len(out["side_damage"]) == 1
    last = out["checks"][-1]
    assert last["type"] == "side_damage" and not last["ok"]
    assert "Profile.md (added)" in last["why"] and "outside the write roots" in last["why"]
    assert all(r["ok"] for r in out["results"])   # every requirement was met: the damage alone fails the step


def test_a_file_changed_beside_the_write_root_is_side_damage(env):
    s, g, root = env
    (root.parent / "keep.txt").write_text("v1")
    started, snap, _ = run_eval(env, None)
    (root.parent / "keep.txt").write_text("v2 (changed by the step)")
    (root / "n.md").write_text("ok")
    out = evaluate(env, checklist.normalize([], [{"type": "file_exists", "path": str(root / "n.md")}]), Judge(),
                   started, snap)
    assert not out["proven"] and "keep.txt (changed)" in out["side_damage"][0]


def test_a_removed_file_outside_the_roots_is_side_damage(env, tmp_path):
    s, g, root = env
    (tmp_path / "vault" / "Note.md").write_text("mine")
    started, snap, _ = run_eval(env, None)
    (tmp_path / "vault" / "Note.md").unlink()
    (root / "n.md").write_text("ok")
    out = evaluate(env, checklist.normalize([], [{"type": "file_exists", "path": str(root / "n.md")}]), Judge(),
                   started, snap)
    assert out["side_damage"] and out["side_damage"][0].endswith("Note.md (removed)")


def test_work_inside_the_roots_and_wazzles_own_folders_are_not_side_damage(env, tmp_path):
    s, g, root = env
    started, snap, _ = run_eval(env, None)
    (root / "sub").mkdir()
    (root / "sub" / "deep.md").write_text("inside the roots")
    (root / "n.md").write_text("ok")
    (tmp_path / "vault" / "Journal").mkdir()
    (tmp_path / "vault" / "Journal" / "2026-11-21.md").write_text("Waggle's own digest")
    (tmp_path / "vault" / "Sources").mkdir()
    (tmp_path / "vault" / "Sources" / "x.md").write_text("Waggle's own intake")
    (tmp_path / "vault" / ".git").mkdir()
    (tmp_path / "vault" / ".git" / "HEAD").write_text("ref")
    out = evaluate(env, checklist.normalize([], [{"type": "file_exists", "path": str(root / "n.md")}]), Judge(),
                   started, snap)
    assert out["proven"] and out["side_damage"] == []


def test_without_a_before_snapshot_there_is_no_side_damage_check(env, tmp_path):
    s, g, root = env
    started = time.time()
    (root / "n.md").write_text("ok")
    (tmp_path / "vault" / "stray.md").write_text("x")
    out = evaluate(env, checklist.normalize([], [{"type": "file_exists", "path": str(root / "n.md")}]), Judge(),
                   started, None)
    assert out["proven"] and all(c["type"] != "side_damage" for c in out["checks"])


# ---- autowork: the whole loop ------------------------------------------------------------------------------------

def proposal(root, **over):
    return {"action": "write_note", "title": "Write the note", "instructions": "write n.md", "workdir": str(root),
            "evidence": [{"type": "file_exists", "path": str(root / "n.md")}],
            "requirements": ["The note gives a deadline", "The note names the project"], **over}


def picker(prop):
    calls = []

    def ask(prompt, **k):
        calls.append(prompt)
        return {"json": prop, "text": ""}
    ask.calls = calls
    return ask


def agent_that(effect):
    runs = []

    def run(name, prompt, **k):
        runs.append(prompt)
        effect()
        return agent.AgentResult(text=CLAIM, ok=True)
    run.runs = runs
    return run


def test_a_step_is_proven_when_every_requirement_is_met(env):
    s, g, root = env
    judge = Judge(quote="Deadline")
    run = agent_that(lambda: (root / "n.md").write_text("Project: BIOBUZZ\nDeadline: 2026-12-01\n"))
    out = autowork.run_once(s, asker=picker(proposal(root)), run_fn=run, judge_seams={"local": judge, "pressure": 0.2,
                            "asker": lambda *a, **k: pytest.fail("no Claude call was needed")})
    assert out["outcome"] == "proven", out
    row = autowork.steps("g")[-1]
    assert [c["id"] for c in row["evidence"]] == ["r1", "r2", "r3", "side"] and all(c["ok"] for c in row["evidence"])
    assert "SENTINEL-CLAIM" not in "".join(judge.prompts)       # the agent's own report never reached the judge
    assert "The files you leave behind must meet" in run.runs[0]  # but the agent was told what the checker wants
    assert "Cascade:" in cascade.evening_line()


def test_an_unmet_requirement_leaves_the_step_unproven_and_the_note_says_why(env):
    s, g, root = env
    judge = Judge({"r2": False}, quote="Project")
    run = agent_that(lambda: (root / "n.md").write_text("Project: BIOBUZZ\n"))
    out = autowork.run_once(s, asker=picker(proposal(root)), run_fn=run,
                            judge_seams={"local": judge, "pressure": 0.2, "asker": lambda *a, **k: pytest.fail("x")})
    assert out["outcome"] == "unproven" and "The note gives a deadline" in out["why"]
    row = autowork.steps("g")[-1]
    assert [(c["id"], c["ok"]) for c in row["evidence"]] == [("r1", True), ("r2", False), ("r3", True), ("side", True)]
    line = autowork.report(s)["lines"][0]
    assert "0 proven, 1 unproven" in line and "missing: Write the note: The note gives a deadline" in line
    assert objectives.status(out["objective"])["status"] == "unproven"


def test_a_step_that_touches_a_file_outside_its_roots_is_unproven_even_when_every_requirement_is_met(env, tmp_path):
    s, g, root = env

    def effect():
        (root / "n.md").write_text("Project: BIOBUZZ\nDeadline: 2026-12-01\n")
        (tmp_path / "vault" / "Me.md").write_text("changed something the goal does not own")
    out = autowork.run_once(s, asker=picker(proposal(root)), run_fn=agent_that(effect),
                            judge_seams={"local": Judge(), "pressure": 0.2, "asker": lambda *a, **k: pytest.fail("x")})
    assert out["outcome"] == "unproven" and "changed outside the write roots" in out["why"]
    assert autowork.steps("g")[-1]["evidence"][-1]["type"] == "side_damage"


def test_with_the_judge_off_the_old_proof_by_code_alone_decides(env, tmp_path):
    s, g, root = env
    s.checklist_judge = False
    s.save()
    s = Settings.load()

    def effect():
        (root / "n.md").write_text("only this")
        (tmp_path / "vault" / "Me.md").write_text("not checked any more")
    judge = Judge({"r2": False})
    out = autowork.run_once(s, asker=picker(proposal(root)), run_fn=agent_that(effect),
                            judge_seams={"local": judge})
    assert out["outcome"] == "proven" and judge.prompts == []
    assert [c["type"] for c in autowork.steps("g")[-1]["evidence"]] == ["file_exists"]


# ---- objectives: the plan carries the checklist ------------------------------------------------------------------

STEP = {"title": "Write the summary", "detail": "Write Research/summary.md", "agent": "general",
        "done_when": "the note exists", "depends_on": [],
        "requirements": [{"text": "The summary names three internships", "path": "Research/summary.md"},
                         {"text": "The summary gives each deadline"},
                         {"text": "The final answer says where the note is", "content": True}]}


def test_the_plan_stores_two_to_five_requirements_per_step_and_shows_them_to_the_agent(env):
    s, g, root = env
    oid = objectives.create("Find three internships with deadlines", asker=lambda *a, **k: {
        "json": {"title": "Internships", "steps": [STEP]}}, settings=s, origin="shadow")
    con = objectives._db()
    row = con.execute("SELECT * FROM steps WHERE objective_id=?", (oid,)).fetchone()
    reqs = objectives.step_requirements(row)
    assert [r["id"] for r in reqs] == ["r1", "r2", "r3"] and reqs[0]["path"] == "Research/summary.md"
    assert reqs[2]["content"] is True
    prompt = objectives._step_prompt(con, con.execute("SELECT * FROM objectives WHERE id=?", (oid,)).fetchone(), row)
    assert "never against your own report" in prompt and "2. The summary gives each deadline" in prompt


def test_a_planned_step_is_judged_on_its_files_and_its_final_output_never_its_report(env, monkeypatch):
    s, g, root = env
    from waggle import vault as vault_
    oid = objectives.create_planned("Find three internships", [STEP], origin="shadow", title="Internships")
    judge = Judge()
    monkeypatch.setattr(cascade, "local_available", lambda st: (True, ""))
    monkeypatch.setattr(ollama, "chat_json", lambda messages, schema=None, **k: judge(messages))
    monkeypatch.setattr(claude_cli, "ask", lambda *a, **k: pytest.fail("no Claude call was needed"))
    monkeypatch.setattr(objectives.agents, "verify", lambda *a, **k: pytest.fail("old check"))

    def run(name, prompt, **k):
        vault_.write("Research/summary.md", "Deadline: A 2026-12-01\nDeadline: B 2026-12-05\n", s)
        return agent.AgentResult(text=CLAIM + " FINAL ANSWER: the note is at Research/summary.md", ok=True)
    assert objectives.run_step(oid, settings=s, run_fn=run) == "done"
    seen = judge.prompts[0]
    assert "Deadline: A 2026-12-01" in seen                       # the file the step named
    assert "FINAL ANSWER: the note is at" in seen                 # the deliverable, for the [content] requirement
    assert "DELIVERABLE" in seen
    con = objectives._db()
    proof = json.loads(con.execute("SELECT proof FROM steps WHERE objective_id=?", (oid,)).fetchone()[0])
    assert [(c["id"], c["by"], c["ok"]) for c in proof] == [("r1", "code", True), ("r2", "judge", True),
                                                          ("r3", "judge", True)]


def test_an_unmet_requirement_sends_the_step_back_with_the_failing_requirement(env, monkeypatch):
    s, g, root = env
    from waggle import vault as vault_
    oid = objectives.create_planned("Find three internships", [STEP], origin="shadow", title="Internships")
    monkeypatch.setattr(cascade, "local_available", lambda st: (True, ""))
    judge = Judge({"r2": False})
    monkeypatch.setattr(ollama, "chat_json", lambda messages, schema=None, **k: judge(messages))
    prompts = []

    def run(name, prompt, **k):
        prompts.append(prompt)
        vault_.write("Research/summary.md", "Deadline: A\n", s)
        return agent.AgentResult(text=CLAIM, ok=True)
    assert objectives.run_step(oid, settings=s, run_fn=run) == "retry"
    assert objectives.run_step(oid, settings=s, run_fn=run) == "blocked"
    assert "[r2] The summary gives each deadline" in prompts[1]   # the retry names the requirement that was missed


def test_a_step_waits_when_the_judge_cannot_run(env, monkeypatch):
    s, g, root = env
    from waggle import vault as vault_
    oid = objectives.create_planned("Find three internships", [STEP], origin="shadow", title="Internships")

    def down(*a, **k):
        raise ollama.OllamaError("down")
    monkeypatch.setattr(cascade, "local_available", lambda st: (True, ""))
    monkeypatch.setattr(ollama, "chat_json", down)
    monkeypatch.setattr(claude_cli, "ask", lambda *a, **k: (_ for _ in ()).throw(
        claude_cli.ClaudeError("limit reached", "limit")))

    def run(name, prompt, **k):
        vault_.write("Research/summary.md", "x", s)
        return agent.AgentResult(text="done", ok=True)
    assert objectives.run_step(oid, settings=s, run_fn=run) == "waiting_budget"
    assert objectives.status(oid)["steps"][0]["status"] == "verifying"   # the work is kept, only the check waits


def test_a_step_planned_without_requirements_keeps_the_old_check(env, monkeypatch):
    s, g, root = env
    oid = objectives.create_planned("Do a thing", [{"title": "t", "detail": "d", "agent": "general",
                                                    "done_when": "done"}], origin="shadow", title="x")
    seen = []
    monkeypatch.setattr(objectives.agents, "verify", lambda task, report, settings=None, strict=False: (
        seen.append(report) or (True, "")))
    run = lambda *a, **k: agent.AgentResult(text="did it", ok=True)  # noqa: E731
    assert objectives.run_step(oid, settings=s, run_fn=run) == "done" and seen == ["did it"]


def test_with_the_judge_off_a_planned_step_takes_the_old_check_on_the_report(env, monkeypatch):
    s, g, root = env
    s.checklist_judge = False
    s.save()
    oid = objectives.create_planned("Find three internships", [STEP], origin="shadow", title="Internships")
    seen = []
    monkeypatch.setattr(objectives.agents, "verify", lambda task, report, settings=None, strict=False: (
        seen.append(report) or (True, "")))
    monkeypatch.setattr(ollama, "chat_json", lambda *a, **k: pytest.fail("the judge must not run"))
    from waggle import vault as vault_

    def run(name, prompt, **k):
        vault_.write("Research/summary.md", "x", Settings.load())
        return agent.AgentResult(text="did it", ok=True)
    assert objectives.run_step(oid, settings=Settings.load(), run_fn=run) == "done" and len(seen) == 1
