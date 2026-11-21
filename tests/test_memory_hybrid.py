"""Hybrid memory search: an FTS5 (BM25) index beside the embeddings, rank fusion, a date boost, and the setting that
restores the old embeddings-only search. Fake embedders and a temp home only: no Ollama, no real vault."""
import datetime as dt
import hashlib
import sqlite3

import pytest

from waggle import memory, ollama, vault, when
from waggle.config import data_path

NOW = dt.datetime(2026, 11, 20, 10, 0)


def word_embed(texts):
    """A 512-dim bag of words (no hash collisions to speak of)."""
    out = []
    for t in texts:
        v = [0.0] * 512
        for w in t.lower().replace("search_query: ", "").replace("search_document: ", "").split():
            w = w.strip(".,!?#[]*:()'\"-")
            if len(w) > 2:
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 512] += 1.0
        out.append(v)
    return out


@pytest.fixture
def s(settings):
    vault.init(settings)
    return settings


def write(s, notes):
    for rel, text in notes.items():
        vault.write(rel, text, s)


def fts_rows():
    con = sqlite3.connect(data_path("memory.db"))
    rows = con.execute("SELECT path, idx FROM chunks_fts ORDER BY path, idx").fetchall()
    con.close()
    return rows


def chunk_rows():
    con = sqlite3.connect(data_path("memory.db"))
    rows = con.execute("SELECT path, idx FROM chunks WHERE idx >= 0 ORDER BY path, idx").fetchall()
    con.close()
    return rows


NOTES = {
    "Projects/Hive Scale.md": "# Hive Scale\n\nA load cell under the hive weighs the honey flow every hour.\n",
    "Projects/Laser.md": "# Laser\n\nThe club laser is a ZAPPER-9 that cuts plywood sheets.\n",
    "Me/Likes.md": "# Likes\n\nLikes mountain biking and baking sourdough bread on weekends.\n",
}


# ---- the full-text index follows the chunks -------------------------------------------------------------------------

def test_fts_rows_follow_every_index_write(s):
    write(s, NOTES)
    memory.reindex(s, embedder=word_embed)
    assert fts_rows() == chunk_rows() and len(fts_rows()) >= 3
    # an edited note: its old rows go, its new ones come
    vault.write("Projects/Laser.md", "# Laser\n\nNow a different cutter, the Trotec Speedy, does the plywood.\n", s)
    memory.reindex(s, embedder=word_embed)
    assert fts_rows() == chunk_rows()
    con = sqlite3.connect(data_path("memory.db"))
    assert con.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'trotec'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'zapper'").fetchone()[0] == 0
    con.close()


def test_fts_rows_go_when_a_note_is_deleted_or_becomes_excluded(s):
    write(s, NOTES)
    memory.reindex(s, embedder=word_embed)
    vault.safe_path("Me/Likes", s).unlink()
    memory.reindex(s, embedder=word_embed)
    assert fts_rows() == chunk_rows() and all(p != "Me/Likes.md" for p, _ in fts_rows())
    vault.write("Projects/Laser.md", "---\nwaggle_index: false\n---\n# Laser\n\nThe club laser is a ZAPPER-9.\n", s)
    memory.reindex(s, embedder=word_embed)
    assert fts_rows() == chunk_rows() and all(p != "Projects/Laser.md" for p, _ in fts_rows())


def test_a_raw_delete_from_chunks_keeps_the_index_in_step(s):
    """purge.py deletes chunk rows by rowid: the triggers must follow it, not the code that wrote the rows."""
    write(s, NOTES)
    memory.reindex(s, embedder=word_embed)
    con = memory._db()
    rowid = con.execute("SELECT rowid FROM chunks WHERE path='Projects/Laser.md' AND idx>=0").fetchone()[0]
    con.execute("DELETE FROM chunks WHERE rowid=?", (rowid,))
    con.commit()
    con.close()
    assert fts_rows() == chunk_rows() and all(p != "Projects/Laser.md" for p, _ in fts_rows())


