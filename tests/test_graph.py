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
    assert out == {"turns": 1, "entities": 1, "decisions_filed": 1, "dropped": 1}  # the 80-char "name"
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


def _extract(settings, msg, out):
    traces.record(msg, session="tg-7")
    return graph.extract_from_traces(settings=settings, chat=lambda m: out)


def test_ideas_are_not_decisions_and_requests_are_neither(settings):
    _seed(settings)
    graph.build_from_vault(settings)
    _extract(settings, "maybe we could try a turret on the robot build shooter later", {"decisions": [
        {"project": "Robot Build", "decision": "Try a turret on the shooter", "status": "considering"}]})
    _extract(settings, "write a short note in Inbox/test.md listing today's date please", {"decisions": [
        {"project": "Robot Build", "decision": "I'll write a short note in Inbox/test.md listing today's date",
         "status": "decided"}]})
    dec = projects.sections("Robot Build", settings)["Decisions"]
    assert "turret" not in dec and "Inbox" not in dec
    nb = graph.neighbors("Robot Build")
    assert [i["summary"] for i in nb["ideas"]] == ["Try a turret on the shooter"]
    assert all("turret" not in d["summary"] for d in nb["decisions"])
    assert "considering (not decided): Try a turret" in graph.context_for("how is the robot build going")


def test_ungrounded_decisions_and_noise_entities_are_dropped(settings):
    _seed(settings)
    graph.build_from_vault(settings)
    out = _extract(settings, "Telegram reminded me about tomorrow, check hello.txt", {
        "entities": [{"name": "tomorrow", "type": "event"}, {"name": "hello.txt", "type": "topic"},
                     {"name": "unread emails", "type": "topic"}, {"name": "Telegram", "type": "tool"},
                     {"name": "Priya", "type": "person"}],  # Priya was never mentioned
        "relations": [{"src": "Telegram", "rel": "uses", "dst": "robot batteries"}],
        "decisions": [{"project": "Robot Build", "decision": "Switch to mecanum wheels", "status": "decided"}]})
    assert out["entities"] == 1 and out["decisions_filed"] == 0
    assert graph.neighbors("tomorrow") is None and graph.neighbors("Priya") is None
    assert graph.neighbors("Telegram")["related"] == []


def test_changed_decision_supersedes_the_old_one(settings):
    _seed(settings)
    graph.build_from_vault(settings)
    _extract(settings, "we're switching the robot build to a flywheel shooter instead of the roller shooter", {
        "decisions": [{"project": "Robot Build", "decision": "Use a flywheel shooter", "status": "decided",
                       "replaces": "use a roller shooter", "quote": "switching to a flywheel shooter"}]})
    dec = projects.sections("Robot Build", settings)["Decisions"]
    assert "~~2026-11-01: Use a roller shooter~~ _(changed" in dec and "flywheel" in dec
    nb = graph.neighbors("Robot Build")
    cur = [d["summary"] for d in nb["decisions"]]
    assert "Use a flywheel shooter" in cur and "Use a roller shooter" not in cur
    assert nb["decisions"][0]["replaces"] == "Use a roller shooter"
    assert nb["decisions"][0]["source_ref"].startswith("chat tg-7") and "flywheel" in nb["decisions"][0]["quote"]
    graph.build_from_vault(settings)  # the struck line stays superseded after a rebuild
    assert "Use a roller shooter" not in [d["summary"] for d in graph.neighbors("Robot Build")["decisions"]]
    assert "changed from: Use a roller shooter" in graph.context_for("robot build plan?")


def test_deleting_a_decision_in_the_note_retracts_it(settings):
    _seed(settings)
    graph.record_decision("Robot Build", "Go with the ramp transfer", settings)
    graph.build_from_vault(settings)
    assert any("ramp" in d["summary"].lower() for d in graph.neighbors("Robot Build")["decisions"])
    projects._set_section("Robot Build", "Decisions", "- 2026-11-01: Use a roller shooter", settings)
    out = graph.build_from_vault(settings)
    assert out["decisions"] == 1
    assert [d["summary"] for d in graph.neighbors("Robot Build")["decisions"]] == ["Use a roller shooter"]


def test_aliases_and_spelling_variants_resolve_to_one_node(settings):
    projects.create("BIOBUZZ Robot", goal="Win", current_focus="intake")
    text = vault.read("Projects/BIOBUZZ Robot")
    vault.write("Projects/BIOBUZZ Robot", text.replace("type: project", "type: project\naliases: [biobuzz, meet 0]"))
    graph.build_from_vault(settings)
    con = graph._db()
    assert graph.canonical(con, "biobuzz") == "biobuzz robot"
    assert graph.canonical(con, "BioBuzz-Robots") == "biobuzz robot"  # punctuation/plural-insensitive
    con.close()
    assert "BIOBUZZ Robot (project)" in graph.context_for("any update on biobuzz?")


def test_corrections_retract_confirm_merge(settings):
    _seed(settings)
    graph.build_from_vault(settings)
    _extract(settings, "thinking about a ramp intake for the robot build", {"decisions": [
        {"project": "Robot Build", "decision": "Add a ramp intake", "status": "considering"}]})
    res = graph.correct("idea: Add a ramp intake", "confirm", settings=settings)
    assert res["filed_in_project"] and "ramp intake" in projects.sections("Robot Build", settings)["Decisions"]
    assert graph.neighbors("Robot Build")["ideas"] == []
    assert graph.correct("decision: Two motors on the drive", "retract", settings=settings) == {
        "retracted": "decision: Two motors on the drive"}
    assert "~~Two motors on the drive~~" in projects.sections("Robot Build", settings)["Decisions"]
    graph._note("Arjun S", "member_of", "FTC 7831")
    assert graph.correct("Arjun S", "merge", "Arjun") == {"merged": "Arjun S", "into": "Arjun"}
    assert any(r["name"] == "FTC 7831" for r in graph.neighbors("Arjun")["related"])
    con = graph._db()
    assert graph.canonical(con, "arjun s") == "arjun"
    con.close()


def test_cleanup_of_old_noise_keeps_real_things(settings):
    _seed(settings)
    graph.build_from_vault(settings)
    con = graph._db()
    for name, typ in (("today", "topic"), ("hello.txt", "topic"), ("calendar tomorrow", "topic"), ("Telegram", "tool")):
        con.execute("INSERT INTO nodes (key, name, type, source, mentions) VALUES (?,?,?,?,1)",
                    (graph.key(name), name, typ, "chat"))
    graph.add_edge(con, "telegram", "uses", "calendar tomorrow", "chat")
    graph.upsert_node(con, "decision: I'll write a short note in Inbox/x.md", "decision",
                      "I'll write a short note in Inbox/x.md", "chat")
    out = graph.cleanup(con)
    con.commit()
    assert out == {"removed": 3, "retracted": 1}
    assert graph.neighbors("Telegram")["related"] == [] and graph.neighbors("today") is None
    assert graph.neighbors("Arjun") is not None  # vault data untouched
