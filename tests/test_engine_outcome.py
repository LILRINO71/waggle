"""The record of a turn: every tool call with its outcome and result in the trace, and the outcome rule
(ok / recovered / degraded / failed) that reads them, no model involved."""
import json

import pytest
from engine_fakes import FakeClaude, FakeDelivery, ev_result, ev_tool

from waggle import chat_engine, claude_cli, traces
from waggle.chat_session import ChatSession
from waggle.core import store

HOOK_REFUSAL = "NEEDS_APPROVAL [QX7K]: running a command that deletes files. You cannot do this without the user's OK."
BLOCKED = "Blocked by Waggle's safety policy: touching the safety files. Do not retry or work around this; tell the user."


@pytest.fixture(autouse=True)
def _close_store():
    yield
    store.close_all()


def step(tool, ok=True, denied=False, result="", inp=""):
    return {"tool": tool, "ok": ok, "denied": denied, "result": result, "input": inp}


def outcome(steps, **kw):
    kw.setdefault("answered_by", "claude-x")
    kw.setdefault("delivered", True)
    return traces.turn_outcome(steps=steps, **kw)


# ---- the rule ----------------------------------------------------------------------------------------------------

def test_ok_when_no_tool_failed():
    assert outcome([]) == ("ok", [])
    assert outcome([step("Bash"), step("Read")]) == ("ok", [])


def test_recovered_when_a_later_call_of_the_same_kind_of_work_succeeded():
    out, why = outcome([step("Bash", ok=False, result="exit 1"), step("Bash", ok=False, result="exit 2"),
                        step("PowerShell", ok=True, result="done")])
    assert out == "recovered" and len(why) == 2 and all("later PowerShell succeeded" in w for w in why)
    assert outcome([step("WebFetch", ok=False), step("WebSearch")])[0] == "recovered"
    assert outcome([step("mcp__waggle__recall", ok=False), step("mcp__waggle__vault_search")])[0] == "recovered"
    assert outcome([step("Read", ok=False), step("Glob"), step("Read")])[0] == "recovered"


def test_degraded_when_a_tool_failed_and_the_answer_went_out_without_it():
    out, why = outcome([step("Bash", ok=False, result="exit 1")])
    assert out == "degraded" and why == ["tool error: Bash"]
    # a success of some other kind of work does not repair it
    assert outcome([step("Bash", ok=False), step("Read")])[0] == "degraded"
    # the last failure has nothing after it, even though an earlier call of that kind worked
    assert outcome([step("Bash", ok=False), step("Bash"), step("Bash", ok=False)])[0] == "degraded"
    assert outcome([step("Read", ok=None)]) == ("degraded", ["tool without a result: Read"])


def test_a_refusal_by_the_safety_hook_is_degraded_not_ok():
    out, why = outcome([step("Bash", ok=False, denied=True, result=HOOK_REFUSAL)])
    assert out == "degraded" and why == ["needs approval: Bash (the answer went out without it)"]
    out, why = outcome([step("Write", ok=False, denied=True, result=BLOCKED)])
    assert out == "degraded" and why[0].startswith("blocked by the safety policy: Write")
    # another command that happens to work is not the action that needs the user's OK
    assert outcome([step("Bash", ok=False, denied=True, result=HOOK_REFUSAL, inp="rm x"),
                    step("PowerShell", inp="ls")])[0] == "degraded"
    # ...unless the identical call went through afterwards (approved)
    out, why = outcome([step("Bash", ok=False, denied=True, result=HOOK_REFUSAL, inp="rm x"),
                        step("Bash", ok=True, inp="rm x")])
    assert out == "recovered" and why == ["needs approval, then allowed: Bash"]


def test_degraded_lists_everything_and_failed_beats_all():
    out, why = outcome([step("Bash", ok=False), step("Bash"), step("Read", ok=False, denied=True, result=HOOK_REFUSAL)],
                       partial=True)
    assert out == "degraded" and len(why) == 3 and any("later Bash succeeded" in w for w in why)
    assert outcome([step("Bash", ok=False)], failure_notice=True, answered_by="none")[0] == "failed"
    assert outcome([], delivered=False, queued=False)[0] == "failed"
    assert outcome([step("Bash", ok=False), step("Bash")], delivered=False, queued=True)[0] == "degraded"
    assert outcome([step("Bash", ok=False), step("Bash")], answered_by="local")[0] == "degraded"


# ---- what the parser calls a refusal -----------------------------------------------------------------------------

def test_refusal_needs_the_hooks_own_words_in_an_error_or_at_the_start():
    assert claude_cli.is_refusal(HOOK_REFUSAL) and claude_cli.is_refusal(BLOCKED)
    assert claude_cli.is_refusal("something NEEDS_APPROVAL [A1B2]: x", error=True)
    # a successful read of a file that talks about the policy is not a refusal
    assert not claude_cli.is_refusal("The doc says: NEEDS_APPROVAL is returned when ...", error=False)
    assert not claude_cli.is_refusal("File does not exist.", error=True)


