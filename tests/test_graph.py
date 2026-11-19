"""Knowledge graph: built from the vault and from chats; used as turn context."""
from waggle import graph, projects, traces, vault


def _seed(settings):
    projects.create("Robot Build", goal="Win state", current_focus="Shooter", next_actions=["Test shooter"])
    projects._set_section("Robot Build", "Decisions", "- 2026-11-01: Use a roller shooter\n- Two motors on the drive",
                          settings)
    vault.write("People/Arjun.md", "# Arjun\nTeammate on [[Robot Build]], builds the intake.\n")
    vault.write("Ideas/Ramp transfer.md", "# Ramp transfer\nFor [[Robot Build|the robot]], see [[Arjun]].\n")


def test_build_from_vault_nodes_links_and_decisions(settings):
    _seed(settings)
    counts = graph.build_from_vault(settings)
    assert counts["decisions"] == 2 and counts["links"] >= 3
    nb = graph.neighbors("robot build")
    assert nb["type"] == "project"
    assert nb["decisions"][0]["date"] == "2026-11-01"  # dated decisions first
    related = {r["name"] for r in nb["related"]}
    assert {"Arjun", "Ramp transfer"} <= related
    assert graph.stats()["by_type"]["person"] == 1
    edges = graph.stats()["edges"]
    graph.build_from_vault(settings)  # rebuild is idempotent for edges
    assert graph.stats()["edges"] == edges
    assert len(graph.neighbors("Robot Build")["decisions"]) == 2


def test_context_for_mentions_only(settings):
    _seed(settings)
    graph.build_from_vault(settings)
    assert graph.context_for("what's the weather?") == ""
    block = graph.context_for("what should I do next on the robot build?")
    assert block.startswith("<knowledge_graph>") and "roller shooter" in block and "Arjun" in block


def test_record_decision_files_into_project_and_dedupes(settings):
    _seed(settings)
    assert graph.record_decision("Robot Build", "Go with the ramp transfer for the shooter feed", settings)
    assert not graph.record_decision("Robot Build", "go with the ramp transfer for shooter feed", settings)
    dec = projects.sections("Robot Build", settings)["Decisions"]
    assert dec.count("ramp transfer") == 1 and "_(from chat)_" in dec and "roller shooter" in dec


def test_extract_from_traces_with_fake_model(settings):
    _seed(settings)
    traces.record("let's go with the ramp transfer on the robot build, Arjun will CAD it", session="tg-1")
    traces.record("background objective step that should be skipped entirely", session="objective-9")
    seen = []

    def fake_chat(msgs):
        seen.append(msgs[1]["content"])
        return {"entities": [{"name": "Arjun", "type": "person"}, {"name": "x" * 80, "type": "person"}],
                "relations": [{"src": "Arjun", "rel": "works_on", "dst": "Robot Build"}],
                "decisions": [{"project": "robot build", "decision": "Use the ramp transfer"}]}
    out = graph.extract_from_traces(since=0, settings=settings, chat=fake_chat)
    assert out == {"turns": 1, "entities": 1, "decisions_filed": 1}
    assert "objective step" not in seen[0]
    assert "ramp transfer" in projects.sections("Robot Build", settings)["Decisions"]
    nb = graph.neighbors("Robot Build")
    assert any("ramp transfer" in (d["summary"] or "").lower() for d in nb["decisions"])
    assert graph.extract_from_traces(settings=settings, chat=fake_chat) == {"turns": 0}  # watermark moved on


def test_graph_note_tool(settings):
    _seed(settings)
    assert graph._note("Arjun", "member_of", "FTC 7831") == {"recorded": True}
    assert any(r["name"] == "FTC 7831" for r in graph.neighbors("Arjun")["related"])
    res = graph._note("Robot Build", "decided", "Switch to 6000 rpm motors")
    assert res == {"recorded": True, "filed_in_project": True}


def test_main_is_silent(settings, capsys):
    _seed(settings)
    import waggle.ollama as ollama

    def down(*a, **k):
        raise ConnectionError("ollama down")
    ollama.chat_json, real = down, ollama.chat_json
    try:
        traces.record("a long enough message about the robot build", session="cli-1")
        graph.main()
    finally:
        ollama.chat_json = real
    assert capsys.readouterr().out == ""  # cron stdout would go to the phone
