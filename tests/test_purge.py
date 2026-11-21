"""Purge: what non-user turns left in the vault, facts, graph and index goes, with an undo outside the vault.
Invented content only."""
import hashlib
import json
import time

import pytest

from waggle import graph, kstore, learn, memory, ollama, purge, traces, vault
from test_knowledge import word_embed

STATED = "ugh I am kind of stressed, I have a quokka meet Saturday and a zebra test tomorrow"


@pytest.fixture
def s(settings, tmp_path, monkeypatch):
    settings.watch_roots = [str(tmp_path / "work")]
    settings.save()
    (tmp_path / "work").mkdir()
    vault.init(settings)
    monkeypatch.setattr(ollama, "embed", lambda texts, kind="document", settings=None: word_embed(texts))
    return settings


def _snapshot(s, tmp_path):
    snap = {}
    for p in sorted(vault.root(s).rglob("*.md")):
        snap[p.relative_to(vault.root(s)).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    for name in ("knowledge.db", "graph.db", "memory.db"):
        f = tmp_path / "home" / name
        snap[name] = hashlib.sha256(f.read_bytes()).hexdigest() if f.exists() else None
    return snap


@pytest.fixture
def world(s, tmp_path):
    """Three turns: a test through the CLI that states things, an old-style test row, and a real user turn."""
    t_cli = traces.record(STATED, session="dev", platform="cli", reply="Good luck with the quokka meet!")
    t_test = traces.record("my narwhal has a pickle schedule on Sunday for the harbour", session="x",
                           platform="telegram", origin="test", reply="ok")
    t_user = traces.record("I am building a hive scale with a load cell this month", session="tg",
                           platform="telegram", origin="user", reply="Nice.")
    minute = time.strftime("%Y-%m-%d %H:%M", time.localtime(traces.get(t_cli)["time"]))
    vault.write("Journal/2026-11-19", "\n".join([
        "# 2026-11-19", "", "My own diary line stays.",
        "- 12:00 (cli, 2 turns)",
        f"  - asked: {STATED} -> answered",
        "  - asked: what is a quokka -> answered",
        "- 12:30 (telegram, 1 turn)",
        "  - asked: I am building a hive scale with a load cell this month -> answered", ""]), s)
    vault.write("Me/Learned", "# Learned\n\n- Quokka meet Saturday; zebra test tomorrow (2026-11-19)\n"
                "- Building a hive scale with a load cell (2026-11-19)\n- Likes jazz piano a lot\n", s)
    vault.write("Me/History/Removed-from-Profile-20261119", "# Removed\n\n- Zebra test tomorrow and a quokka meet on "
                "Saturday\n- A hand note about harbour tides in the morning\n", s)
    vault.write("Projects/Hive Scale", "# Hive Scale\n\nLoad cell under the hive weighs honey flow every hour.\n", s)
    memory.reindex(s, embedder=word_embed)
    # facts: one with a source turn from the test, one without a source that matches it, one from the user
    learn._insert({"kind": "event", "text": "Quokka meet", "date": "2099-01-01", "expires": "2099-01-02",
                   "source": "digest x", "origin": "test", "source_turn": t_cli})
    learn._insert({"kind": "preference", "text": "Has a zebra test tomorrow and a quokka meet Saturday",
                   "source": "tool"})
    learn._insert({"kind": "goal", "text": "Wants to build a hive scale", "source": "digest y", "origin": "user",
                   "source_turn": t_user})
    # graph: a chat entity from the CLI turn, a decision from the user's turn, one from the vault
    con = graph._db()
    graph.upsert_node(con, "Quokka meet", "event", "meet on Saturday", "chat", source_ref=f"chat dev {minute}")
    graph.upsert_node(con, "Hive Scale", "project", "", "vault")
    k = graph.upsert_node(con, "decision: use a load cell", "decision", "use a load cell", "chat",
                          source_ref=f"chat tg {minute}")
    graph.add_edge(con, graph.key("Hive Scale"), "decided", k, "chat")
    graph.add_edge(con, graph.key("Hive Scale"), "related_to", graph.key("Quokka meet"), "chat")
    con.commit()
    con.close()
    kstore.put("digest.days", {"2026-11-19": {"distilled": {"summary": "She had a quokka meet Saturday and a zebra "
                                                                          "test tomorrow.", "conversations": []},
                                              "distilled_last": 5, "written_last": 5, "turns": 3}})
    return {"cli": t_cli, "test": t_test, "user": t_user}


def test_a_dry_run_reports_per_store_and_changes_nothing(s, world, tmp_path):
    before = _snapshot(s, tmp_path)
    r = purge.purge(s)
    assert r["dry_run"] and "id" not in r
    assert r["vault"]["notes"] == 3 and r["vault"]["lines"] == 5
    assert r["facts"]["count"] == 2 and r["graph"]["nodes"] == 1 and r["graph"]["edges"] == 1
    assert r["digest_state"]["days"] == 1 and r["index"]["stale_chunks"] >= 1
    assert r["non_user_turns"] == 2
    assert _snapshot(s, tmp_path) == before
    assert not (tmp_path / "home" / "purge").exists() or not list((tmp_path / "home" / "purge").iterdir())


def test_apply_removes_the_pollution_everywhere_and_nothing_else(s, world, tmp_path):
    keep_before = _snapshot(s, tmp_path)
    r = purge.purge(s, apply=True, embedder=word_embed)
    assert r["id"].startswith("purge-") and r["remaining"] == {"vault_lines": 0, "facts": 0, "graph_nodes": 0,
                                                              "state_days": 0, "index_chunks": 0}
    j = vault.read("Journal/2026-11-19", s)
    assert "quokka" not in j.lower() and "(cli" not in j and "What is a quokka".lower() not in j.lower()
    assert "My own diary line stays." in j and "hive scale with a load cell" in j and "(telegram, 1 turn)" in j
    learned = vault.read("Me/Learned", s)
    assert "quokka" not in learned.lower() and "hive scale" in learned and "jazz piano" in learned
    hist = vault.read("Me/History/Removed-from-Profile-20261119", s)
    assert "zebra" not in hist.lower() and "harbour tides" in hist  # the hand note in the same file stays
    assert vault.read("Projects/Hive Scale", s).endswith("every hour.\n")
    assert _snapshot(s, tmp_path)["Projects/Hive Scale.md"] == keep_before["Projects/Hive Scale.md"]
    # facts
    status = {f["text"]: f["status"] for f in learn.facts(status=None)}
    assert status["Quokka meet"] == "purged" and status["Wants to build a hive scale"] == "active"
    assert status["Has a zebra test tomorrow and a quokka meet Saturday"] == "purged"
    assert [f["text"] for f in learn.facts("active")] == ["Wants to build a hive scale"]
    # graph
    con = graph._db()
    names = {r["name"] for r in con.execute("SELECT name FROM nodes")}
    edges = {(r["rel"]) for r in con.execute("SELECT rel FROM edges")}
    con.close()
    assert "Quokka meet" not in names and "Hive Scale" in names and "decision: use a load cell" in names
    assert edges == {"decided"}
    # digest state: the model-written summary is gone, to be written again from the user's turns
    st = kstore.get("digest.days")["2026-11-19"]
    assert "distilled" not in st and "written_last" not in st and st["turns"] == 3
    # the undo record is outside the vault, under the home
    assert str(tmp_path / "home" / "purge") in r["manifest"] and not (vault.root(s) / "purge").exists()
    assert json.loads(open(r["manifest"], encoding="utf-8").read())["vault"]


def test_a_removed_sentence_can_no_longer_be_retrieved(s, world):
    q = "quokka meet Saturday zebra test tomorrow"
    pre, _ = memory.relevant(q, s, word_embed)
    assert any("quokka" in h["text"].lower() or "zebra" in h["text"].lower() for h in pre)  # it was reachable
    purge.purge(s, apply=True, embedder=word_embed)
    post, _ = memory.relevant(q, s, word_embed)
    assert not any("quokka" in h["text"].lower() or "zebra" in h["text"].lower() for h in post)
    assert not any("quokka" in h["text"].lower() for h in memory.search(q, settings=s, embedder=word_embed))
    con = memory._db()
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE lower(text) LIKE '%quokka%' OR lower(text) LIKE "
                       "'%zebra%'").fetchone()[0] == 0
    con.close()
    assert "quokka" not in memory.context_block(q, s, word_embed).lower()


