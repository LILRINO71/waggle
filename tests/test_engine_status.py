"""StreamWriter: text that has arrived is never held back, and a tool status line shows progress meanwhile."""
import json
import time

from engine_fakes import FakeDelivery

from waggle.delivery import StreamWriter, tool_status


def tool(name, **inp):
    return {"tool": name, "input": json.dumps(inp), "ok": None, "denied": False, "result": ""}


def test_status_lines_come_from_the_tool_call_by_a_fixed_rule():
    assert tool_status([]) == ""
    assert tool_status([tool("Bash", command="ls -la")]) == "Running a command…"
    assert tool_status([tool("Bash", command="ls"), tool("PowerShell", command="dir")]) == "Running 2 commands…"
    assert tool_status([tool("Bash", command="gh pr list --repo a/b")]) == "Checking GitHub…"
    assert tool_status([tool("Bash", command="cd x && gh issue view 3")]) == "Checking GitHub…"
    assert tool_status([tool("Bash", command="echo ghost")]) == "Running a command…"
    assert tool_status([tool("Read", file_path="a")]) == "Reading a file…"
    assert tool_status([tool("Read", file_path="a"), tool("Read", file_path="b")]) == "Reading 2 files…"
    assert tool_status([tool("Read", file_path="a"), tool("Read", file_path="b"), tool("Grep", pattern="x")]) == "Searching files…"
    assert tool_status([tool("WebSearch", query="x")]) == "Searching the web…"
    assert tool_status([tool("WebFetch", url="u")]) == "Reading a web page…"
    assert tool_status([tool("Edit", file_path="a")]) == "Editing a file…"
    assert tool_status([tool("mcp__claude_ai_Gmail__search_threads")]) == "Checking your email…"
    assert tool_status([tool("mcp__waggle__background_task")]) == "Starting a background job…"
    assert tool_status([tool("mcp__waggle__recall")]) == "Checking my notes…"
    assert tool_status([tool("Whatever")]) == "Working on it…"
    broken = {"tool": "Bash", "input": '{"command": "gh pr list --rep', "ok": None}  # input cut at 400 characters
    assert tool_status([broken]) == "Checking GitHub…"


def test_status_is_posted_at_the_first_tool_call_and_the_answer_replaces_it_in_the_same_message():
    d = FakeDelivery()
    w = StreamWriter(d, interval=0.1, tick=0.01)
    t0 = time.monotonic()
    w.tool(tool("Bash", command="ls"))
    time.sleep(0.05)
    assert d.messages == ["Running a command…"] and d.log[0][1] - t0 < 0.1
    time.sleep(0.2)
    w.tool(tool("Read", file_path="a"))  # the work moves on: the same message is edited
    time.sleep(0.2)
    assert d.messages == ["Reading a file…"] and [k for k, _, _ in d.log] == ["post", "edit"]
    answer = "Here is what I found. " * 20
    done = time.monotonic()
    out = w.finish(answer)
    assert out["delivered"] and d.messages == [answer.strip()] and len(d.messages) == 1
    assert d.log[-1][0] == "edit" and d.log[-1][1] - done < 0.05  # the final text is not held back
    assert out["first_visible_at"] is not None and out["first_shown_at"] > out["first_visible_at"]


def test_a_burst_after_a_tool_call_is_one_edit_with_no_added_delay():
    d = FakeDelivery()
    w = StreamWriter(d, interval=5.0, tick=0.01)  # a long interval: the answer must not wait for it
    w.tool(tool("Bash", command="gh pr list"))
    time.sleep(0.05)
    text = "A complete answer. " * 30
    t = time.monotonic()
    for i in range(1, 20):
        w.update(text[:i * len(text) // 19])  # the whole text within a few ms
    out = w.finish(text)
    assert d.messages == [text.strip()] and [k for k, _, _ in d.log] == ["post", "edit"]
    assert d.log[0][2] == "Checking GitHub…" and d.log[-1][1] - t < 0.1 and out["delivered"]


def test_words_written_before_the_tool_call_are_shown_instead_of_the_status():
    d = FakeDelivery()
    w = StreamWriter(d, interval=0.1, tick=0.01)
    w.update("Let me check that for you, one moment while I look at the files.")
    time.sleep(0.6)  # past the hold
    w.tool(tool("Read", file_path="a"))
    time.sleep(0.3)
    assert d.messages == ["Let me check that for you, one moment while I look at the files."]
    assert all("Reading" not in t for _, _, t in d.log)
    w.finish("Let me check that for you, one moment while I look at the files.\n\nIt is fine.")
    assert d.messages[0].endswith("It is fine.")


def test_status_edits_are_at_most_one_per_interval():
    d = FakeDelivery()
    w = StreamWriter(d, interval=0.25, tick=0.01)
    for i in range(6):
        w.tool(tool("Read", file_path=str(i)))
        time.sleep(0.05)
    time.sleep(0.4)
    stamps = [t for _, t, _ in d.log]
    assert all(b - a >= 0.24 for a, b in zip(stamps, stamps[1:])) and d.messages == ["Reading 6 files…"]
    w.finish("done")


def test_no_status_when_the_transport_cannot_edit_and_a_fast_answer_with_no_tool_is_one_post():
    d = FakeDelivery(can_edit=False)
    w = StreamWriter(d, interval=0.05, tick=0.01)
    w.tool(tool("Bash", command="ls"))
    time.sleep(0.1)
    assert d.log == []
    w.finish("answer")
    assert d.messages == ["answer"]
    d2 = FakeDelivery()
    w2 = StreamWriter(d2, interval=0.05, tick=0.01)
    w2.update("Quick one.")
    out = w2.finish("Quick one.")
    assert [k for k, _, _ in d2.log] == ["post"] and out["first_visible_at"] == out["first_shown_at"]
