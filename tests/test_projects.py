from waggle import events, jobs, projects, vault


def make(settings):
    vault.init(settings)
    projects.create("BIOBUZZ Robot", settings, aliases=["biobuzz", "intake"],
                    goal="Reliable scoring robot", current_focus="Transfer",
                    open_problems=["Jams at transfer"], next_actions=["Research transfers", "Check envelope"],
                    decisions=["Reliability first"])


def test_create_parse_and_find(settings):
    make(settings)
    assert projects.all_projects(settings)[0]["aliases"] == ["biobuzz", "intake"]
    sec = projects.sections("BIOBUZZ Robot", settings)
    assert sec["Goal"] == "Reliable scoring robot" and "Jams" in sec["Open problems"]
    assert projects.find("where were we with the Intake?", settings) == ["BIOBUZZ Robot"]
    assert projects.find("intakes are cool", settings) == []  # whole words only
    ctx = projects.context_for("biobuzz status", settings)
    assert "Active projects: BIOBUZZ Robot" in ctx and "## Decisions" in ctx


def test_tasks_and_log(settings):
    make(settings)
    projects.add_task("BIOBUZZ Robot", "Order belts", settings)
    assert [t["text"] for t in projects.tasks("BIOBUZZ Robot", settings)] == [
        "Research transfers", "Check envelope", "Order belts"]
    assert projects.complete_task("BIOBUZZ Robot", "research transfer", settings) == "Research transfers"
    assert projects.tasks("BIOBUZZ Robot", settings)[0]["done"]
    assert projects.complete_task("BIOBUZZ Robot", "zzz qqq", settings) is None
    projects.add_log("BIOBUZZ Robot", "Picked column transfer", settings)
    projects.add_log("BIOBUZZ Robot", "Second entry", settings)
    log = projects.sections("BIOBUZZ Robot", settings)["Log"]
    assert "Picked column transfer" in log and "Second entry" in log
    # other sections untouched by the edits
    assert projects.sections("BIOBUZZ Robot", settings)["Decisions"] == "- Reliability first"
    kinds = [e["kind"] for e in events.read()]
    assert "task" in kinds and "project_update" in kinds


def test_job_links_to_project(settings):
    make(settings)
    jid = jobs.create("Transfer prototype", "Build it", "build a transfer", settings, project="BIOBUZZ Robot")
    assert jobs.load()[jid]["project"] == "BIOBUZZ Robot"
    assert "Transfer prototype" in projects.sections("BIOBUZZ Robot", settings)["Log"]