def test_a_database_made_before_the_full_text_index_is_filled_on_first_open(s):
    write(s, NOTES)
    memory.reindex(s, embedder=word_embed)
    con = sqlite3.connect(data_path("memory.db"))
    for stmt in ("DROP TRIGGER chunks_fts_ai", "DROP TRIGGER chunks_fts_ad", "DROP TABLE chunks_fts"):
        con.execute(stmt)
    con.commit()
    con.close()
    memory._db().close()  # the next open creates it again, and fills it from the chunks
    assert fts_rows() == chunk_rows() and len(chunk_rows()) >= 3


def test_a_drifted_index_is_rebuilt(s):
    write(s, NOTES)
    memory.reindex(s, embedder=word_embed)
    con = sqlite3.connect(data_path("memory.db"))
    con.execute("DELETE FROM chunks_fts")  # e.g. a restored copy of only one of the two tables
    con.commit()
    con.close()
    memory._db().close()
    assert fts_rows() == chunk_rows()


def test_fts_search_finds_a_rare_word_and_scores_are_positive(s):
    write(s, NOTES)
    memory.reindex(s, embedder=word_embed)
    hits, df, total = memory.fts_search("what is the ZAPPER-9 for?")
    assert hits[0][0] == "Projects/Laser.md" and hits[0][2] > 0 and df["zapper"] == 1 and total >= 3
    assert memory.fts_search("what is the of and to") [0] == []  # only stop words: nothing to search for
    assert memory.fts_search("sourdough bakery")[0][0][0] == "Me/Likes.md"  # a stem match ("baking" is not "bakery")


def test_a_query_full_of_quotes_and_operators_never_breaks_the_search(s):
    write(s, NOTES)
    memory.reindex(s, embedder=word_embed)
    for q in ['"honey" OR (flow', "NEAR(a b) -x *", "hive:scale AND NOT cell", "''; DROP TABLE chunks; --"]:
        memory.fts_search(q)
    assert chunk_rows()


# ---- fusion ------------------------------------------------------------------------------------------------------

def put_chunks(rows):
    """rows: (path, idx, text, vec) straight into the index, as reindex writes them."""
    con = memory._db()
    con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?)",
                    [(p, 1.0, i, t, memory._pack(v)) for p, i, t, v in rows])
    con.commit()
    con.close()


def test_fusion_order_follows_reciprocal_rank(s):
    # the query's embedding is [1, 0]: dense order A, B, C. Only C contains the query's rare word.
    put_chunks([("Notes/A.md", 0, "alpha text about something general", [1.0, 0.0]),
                ("Notes/B.md", 0, "bravo text about something general", [0.9, 0.4]),
                ("Notes/C.md", 0, "charlie text about the quokka only", [0.5, 0.9])])
    scored, embedded = memory._rank("quokka", s, embedder=lambda t: [[1.0, 0.0]], now=NOW)
    assert embedded and [r["path"] for r in scored] == ["Notes/C.md", "Notes/A.md", "Notes/B.md"]
    c, a, b = scored
    k = memory.RRF_K
    assert c["rrf"] == pytest.approx(1 / (k + 3) + memory.LEX_WEIGHT / (k + 1), abs=1e-5)  # dense rank 3, lexical 1
    assert a["rrf"] == pytest.approx(1 / (k + 1), abs=1e-5) and b["rrf"] == pytest.approx(1 / (k + 2), abs=1e-5)
    assert c["bm25"] > 0 and a["bm25"] == 0 and c["lex_rank"] == 0 and a["lex_rank"] is None
    assert a["score"] > c["score"]  # `score` stays the cosine: what the relevance gate reads


def test_a_chunk_in_both_rankings_beats_one_that_is_first_in_only_one(s):
    put_chunks([("Notes/A.md", 0, "alpha only dense", [1.0, 0.0]),
                ("Notes/B.md", 0, "bravo kiwi and dense too", [0.95, 0.1]),
                ("Notes/C.md", 0, "kiwi kiwi kiwi lexical", [0.0, 1.0])])
    scored, _ = memory._rank("kiwi", s, embedder=lambda t: [[1.0, 0.0]], now=NOW)
    assert scored[0]["path"] == "Notes/B.md"  # second by cosine, second by BM25: the best of both