# ---- through a whole turn ----------------------------------------------------------------------------------------

def turn(settings, script):
    settings.edit_interval_s = 0.05
    fake = FakeClaude(script)
    sess = ChatSession("telegram:5", settings=settings, popen=fake, claude="claude")
    rec = chat_engine.run_turn("telegram:5", "do it", FakeDelivery(), platform="telegram", context="",
                               settings=settings, session=sess)
    row = traces.get(rec.trace_id)
    row["outcome_why"] = json.loads(row["outcome_why"]) if row["outcome_why"] else []
    row["extra"] = json.loads(row["extra"])
    return rec, row


def test_every_tool_call_is_in_the_trace_with_its_result_redacted_and_cut(settings):
    secret = "ghp_" + "c" * 36

    def script(proc, msg):
        sid = proc.session_id
        proc.emit(ev_tool(sid, "Bash", {"command": "git status"}, "On branch main\n" + "z" * 800, tool_id="t1"))
        proc.emit(ev_tool(sid, "Read", {"file_path": "x.txt"}, f"File does not exist. token {secret}", is_error=True,
                          tool_id="t2", msg_id="m2"))
        proc.emit(ev_tool(sid, "Bash", {"command": "rm x"}, HOOK_REFUSAL, is_error=True, tool_id="t3", msg_id="m3"))
        proc.answer("Done, apart from the delete.")
    rec, row = turn(settings, script)
    steps = row["steps"]
    assert [s["tool"] for s in steps] == ["Bash", "Read", "Bash"]
    assert [(s["ok"], s["denied"]) for s in steps] == [(1, 0), (0, 0), (0, 1)]
    assert len(steps[0]["result"]) == traces.STEP_RESULT_CAP and steps[0]["result"].startswith("On branch main")
    assert secret not in steps[1]["result"] and "File does not exist" in steps[1]["result"]
    assert steps[2]["result"].startswith("NEEDS_APPROVAL")
    tools = row["extra"]["tools"]  # the same calls, compact, in the row itself
    assert [t["tool"] for t in tools] == ["Bash", "Read", "Bash"]
    assert [t["status"] for t in tools] == ["ok", "failed", "refused"]
    assert len(tools[0]["result"]) == 500 and secret not in json.dumps(tools)
    assert rec.outcome == "degraded" and row["ok"] == 0


def test_a_turn_where_the_hook_refused_two_commands_is_degraded(settings):
    def script(proc, msg):
        sid = proc.session_id
        proc.emit(ev_tool(sid, "Bash", {"command": "rm a"}, HOOK_REFUSAL, is_error=True, tool_id="t1"))
        proc.emit(ev_tool(sid, "PowerShell", {"command": "Remove-Item b"}, HOOK_REFUSAL, is_error=True, tool_id="t2",
                          msg_id="m2"))
        proc.answer("I need your OK to delete those. Reply yes.")
    rec, row = turn(settings, script)
    assert row["turn_outcome"] == "degraded" and rec.delivered
    assert row["outcome_why"] == ["needs approval: Bash (the answer went out without it)",
                                  "needs approval: PowerShell (the answer went out without it)"]


def test_a_turn_where_two_commands_failed_and_a_third_route_worked_is_recovered(settings):
    def script(proc, msg):
        sid = proc.session_id
        proc.emit(ev_tool(sid, "Bash", {"command": "gh pr list"}, "gh: command not found", is_error=True, tool_id="t1"))
        proc.emit(ev_tool(sid, "Bash", {"command": "git log"}, "fatal: not a git repository", is_error=True,
                          tool_id="t2", msg_id="m2"))
        proc.emit(ev_tool(sid, "PowerShell", {"command": "Get-ChildItem"}, "a.txt", tool_id="t3", msg_id="m3"))
        proc.answer("Found a.txt.")
    rec, row = turn(settings, script)
    assert row["turn_outcome"] == "recovered" and row["ok"] == 1 and rec.delivered
    assert len(row["outcome_why"]) == 2 and all("later PowerShell succeeded" in w for w in row["outcome_why"])
    assert [s["ok"] for s in row["steps"]] == [0, 0, 1]


def test_a_clean_turn_stays_ok_and_a_failed_turn_is_failed(settings):
    def good(proc, msg):
        proc.emit(ev_tool(proc.session_id, "Read", {"file_path": "a"}, "text"))
        proc.answer("Here it is.")
    rec, row = turn(settings, good)
    assert row["turn_outcome"] == "ok" and row["outcome_why"] == []

    def bad(proc, msg):
        proc.emit(ev_result(proc.session_id, "Claude AI usage limit reached|1763600000", is_error=True))
    rec, row = turn(settings, bad)
    assert row["turn_outcome"] == "failed" and row["ok"] == 0
