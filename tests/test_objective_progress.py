"""An objective's finished steps reach the evening note while it runs (not only its final report)."""
from waggle import initiative, objectives


def make(con, oid, origin="telegram", steps=3):
    con.execute("INSERT INTO objectives (id, title, goal, status, created, updated, origin) VALUES (?,?,?,?,?,?,?)",
                (oid, "Earn first money", "g", "active", 1.0, 1.0, origin))
    for i in range(1, steps + 1):
        con.execute("INSERT INTO steps (objective_id, idx, title, detail, status) VALUES (?,?,?,?,?)",
                    (oid, i, f"Step {i}", "d", "pending"))
    con.commit()


def test_a_finished_step_is_a_line_for_tonight(settings, monkeypatch):
    con = objectives._db()
    make(con, "o1")
    con.close()
    monkeypatch.setattr(objectives, "_origin", lambda oid: "telegram")
    monkeypatch.setattr("waggle.origin.objective_source", lambda oid, db=None: "telegram")
    objectives.progress_line("o1", 1, "Research options", 2)
    lines = [r["title"] for r in initiative.evening_items(__import__("time").time())]
    assert any("Earn first money: step 1 of 3 done (Research options); 2 to go" in t for t in lines)


def test_test_and_quiet_objectives_stay_out(settings, monkeypatch):
    con = objectives._db()
    make(con, "o2")
    con.close()
    monkeypatch.setattr(objectives, "_origin", lambda oid: "telegram")
    monkeypatch.setattr("waggle.origin.objective_source", lambda oid, db=None: "oneshot")
    objectives.progress_line("o2", 1, "x", 2)
    monkeypatch.setattr(objectives, "_origin", lambda oid: next(iter(objectives.QUIET_ORIGINS)))
    objectives.progress_line("o2", 2, "y", 1)
    assert not [r for r in initiative.evening_items(__import__("time").time()) if r["kind"] == "objective"]
