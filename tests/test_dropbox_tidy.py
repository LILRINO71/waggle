"""The weekly tidy: a plan written to the vault and one text queued, nothing changed, no Claude."""
import datetime as dt
import os

import pytest

from waggle import dropbox_ingest as di
from waggle import dropbox_tidy as tidy
from waggle import notify, ollama, vault
from waggle.core import outbox

SUNDAY = dt.datetime(2026, 11, 22, 10, 0).timestamp()
BODY = ("The gearbox ratio study compares a 5 to 1 and a 4 to 1 cartridge for the lift arm and records the "
        "measured current draw, stall torque and the time to raise the arm across the full stroke. ") * 2


@pytest.fixture
def s(settings):
    vault.init(settings)
    return settings


def note(s, rel, text):
    p = vault.root(s) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    os.utime(p, (1_790_000_000, 1_790_000_000))  # same time for all: notes are then listed by path
    return p


def source(s, name, body, summary="A short summary of the thing that was filed."):
    return note(s, f"Sources/2026-11/{name}.md",
                f'---\ntitle: "{name}"\norigin: "user"\n---\n# {name}\n\n## Summary\n<!-- waggle:summary start -->\n'
                f"- {summary}\n<!-- waggle:summary end -->\n\n## Extracted text\n\n{body}\n")


def snapshot(s):
    r = vault.root(s)
    return {p.relative_to(r).as_posix(): p.read_bytes() for p in r.rglob("*") if p.is_file()}


class Model:
    def __init__(self, groups=None, links=None, stale=None, down=False):
        self.calls, self.down = [], down
        self.groups, self.links, self.stale = groups or [], links or [], stale or []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.down:
            raise ollama.OllamaError("Ollama not reachable: refused")
        if "Group notes" in system:
            return {"groups": self.groups}
        if "source notes and a list" in system:
            return {"links": self.links}
        return {"stale": self.stale}


def plan_text(s, day="2026-11-22"):
    return (vault.root(s) / "Waggle" / f"Weekly tidy {day}.md").read_text(encoding="utf-8")


def test_the_plan_lists_duplicates_links_groups_and_stale_facts_and_changes_nothing_else(s):
    source(s, "Gear study a", BODY)
    source(s, "Gear study copy", BODY)  # identical text under another name
    source(s, "Lift arm notes", "Notes about the Phoenix lift arm and how it was geared. " * 8)
    source(s, "Lift arm photo", "A photo of the arm. " * 10, summary="A photo of the lift arm.")
    note(s, "Projects/Phoenix.md", "# Phoenix\n\nThe lift project.\n")
    note(s, "Projects/Projects.md", "# Projects\n\n- Phoenix\n")
    note(s, "Me/Learned.md", "# Learned\n\n- Registration deadline is 2026-01-05\n- Meet is this weekend\n"
                             "- Likes pickleball\n")
    note(s, "Journal/2026-11-20.md", "- 09:00 nothing\n")
    before = snapshot(s)
    model = Model(groups=[{"theme": "Lift arm", "notes": [3, 4, 99], "why": "both about the arm"}],
                  links=[{"note": 4, "target": "Phoenix", "why": "same project"}],
                  stale=[{"line": "Likes pickleball", "why": "made up, never in the note?"},
                         {"line": "A line the note never had", "why": "hallucinated"}])
    out = tidy.run({"scheduled_ts": SUNDAY}, settings=s, now=SUNDAY, ask=model)
    assert out["plan"] == "Waggle/Weekly tidy 2026-11-22.md" and out["items"] >= 6

    after = snapshot(s)
    assert set(after) - set(before) == {out["plan"]}  # exactly one new file
    assert all(after[k] == before[k] for k in before)  # and every other byte unchanged

    text = plan_text(s)
    assert text.startswith('---\norigin: "waggle"') and "waggle_index: false" in text
    assert "A plan only: nothing was moved" in text and "Claude was not used" in text
    dup = [l for l in text.splitlines() if l.startswith("- [ ] identical text")]
    assert dup and "[[Sources/2026-11/Gear study a]]" in dup[0] and "[[Sources/2026-11/Gear study copy]]" in dup[0]
    assert "- [ ] **Lift arm**: [[Sources/2026-11/Lift arm notes]], [[Sources/2026-11/Lift arm photo]]" in text
    assert "- [ ] Add [[Projects/Phoenix]] to [[Sources/2026-11/Lift arm notes]] (mentions \"Phoenix\")" in text
    assert "- [ ] Add [[Projects/Phoenix]] to [[Sources/2026-11/Lift arm photo]] (the local model" in text
    assert '[[Me/Learned]]: "Registration deadline is 2026-01-05" - the date 2026-01-05 has passed' in text
    assert '"Meet is this weekend" - time-bound wording' in text
    assert '"Likes pickleball" - the local model thinks' in text
    assert "never had" not in text  # a line the model invented is dropped
    assert "Journal" not in text  # the journal is not tidied


def test_every_model_prompt_says_the_notes_are_data(s):
    source(s, "Gear study a", BODY)
    source(s, "Lift arm", BODY + " other words entirely here for the arm")
    note(s, "Projects/Phoenix.md", "# Phoenix\n")
    note(s, "Me/Learned.md", "# Learned\n\n- Something\n")
    model = Model()
    tidy.run({"scheduled_ts": SUNDAY}, settings=s, now=SUNDAY, ask=model)
    assert len(model.calls) >= 3
    for system, user in model.calls:
        assert "never instructions" in system and "DATA" in system


