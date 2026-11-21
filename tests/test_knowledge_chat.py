"""The chat engine uses the knowledge: the small core block always, gated retrieval, memory tools reachable.
Invented content only."""
import datetime as dt
import time

import pytest

from waggle import chat_engine, learn, memory, ollama, policy, projects, traces, vault
from test_knowledge import _knowledge_vault, word_embed


@pytest.fixture
def s(settings, tmp_path):
    settings.watch_roots = [str(tmp_path / "work")]
    settings.save()
    (tmp_path / "work").mkdir()
    vault.init(settings)
    return settings


@pytest.fixture
def world(s, monkeypatch):
    _knowledge_vault(s)
    projects.create("Hive Scale", s, overwrite=True, current_focus="Calibrate the load cell", next_actions=["Seal box"])
    vault.write("Me/Profile", "# Profile\n\n## About me\n- **Name:** Robin\n- **School:** Oakridge High\n", s)
    monkeypatch.setattr(ollama, "embed", lambda texts, kind="document", settings=None: word_embed(texts))
    soon = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    learn.file_fact({"kind": "deadline", "text": "Honey report due", "date": soon}, s, source="test",
                    origin="calendar")
    return s


def ctx(text, s, chat="telegram:5"):
    return chat_engine.build_context(chat, text, False, s)


def test_the_core_block_is_in_every_turn_with_deadlines(world):
    block = ctx("hello there", world)
    assert "<waggle_core>" in block and "Oakridge High" in block
    assert "Hive Scale: Calibrate the load cell | next: Seal box" in block
    assert "Honey report due" in block  # a commitment with a date, from the facts table


def test_the_core_survives_a_slow_or_broken_retrieval(world, monkeypatch):
    monkeypatch.setattr(chat_engine, "CONTEXT_BUDGET_S", 0.2)
    monkeypatch.setattr(memory, "retrieval_block", lambda *a, **k: time.sleep(3) or "SLOW NOTE")
    t = time.time()
    block = ctx("hive scale honey flow", world)
    assert time.time() - t < 1.5 and "SLOW NOTE" not in block and "Oakridge High" in block
    monkeypatch.setattr(memory, "retrieval_block", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert "Oakridge High" in ctx("hive scale honey flow", world)


def test_retrieval_is_gated_by_relevance_not_a_fixed_count(world):
    none = ctx("quantum zebra xylophone", world)
    assert "<waggle_memory>" not in none and "<waggle_core>" in none
    one = ctx("hive scale honey flow load cell", world)
    assert "<waggle_memory>" in one and "Projects/Hive Scale.md" in one
    n_one = one.count("--- ")
    assert 1 <= n_one <= memory.MAX_INJECT
    # the polluted notes never come back, however well they match
    assert "Waggle HQ" not in one and "Objectives/" not in one
    assert memory.retrieval_log(mode="inject")[-1]["paths"]  # and the injection was logged with the chat as session


def test_retrieval_is_logged_under_the_chat_that_asked(world):
    ctx("hive scale honey flow load cell", world, chat="telegram:9")
    ctx("quantum zebra xylophone", world, chat="telegram:9")
    log = memory.retrieval_log(session="telegram:9", mode="inject")
    assert len(log) == 2 and log[0]["paths"] and log[1]["paths"] == []


def test_a_follow_up_uses_the_users_last_turn_not_a_test_prompt(world):
    traces.record("what does the hive scale weigh", platform="telegram", origin="user", reply="Honey flow, hourly.")
    traces.record("tell me about the chemistry class", platform="telegram", origin="test", reply="x")
    traces.record("tell me about the garden beds", platform="cli", reply="x")
    used, follow = memory.query_for("and how often?")
    assert follow and "hive scale" in used and "chemistry" not in used and "garden" not in used


def test_the_memory_tools_are_offered_by_wagglesown_tool_server_and_allowed_by_policy(world):
    import waggle.waggle_mcp as mcp
    for name in ("note_read", "related_to", "memory_core", "vault_search"):
        assert name in mcp.TOOLS and policy.WAGGLE_TOOLS[name][0] == policy.ALLOW
    listed = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {"note_read", "related_to", "memory_core"} <= {t["name"] for t in listed}
    out = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "note_read", "arguments": {"name": "Hive Scale"}}})["result"]
    text = out["content"][0]["text"]
    assert "Hive Scale.md" in text and "load cell" in text.lower()
    core = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "memory_core", "arguments": {}}})["result"]["content"][0]["text"]
    assert "Oakridge High" in core
    rel = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "related_to", "arguments": {"name": "Hive Scale"}}})["result"]
    assert "Hive Scale" in rel["content"][0]["text"]
    assert policy.decide_waggle_tool("mcp__waggle__note_read", {}).allowed if hasattr(policy, "decide_waggle_tool") \
        else True


def test_a_tool_search_made_through_the_server_is_logged_under_its_chat(world, monkeypatch):
    import waggle.waggle_mcp as mcp
    monkeypatch.setenv("WAGGLE_SESSION", "telegram:7")
    mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "note_read", "arguments": {"name": "Hive Scale"}}})
    assert memory.retrieval_log(session="telegram:7", mode="read")