def test_vectors_from_another_embedding_model_are_never_compared(s):
    put_chunks([("Notes/A.md", 0, "alpha about quokkas", [1.0, 0.0, 0.0]),  # 3 dims: made by another model
                ("Notes/B.md", 0, "bravo about quokkas too", [1.0, 0.0])])
    scored, _ = memory._rank("quokkas", s, embedder=lambda t: [[1.0, 0.0]], now=NOW)
    assert [r["path"] for r in scored] == ["Notes/B.md"]


# ---- the date boost ----------------------------------------------------------------------------------------------

DATED = {
    "Journal/2026-11-19.md": "# 2026-11-19\n\nTalked with Waggle about the robot intake design and repos.\n",
    "Journal/2026-11-02.md": "# 2026-11-02\n\nTalked with Waggle about the robot intake design and repos.\n",
    "Projects/Robot.md": "# Robot\n\nNotes about the robot intake design and repos, with no date at all.\n",
}


def test_a_question_about_yesterday_puts_yesterdays_note_first(s):
    write(s, DATED)
    memory.reindex(s, embedder=word_embed)
    top = memory._rank("what did we talk about yesterday, the robot intake design", s, word_embed, now=NOW)[0]
    assert top[0]["path"] == "Journal/2026-11-19.md" and top[0]["boost"] > 0
    other = {r["path"]: r for r in top}
    assert other["Journal/2026-11-02.md"]["boost"] == 0 and other["Journal/2026-11-02.md"]["rrf"] < top[0]["rrf"]


def test_a_question_without_a_date_gets_no_boost(s):
    write(s, DATED)
    memory.reindex(s, embedder=word_embed)
    scored, _ = memory._rank("the robot intake design and repos", s, word_embed, now=NOW)
    assert all(r["boost"] == 0 for r in scored)


def test_an_explicit_day_and_last_week_pick_their_notes(s):
    write(s, DATED)
    memory.reindex(s, embedder=word_embed)
    top = memory._rank("robot intake design on 2026-11-02", s, word_embed, now=NOW)[0]
    assert top[0]["path"] == "Journal/2026-11-02.md"
    top = memory._rank("the robot intake design from last week", s, word_embed, now=NOW)[0]
    assert all(r["boost"] == 0 for r in top)  # last week = Nov 9-15: nothing is dated there, nothing is boosted


@pytest.mark.parametrize("text,span", [
    ("what did I say yesterday", (dt.date(2026, 11, 19), dt.date(2026, 11, 19))),
    ("the day before yesterday", (dt.date(2026, 11, 18), dt.date(2026, 11, 18))),
    ("3 days ago I decided", (dt.date(2026, 11, 17), dt.date(2026, 11, 17))),
    ("last week's plan", (dt.date(2026, 11, 9), dt.date(2026, 11, 15))),
    ("this week", (dt.date(2026, 11, 16), dt.date(2026, 11, 20))),
    ("last month", (dt.date(2026, 10, 1), dt.date(2026, 10, 31))),
    ("last Friday", (dt.date(2026, 11, 13), dt.date(2026, 11, 13))),
    ("on Nov 12", (dt.date(2026, 11, 12), dt.date(2026, 11, 12))),
    ("2026-11-01 notes", (dt.date(2026, 11, 1), dt.date(2026, 11, 1))),
    ("in October", (dt.date(2026, 10, 1), dt.date(2026, 10, 31))),
    ("recently", (dt.date(2026, 11, 13), dt.date(2026, 11, 20))),
    ("tomorrow", (dt.date(2026, 11, 21), dt.date(2026, 11, 21)))])
def test_when_reads_the_period_a_question_is_about(text, span):
    assert when.range_for(text, NOW) == span


@pytest.mark.parametrize("text", ["the ZAPPER-9 laser", "may I ask something", "a 312 RPM motor", "", None])
def test_when_finds_no_period_in_a_question_without_one(text):
    assert when.range_for(text, NOW) is None


# ---- the setting ------------------------------------------------------------------------------------------------