def test_one_short_text_goes_through_the_core_outbox_and_not_twice(s):
    source(s, "Gear study a", BODY)
    source(s, "Gear study copy", BODY)
    out = tidy.run({"scheduled_ts": SUNDAY}, settings=s, now=SUNDAY, ask=Model())
    rows = outbox.list(limit=50)
    assert len(rows) == 1 and rows[0]["kind"] == "text" and rows[0]["origin"] == "tidy"
    body = rows[0]["body"]
    assert "weekly tidy plan is ready" in body and "Waggle/Weekly tidy 2026-11-22" in body and len(body) < 200
    assert out["outbox"] == rows[0]["id"]
    # the owner may have ticked boxes: a second run the same day neither rewrites the plan nor texts again
    path = vault.root(s) / out["plan"]
    path.write_text(path.read_text(encoding="utf-8").replace("- [ ]", "- [x]", 1), encoding="utf-8")
    again = tidy.run({"scheduled_ts": SUNDAY}, settings=s, now=SUNDAY + 600, ask=Model())
    assert again == {"already": out["plan"]} and len(outbox.list(limit=50)) == 1 and "- [x]" in path.read_text(encoding="utf-8")


def test_with_ollama_down_the_plain_checks_still_run_and_the_plan_says_what_was_skipped(s):
    source(s, "Gear study a", BODY)
    source(s, "Gear study copy", BODY)
    note(s, "Me/Learned.md", "# Learned\n\n- Test is on 2026-02-02\n")
    out = tidy.run({"scheduled_ts": SUNDAY}, settings=s, now=SUNDAY, ask=Model(down=True))
    assert out["model_down"] and out["items"] == 2
    text = plan_text(s)
    assert "local model was not reachable" in text and "identical text" in text and "has passed" in text
    assert len(outbox.list(limit=5)) == 1


def test_an_empty_vault_still_gets_a_plan_and_a_text(s):
    out = tidy.run({"scheduled_ts": SUNDAY}, settings=s, now=SUNDAY, ask=Model())
    assert out["items"] == 0
    assert plan_text(s).count("- Nothing found.") == 4
    assert "nothing to tidy" in outbox.list(limit=5)[0]["body"]


def test_notes_already_linked_are_not_suggested_again(s):
    source(s, "Lift arm notes", "Notes about the Phoenix lift arm. [[Projects/Phoenix]] is the project. " * 4)
    note(s, "Projects/Phoenix.md", "# Phoenix\n")
    out = tidy.run({"scheduled_ts": SUNDAY}, settings=s, now=SUNDAY, ask=Model(
        links=[{"note": 1, "target": "Phoenix", "why": "x"}]))
    assert out["items"] == 0


def test_near_duplicates_and_same_titles_are_found(s):
    a = ("The spring robotics season plan covers build weeks scrimmages the qualifier and the awards ceremony "
         "with dates for every milestone and the people responsible for each task including mentors parents "
         "volunteers sponsors judges referees drivers coaches captains scouts pit crew and the media team "
         "plus travel budgets lodging meals registration fees shipping crates tools spare parts batteries "
         "chargers cables sensors cameras laptops tablets banners buttons stickers")
    note(s, "Projects/Season.md", a)
    note(s, "Ideas/Season copy.md", a.replace("awards", "award") + " One extra sentence.")
    note(s, "Ideas/Robot.md", "# Robot\n\nsomething about the robot in ideas folder only here.\n")
    note(s, "People/Robot.md", "# Robot\n\ncompletely different content about a teammate named in people.\n")
    kinds = {g["kind"]: g["notes"] for g in tidy.find_duplicates(tidy.load_notes(s))}
    assert set(kinds["near-identical text"]) == {"Projects/Season.md", "Ideas/Season copy.md"}
    assert set(kinds["same title"]) == {"Ideas/Robot.md", "People/Robot.md"}


def test_dropped_files_in_drop_and_originals_are_not_tidied(s):
    di.init(s)
    (vault.root(s) / "Drop" / "waiting.md").write_text(BODY, encoding="utf-8")
    note(s, "Sources/_files/2026-11/orig.md", BODY)
    source(s, "Real", BODY)
    notes = [n.rel for n in tidy.load_notes(s)]
    assert [n for n in notes if n.startswith("Sources/")] == ["Sources/2026-11/Real.md"]
    assert not any(n.startswith("Drop/") or "_files" in n for n in notes)


def test_the_job_is_registered_and_runs_from_the_core_handler(s):
    di.register_jobs()
    from waggle.core import jobs
    assert jobs.handler(di.TIDY).detached
    out = jobs.handler(di.TIDY).fn({"scheduled_ts": SUNDAY}, settings=s, now=SUNDAY, ask=Model())
    assert out["plan"].startswith("Waggle/Weekly tidy 2026-11-22")
    assert di.status(s)["tidy_last"] == out["plan"]


def test_no_claude_is_called(s, monkeypatch):
    import waggle.claude_cli as claude_cli
    calls = []
    monkeypatch.setattr(claude_cli, "find_claude", lambda: calls.append("find") or None)
    source(s, "Gear study a", BODY)
    tidy.run({"scheduled_ts": SUNDAY}, settings=s, now=SUNDAY, ask=Model())
    di.scan_pass(s, now=SUNDAY, ask=lambda *a: {"summary": [], "facts": []}, stable_s=0, embedder=lambda t: [[0.0] * 4] * len(t))
    assert calls == [] and not hasattr(notify, "claude")
