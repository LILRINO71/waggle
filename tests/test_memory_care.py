"""Memory upkeep, project audit, past-conversation recall."""
import time

from waggle import memory_care, projects, traces, vault


def test_project_audit(settings):
    projects.create("Robot", goal="Win state", current_focus="Shooter", next_actions=["Test shooter"])
    projects.create("Empty One")
    audit = {a["name"]: a for a in projects.audit(settings)}
    assert audit["Robot"]["missing"] == [] and audit["Robot"]["open_tasks"] == 1
    assert set(audit["Empty One"]["missing"]) == {"Goal", "Current focus", "Next actions"}
    assert audit["Robot"]["days_since_update"] == 0 and not audit["Robot"]["stale"]


def test_duplicates_and_dedupe(settings):
    vault.write("Me/Likes.md", "# Likes\n- racing sims (2026-11-01)\n- Racing sims\n- Minecraft\n")
    fake = lambda texts: [[1.0, 0.0] if "racing" in t else [0.0, 1.0] for t in texts]
    pairs = memory_care.duplicates(settings, embedder=fake)
    assert len(pairs) == 1 and pairs[0][0] == "Me/Likes.md"
    assert memory_care.dedupe(settings, embedder=fake) == 1
    text = vault.read("Me/Likes.md")
    assert text.lower().count("racing sims") == 1 and "Minecraft" in text


def test_status(settings):
    projects.create("P", goal="g")
    st = memory_care.status(settings)
    assert st["notes"] >= 1 and "Me/Learned.md" in st["facts"]
    assert st["projects_missing_state"][0]["name"] == "P"


def test_recall_context_only_when_referring_to_the_past(monkeypatch):
    tid = traces.record("what gear ratio for the shooter?", reply="Go 1:1 with the 6000 rpm motor.")
    import sqlite3
    from waggle.config import data_path
    con = sqlite3.connect(data_path("traces.db"))
    con.execute("UPDATE traces SET time=? WHERE id=?", (time.time() - 3600, tid))
    con.commit()
    con.close()
    assert traces.context_for("what gear ratio should the intake use?") == ""
    block = traces.context_for("what did you say last time about the shooter gear ratio?")
    assert "<past_conversations>" in block and "1:1" in block


def test_recent_elsewhere_carries_context_across_interfaces():
    traces.record("open my FTC notes", session="voice-1", platform="cli", reply="Opened them.")
    traces.record("background step", session="objective-x", reply="internal")
    block = traces.recent_elsewhere("telegram-9")
    assert "open my FTC notes" in block and "cli" in block and "internal" not in block
    assert traces.recent_elsewhere("voice-1") == ""  # nothing from *other* sessions
