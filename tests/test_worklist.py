"""work_status: one answer across every work path; a broken source is named, never hides the others."""
import json

from waggle import nightshift, worklist, waggle_mcp


def test_empty_home_says_nothing_is_running():
    assert worklist.answer() == "Nothing is running or queued right now."


def test_night_tasks_show_with_their_state(monkeypatch):
    nightshift.sync()  # seeds the owner's default objectives into this throwaway home
    nightshift._set("O1-pr12", status="running")
    text = worklist.answer()
    assert text.splitlines()[0] == "- night shift: SimBench PR #12 refresh (working on it now)"
    assert "SimBench PR #13 refresh (queued for the night shift)" in text


def test_a_broken_source_is_named(monkeypatch):
    def boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(worklist, "SOURCES", (("night shift", boom),) + worklist.SOURCES[1:])
    assert "(could not read: night shift)" in worklist.answer()


def test_the_chat_gets_the_tool_and_policy_allows_it():
    waggle_mcp._load_providers()
    assert "work_status" in waggle_mcp.TOOLS
    out = json.loads(waggle_mcp.TOOLS["work_status"][1]({}))
    assert out["summary"] and out["unreadable"] == []
    from waggle import policy
    assert policy.WAGGLE_TOOLS["work_status"][0] == policy.ALLOW
