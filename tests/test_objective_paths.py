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