def test_memory_hybrid_off_is_the_old_embeddings_only_search(s):
    write(s, {**NOTES, **DATED})
    memory.reindex(s, embedder=word_embed)
    s.memory_hybrid = False
    for q in ["what weighs the honey flow", "the ZAPPER-9", "what did we talk about yesterday"]:
        scored, embedded = memory._rank(q, s, word_embed, now=NOW)
        assert embedded and all("rrf" not in r and "bm25" not in r for r in scored)
        assert [r["score"] for r in scored] == sorted((r["score"] for r in scored), reverse=True)
    old = memory.gate(*memory._rank("honey flow load cell", s, word_embed, now=NOW))[0]
    assert old and old[0]["path"] == "Projects/Hive Scale.md"
    s.memory_hybrid = True
    assert "rrf" in memory._rank("honey flow", s, word_embed, now=NOW)[0][0]


def test_search_returns_the_hybrid_order_and_the_cosine_score(s):
    put_chunks([("Notes/A.md", 0, "alpha text about something general", [1.0, 0.0]),
                ("Notes/C.md", 0, "charlie text about the quokka only", [0.5, 0.9])])
    hits = memory.search("quokka", k=2, settings=s, embedder=lambda t: [[1.0, 0.0]], log=False, now=NOW)
    assert [h["path"] for h in hits] == ["Notes/C.md", "Notes/A.md"]
    assert hits[1]["score"] > hits[0]["score"]
    s.memory_hybrid = False
    hits = memory.search("quokka", k=2, settings=s, embedder=lambda t: [[1.0, 0.0]], log=False, now=NOW)
    assert [h["path"] for h in hits] == ["Notes/A.md", "Notes/C.md"]


# ---- the gate keeps working on the fused list ---------------------------------------------------------------------

def many_notes(n=40):
    rows = []
    for i in range(n):
        v = [0.0] * 8
        v[i % 8] = 1.0
        rows.append((f"Notes/N{i}.md", 0, f"filler note number {i} about assorted things", v))
    return rows


def test_a_rare_word_in_the_question_injects_its_chunk_even_when_the_embedding_is_flat(s):
    put_chunks(many_notes() + [("Notes/Laser.md", 0, "the club laser is a zapper nine cutter", [1.0] + [0.0] * 7)])
    flat = lambda t: [[1.0] * 8]  # no chunk stands out by embedding (every one-hot vector is equally close)
    hits, info = memory.gate(*memory._rank("how does the zapper work", s, flat, now=NOW))
    assert [h["path"] for h in hits] == ["Notes/Laser.md"] and hits[0]["lex_strong"]
    s.memory_hybrid = False
    assert memory.gate(*memory._rank("how does the zapper work", s, flat, now=NOW))[0] == []


def test_an_embedding_only_stand_out_needs_the_full_text_to_agree(s):
    rows = many_notes()
    rows.append(("Notes/Noise.md", 0, "filler note about assorted things nobody asks about", [1.0] * 8))
    put_chunks(rows)
    query_vec = lambda t: [[1.0] * 8]  # lines up with the noise chunk only
    s.memory_hybrid = False
    off = memory.gate(*memory._rank("tell me about the weather on mars", s, query_vec, now=NOW))[0]
    assert [h["path"] for h in off] == ["Notes/Noise.md"]  # today: an off-topic question injects a chunk
    s.memory_hybrid = True
    on = memory.gate(*memory._rank("tell me about the weather on mars", s, query_vec, now=NOW))[0]
    assert on == []  # hybrid: no word of the question is in it, so nothing stands out


def test_a_dated_question_injects_the_days_note(s):
    write(s, DATED)
    memory.reindex(s, embedder=word_embed)
    hits, _ = memory.gate(*memory._rank("what happened yesterday", s, word_embed, now=NOW))
    assert "Journal/2026-11-19.md" in [h["path"] for h in hits]


def test_context_block_uses_the_hybrid_search(s):
    write(s, NOTES)
    memory.reindex(s, embedder=word_embed)
    block = memory.context_block("what is the ZAPPER-9", s, word_embed, previous=False)
    assert "Projects/Laser.md" in block


