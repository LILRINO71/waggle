"""A step's relative file is found where the worker wrote it (2026-11-25: the money objective's research.md was under
~/Claude, the check looked only in the vault, and a finished step ran again)."""
from waggle import objectives, vault


def test_relative_paths_are_found_in_the_agent_work_folder(settings, tmp_path):
    settings.agent_workdir = str(tmp_path / "work")
    made = tmp_path / "work" / "waggle-earn" / "research.md"
    made.parent.mkdir(parents=True)
    made.write_text("# research", encoding="utf-8")
    assert objectives.resolve_named("waggle-earn/research.md", settings) == made
    # nowhere yet: the vault path, where a note-writing step should put it
    assert objectives.resolve_named("Projects/x.md", settings) == vault.own(settings) / "Projects/x.md"


def test_the_vault_wins_when_both_exist(settings, tmp_path):
    settings.agent_workdir = str(tmp_path / "work")
    for root in (vault.own(settings), tmp_path / "work"):
        (root / "Projects").mkdir(parents=True, exist_ok=True)
        (root / "Projects" / "a.md").write_text("x", encoding="utf-8")
    assert objectives.resolve_named("Projects/a.md", settings) == vault.own(settings) / "Projects" / "a.md"


def test_a_requirement_path_in_the_work_folder_is_checked_there(settings, tmp_path, monkeypatch):
    settings.agent_workdir = str(tmp_path / "work")
    made = tmp_path / "work" / "waggle-earn" / "research.md"
    made.parent.mkdir(parents=True)
    made.write_text("# research", encoding="utf-8")
    seen = {}

    def fake_evaluate(reqs, **kw):
        seen["path"] = reqs[0]["path"]
        return {"checks": [], "undecided": [], "proven": True, "missing": []}

    monkeypatch.setattr(objectives.checklist, "evaluate", fake_evaluate)
    monkeypatch.setattr(objectives, "_set_step", lambda *a, **k: None)
    step = {"idx": 1, "detail": "research", "done_when": "file exists", "started": 0, "result": ""}
    reqs = [{"id": "r1", "text": "research saved", "path": "waggle-earn/research.md"}]
    assert objectives._judge_step(None, "o1", step, reqs, settings) == (True, [])
    assert seen["path"] == str(made)


def test_a_folder_requirement_passes_when_something_in_it_was_written(settings, tmp_path):
    import time
    from waggle import checklist
    folder = tmp_path / "product"
    (folder / "plugin").mkdir(parents=True)
    started = time.time() - 60
    req = {"id": "r1", "text": "product files exist", "path": str(folder) + "/"}
    assert not checklist._path_result(req, started, None, settings)["ok"]       # empty folder
    (folder / "plugin" / "Core.lua").write_text("return {}", encoding="utf-8")
    assert checklist._path_result(req, started, None, settings)["ok"]
    assert not checklist._path_result(req, time.time() + 60, None, settings)["ok"]  # nothing new since


def test_a_retry_counts_files_from_the_steps_first_attempt(settings, tmp_path, monkeypatch):
    seen = {}

    def fake_evaluate(reqs, **kw):
        seen["started"] = kw["started"]
        return {"checks": [], "undecided": [], "proven": True, "missing": []}

    monkeypatch.setattr(objectives.checklist, "evaluate", fake_evaluate)
    monkeypatch.setattr(objectives, "_set_step", lambda *a, **k: None)
    step = {"idx": 3, "detail": "build", "done_when": "", "started": 2000.0, "first_started": 1000.0, "result": ""}
    objectives._judge_step(None, "o1", step, [{"id": "r1", "text": "x"}], settings)
    assert seen["started"] == 1000.0
    del step["first_started"]
    objectives._judge_step(None, "o1", step, [{"id": "r1", "text": "x"}], settings)
    assert seen["started"] == 2000.0


def test_claiming_a_step_keeps_its_first_start(settings):
    con = objectives._db()
    con.execute("INSERT INTO steps (objective_id, idx, title, status) VALUES ('o9', 1, 't', 'pending')")
    con.commit()
    for now in (100.0, 200.0):
        con.execute("UPDATE steps SET status='running', started=?, first_started=COALESCE(first_started, ?) "
                    "WHERE objective_id='o9' AND idx=1", (now, now))
    row = con.execute("SELECT started, first_started FROM steps WHERE objective_id='o9'").fetchone()
    assert tuple(row) == (200.0, 100.0)