def test_a_second_apply_finds_nothing(s, world):
    purge.purge(s, apply=True, embedder=word_embed)
    r = purge.purge(s, apply=True, embedder=word_embed)
    assert r["vault"]["lines"] == 0 and r["facts"]["count"] == 0 and r["graph"]["nodes"] == 0
    assert r["index"]["stale_chunks"] == 0


def test_restore_puts_everything_back_and_leaves_edited_notes_alone(s, world, tmp_path):
    before = _snapshot(s, tmp_path)
    r = purge.purge(s, apply=True, embedder=word_embed)
    vault.write("Me/Learned", vault.read("Me/Learned", s) + "- A line the user added after the purge\n", s)
    out = purge.restore(r["id"], s, embedder=word_embed)
    assert out["conflicts"] == ["Me/Learned.md"] and out["notes"] == 2 and out["facts"] == 2
    assert "added after the purge" in vault.read("Me/Learned", s) and "quokka" not in vault.read("Me/Learned", s).lower()
    after = _snapshot(s, tmp_path)
    assert after["Journal/2026-11-19.md"] == before["Journal/2026-11-19.md"]
    assert after["Me/History/Removed-from-Profile-20261119.md"] == before["Me/History/Removed-from-Profile-20261119.md"]
    assert {f["text"]: f["status"] for f in learn.facts(status=None)}["Quokka meet"] == "active"
    con = graph._db()
    assert con.execute("SELECT COUNT(*) FROM nodes WHERE name='Quokka meet'").fetchone()[0] == 1
    con.close()
    assert "distilled" in kstore.get("digest.days")["2026-11-19"]
    assert purge.restore(r["id"], s)["error"] == "already restored"
    assert [x["restored"] for x in purge.list_purges()] == [True]
    assert purge.restore("purge-nope", s)["error"]