# ---- no embeddings: the full-text search still answers ------------------------------------------------------------

def test_with_ollama_down_the_full_text_index_finds_the_note(s):
    write(s, NOTES)
    memory.reindex(s, embedder=word_embed)

    def down(texts):
        raise ConnectionError("Ollama is not running")
    scored, embedded = memory._rank("what is the ZAPPER-9", s, embedder=down, now=NOW)
    assert not embedded and scored[0]["path"] == "Projects/Laser.md"
    assert memory.search("what is the ZAPPER-9", settings=s, embedder=down, log=False)[0]["path"] == "Projects/Laser.md"
    block = memory.retrieval_block("what is the ZAPPER-9", s, down, previous=False)
    assert "Projects/Laser.md" in block


# ---- the embedding model -----------------------------------------------------------------------------------------

def test_a_changed_embedding_model_re_embeds_every_note_and_the_same_one_does_not(s):
    write(s, NOTES)
    calls = []

    def embedder(texts):
        calls.append(len(texts))
        return word_embed(texts)
    n = memory.reindex(s, embedder=embedder)
    assert n >= 3
    first = len(calls)
    assert memory.reindex(s, embedder=embedder) == 0 and len(calls) == first  # nothing changed: nothing embedded
    s.embed_model = "embeddinggemma:300m"
    assert memory.reindex(s, embedder=embedder) == n and len(calls) > first  # another model: all again
    again = len(calls)
    assert memory.reindex(s, embedder=embedder) == 0 and len(calls) == again
    assert fts_rows() == chunk_rows()


def test_an_index_that_never_recorded_its_model_is_taken_as_the_default_one(s):
    write(s, NOTES)
    n = memory.reindex(s, embedder=word_embed)
    con = sqlite3.connect(data_path("memory.db"))
    con.execute("DELETE FROM meta WHERE k='embed_model'")  # an index made before the model was recorded
    con.commit()
    con.close()
    assert memory.reindex(s, embedder=word_embed) == 0  # default model in use: nothing to redo
    con = sqlite3.connect(data_path("memory.db"))
    con.execute("DELETE FROM meta WHERE k='embed_model'")
    con.commit()
    con.close()
    s.embed_model = "qwen3-embedding:0.6b"
    assert memory.reindex(s, embedder=word_embed) == n


def test_each_embedding_model_gets_its_own_task_prefix(s, monkeypatch):
    sent = []
    monkeypatch.setattr(ollama, "_post", lambda path, payload, timeout=300, settings=None: sent.append(payload) or
                        {"embeddings": [[0.0] for _ in payload["input"]]})
    for model, query_start, doc_start in [("nomic-embed-text", "search_query: ", "search_document: "),
                                          ("nomic-embed-text:latest", "search_query: ", "search_document: "),
                                          ("embeddinggemma:300m", "task: search result | query: ", "title: none | text: "),
                                          ("qwen3-embedding:0.6b", "Instruct:", ""),
                                          ("some-new-model", "search_query: ", "search_document: ")]:
        s.embed_model = model
        ollama.embed(["hello"], "query", s)
        ollama.embed(["hello"], "document", s)
        assert sent[-2]["input"][0].startswith(query_start) and sent[-1]["input"][0].startswith(doc_start), model
        assert sent[-1]["model"] == model


def test_a_json_schema_is_sent_as_the_format_field_and_json_mode_still_works(s, monkeypatch):
    sent = []
    monkeypatch.setattr(ollama, "_post", lambda path, payload, timeout=300, settings=None: sent.append(payload) or
                        {"message": {"content": "{}"}})
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    ollama.chat([{"role": "user", "content": "hi"}], schema=schema, settings=s)
    ollama.chat([{"role": "user", "content": "hi"}], json_mode=True, settings=s)
    ollama.chat([{"role": "user", "content": "hi"}], settings=s)
    assert sent[0]["format"] == schema and sent[0]["options"] == {"temperature": 0}
    assert sent[1]["format"] == "json" and "options" not in sent[1]
    assert "format" not in sent[2]
