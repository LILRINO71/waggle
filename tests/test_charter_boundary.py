"""The charter interface the permission rules will consult: finding the charter of background work and judging
one tool call against it. Nothing here touches policy.py."""
import datetime as dt

from waggle import autowork, goals, objectives

FUTURE = (dt.date.today() + dt.timedelta(days=30)).isoformat()


def make_goal(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    goals.add("g1", "Tidy the sandbox", charter={"actions": ["research", "edit_code", "run_tests"],
                                                 "write_roots": [str(root)], "channels": [], "money": 0,
                                                 "expires": FUTURE})
    goals.enable("g1")
    return root


def test_no_charter_for_work_that_belongs_to_no_goal(settings):
    assert goals.charter_for(session="chat-123") is None
    oid = objectives.create_planned("the owner's own objective", [{"title": "x", "agent": "general"}],
                                    origin="user")
    assert goals.charter_for(session=f"objective-{oid}") is None
    assert goals.call_allowed(None, "Write", {"file_path": "C:/anywhere/x.py"})[0]


def test_a_running_work_loop_step_is_bound_by_its_goals_charter(settings, tmp_path):
    root = make_goal(tmp_path)
    oid = objectives.create_planned("[Tidy the sandbox] fix lint", [{"title": "fix", "agent": "coder"}],
                                    origin="autowork")
    info = goals.charter_for(session=f"objective-{oid}")
    assert info["goal"] == "g1" and not info["expired"]
    assert goals.call_allowed(info, "Write", {"file_path": str(root / "a.py")}, settings)[0]
    assert not goals.call_allowed(info, "Write", {"file_path": str(tmp_path / "other" / "a.py")}, settings)[0]
    assert goals.call_allowed(info, "Bash", {"command": f"cd {root} && python -m pytest -q"}, settings)[0]
    ok, why = goals.call_allowed(info, "Bash", {"command": "git add -A && git commit -m x && git push"}, settings)
    assert not ok and ("commit_local" in why or "github_push" in why)
    assert not goals.call_allowed(info, "mcp__telegram__send_message", {"text": "hi"}, settings)[0]
    assert not goals.call_allowed(info, "mcp__shop__checkout_cart", {}, settings)[0]
    assert goals.call_allowed(info, "WebSearch", {"query": "x"}, settings)[0]
    # after the step is recorded, the step table maps the objective to its goal
    autowork.record_step("g1", "proven", objective_id=oid, settings=settings)
    assert goals.charter_for(objective_id=oid)["goal"] == "g1"
    goals.disable("g1")
    info = goals.charter_for(session=f"objective-{oid}")
    assert info["expired"] and not goals.call_allowed(info, "WebSearch", {"query": "x"})[0]


def test_autonomous_work_without_a_known_goal_gets_the_narrowest_charter(settings):
    oid = objectives.create_planned("[Some deleted goal] step", [{"title": "x", "agent": "general"}],
                                    origin="autowork")
    info = goals.charter_for(session=f"objective-{oid}")
    assert info["expired"] and not goals.call_allowed(info, "Read", {"file_path": "x"})[0]


def test_classify_call(settings, tmp_path):
    vault_note = tmp_path / "vault" / "Goals" / "g" / "n.md"
    assert goals.classify_call("Write", {"file_path": str(vault_note)}, settings)["action"] == "write_note"
    assert goals.classify_call("Edit", {"file_path": str(tmp_path / "x.py")}, settings)["action"] == "edit_code"
    assert goals.classify_call("Bash", {"command": "git status"}, settings)["action"] == "research"
    assert goals.classify_call("Bash", {"command": "python -m pytest -q"}, settings)["action"] == "run_tests"
    assert goals.classify_call("Bash", {"command": "gh pr create --fill"}, settings)["channel"] == "github_pr"
    assert goals.classify_call("Bash", {"command": "curl -X POST https://x"}, settings)["channel"] == "web_post"
    assert goals.classify_call("mcp__pay__purchase_item", {}, settings)["money"] == 1