def test_a_phrase_you_name_is_enough_and_a_line_the_user_also_said_stays(s, world):
    vault.write("Me/Likes", "# Likes\n\n- Loves harbour sunsets over the pickle jar\n- Building a hive scale with a "
                "load cell this month\n", s)
    r = purge.purge(s, phrases=["pickle jar"])
    files = {f["path"]: f for f in r["vault"]["files"]}
    assert "Me/Likes.md" in files and files["Me/Likes.md"]["lines"] == 1  # only the phrase line, not the user's own
    assert purge.purge(s)["vault"]["files"] and "Me/Likes.md" not in {f["path"] for f in purge.purge(s)["vault"]["files"]}


# ---- history and quarantine folders are not retrievable ---------------------------------------------------

def test_history_and_archive_notes_are_not_indexed_retrieved_or_read(s):
    vault.write("Me/History/Removed-from-Profile-1", "# Removed\n\nThe user has a gryphon audition next week.\n", s)
    vault.write("Archive/old", "# Old\n\nThe user has a gryphon audition next week.\n", s)
    vault.write("Quarantine/stuff", "# Q\n\nThe user has a gryphon audition next week.\n", s)
    vault.write("Me/Profile", "# Profile\n\nThe user likes beekeeping.\n", s)
    memory.reindex(s, embedder=word_embed)
    con = memory._db()
    paths = {r[0] for r in con.execute("SELECT DISTINCT path FROM chunks")}
    con.close()
    assert paths == {"Me/Profile.md"} or all(not p.startswith(("Me/History", "Archive", "Quarantine")) for p in paths)
    assert not [h for h in memory.search("gryphon audition next week", settings=s, embedder=word_embed)
                if "gryphon" in h["text"]]
    assert "gryphon" not in memory.context_block("gryphon audition next week", s, word_embed)
    for name in ("Me/History/Removed-from-Profile-1", "Removed-from-Profile-1", "Archive/old", "Quarantine/stuff"):
        assert "error" in memory.read_note(name, s), name
    assert "Me/History/Removed-from-Profile-1.md" not in memory.related("Profile", s)["backlinks"]


def test_stale_index_rows_of_an_excluded_note_are_dropped_by_the_purge(s):
    vault.write("Me/History/Removed-from-Profile-2", "# Removed\n\nThe user has a gryphon audition.\n", s)
    con = memory._db()
    con.execute("INSERT INTO chunks VALUES (?,?,?,?,?)", ("Me/History/Removed-from-Profile-2.md", 1.0, 0,
                "[Me/History/Removed-from-Profile-2.md]\nThe user has a gryphon audition.",
                memory._pack(word_embed(["gryphon audition"])[0])))
    con.commit()
    con.close()
    assert purge.purge(s)["index"]["by_reason"] == {"note is excluded from retrieval": 1}
    purge.purge(s, apply=True, embedder=word_embed)
    con = memory._db()
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE path LIKE 'Me/History%'").fetchone()[0] == 0
    con.close()


def test_the_cli_command_dry_runs_by_default(s, world, capsys, tmp_path):
    before = _snapshot(s, tmp_path)
    assert purge.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and out["vault"]["lines"] == 5 and _snapshot(s, tmp_path) == before
    assert purge.main(["list"]) == 0 and json.loads(capsys.readouterr().out) == []


def test_ingest_exposes_the_purge_and_restores_it_by_id(s, world):
    from waggle import ingest
    assert ingest.purge(s)["dry_run"]
    r = ingest.purge(s, apply=True, embedder=word_embed)
    assert ingest.restore(r["id"], s)["notes"] == 3


def test_the_graph_extractor_reads_only_the_users_turns(s):
    traces.record("let's decide the quokka ramp for the robot build today", session="dev", platform="cli")
    seen = []
    graph.extract_from_traces(since=0, settings=s, chat=lambda m: seen.append(m) or {})
    assert seen == []
    traces.record("let's decide the quokka ramp for the robot build today", session="tg", platform="telegram")
    graph.extract_from_traces(since=0, settings=s, chat=lambda m: seen.append(m) or {})
    assert len(seen) == 1
