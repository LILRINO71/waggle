"""Mem0-style fact updates (waggle/factdecide.py, learn.file_fact): ADD / UPDATE / INVALIDATE / SKIP.

Rules decide first; the local model only settles what the rules cannot, through a JSON schema; without Ollama an
unclear case is ADD. Fakes and a temp home only: no Claude, no Ollama. Invented content only."""
import time

import pytest

from waggle import events, factdecide, kstore, learn, ollama, traces, vault

DAY = 86400


@pytest.fixture
def s(settings, tmp_path):
    settings.watch_roots = [str(tmp_path / "work")]
    settings.save()
    (tmp_path / "work").mkdir()
    vault.init(settings)
    return settings


def turn(request, days_ago=0):
    tid = traces.record(request, session="tg", platform="telegram", origin="user", reply="Noted.")
    con = traces._db()
    con.execute("UPDATE traces SET time=? WHERE id=?", (time.time() - days_ago * DAY, tid))
    con.commit()
    con.close()
    return traces.get(tid)


def row(i, kind, text, **kw):
    return {"id": i, "kind": kind, "text": text, "key": kw.pop("key", ""), "date": kw.pop("date", None), **kw}


def check(kind, text, **kw):
    f, why = learn.check({"kind": kind, "text": text, **kw})
    assert f, why
    return f


def no_model(*a, **k):
    raise AssertionError("the local model must not be asked for a case the rules settle")


# ---- the four decisions, by rules ---------------------------------------------------------------------------------

def test_add_when_nothing_known_is_about_the_same_thing():
    known = [row(1, "like", "Likes mountain biking"), row(2, "goal", "Wants to study computer science")]
    d = factdecide.decide(check("like", "Likes baking sourdough bread"), known, chat=no_model)
    assert d["action"] == factdecide.ADD and d["target"] is None


def test_add_with_no_known_facts_at_all():
    d = factdecide.decide(check("like", "Likes baking sourdough bread"), [], chat=no_model)
    assert d["action"] == factdecide.ADD and d["via"] == "none" and d["neighbours"] == []


@pytest.mark.parametrize("new", ["Likes mountain biking", "likes mountain biking.", "LIKES MOUNTAIN BIKING!!"])
def test_skip_a_fact_that_repeats_a_known_one(new):
    known = [row(1, "like", "Likes mountain biking"), row(2, "like", "Likes tea")]
    d = factdecide.decide(check("like", new), known, chat=no_model)
    assert d["action"] == factdecide.SKIP and d["target"]["id"] == 1 and d["via"] == "rules"


def test_skip_by_embedding_when_the_words_differ_but_the_meaning_does_not():
    known = [row(1, "fact", "Owns a red bicycle")]
    vecs = {"Owns a red bicycle": [1.0, 0.0], "Has a crimson bike": [0.99, 0.05]}
    d = factdecide.decide(check("fact", "Has a crimson bike"), known, embedder=lambda ts: [vecs[t] for t in ts],
                          chat=no_model)
    assert d["action"] == factdecide.SKIP and d["target"]["id"] == 1


def test_update_when_a_number_changed():
    known = [row(1, "fact", "Has 2 cats at home"), row(2, "like", "Likes tea")]
    d = factdecide.decide(check("fact", "Has 3 cats at home"), known, chat=no_model)
    assert d["action"] == factdecide.UPDATE and d["target"]["id"] == 1 and "number" in d["reason"]


def test_update_on_an_explicit_change_of_the_same_subject():
    known = [row(1, "preference", "Prefers Python for scripts")]
    d = factdecide.decide(check("preference", "Now prefers Python for scripts over Bash"), known, chat=no_model)
    assert d["action"] == factdecide.UPDATE and d["target"]["id"] == 1


def test_update_moves_a_dated_event():
    known = [row(1, "event", "Robotics meet", date="2026-12-05")]
    new = {"kind": "event", "text": "Robotics meet moved", "date": "2026-12-12"}
    f, why = learn.check(new, ref=__import__("datetime").date(2026, 11, 20))
    assert f, why
    d = factdecide.decide(f, known, chat=no_model)
    assert d["action"] == factdecide.UPDATE and d["target"]["id"] == 1


def test_invalidate_when_a_fact_is_no_longer_true():
    known = [row(1, "fact", "Plays tennis on weekends"), row(2, "like", "Likes tea")]
    d = factdecide.decide(check("fact", "No longer plays tennis"), known, chat=no_model)
    assert d["action"] == factdecide.INVALIDATE and d["target"]["id"] == 1


def test_a_negative_statement_without_a_matching_known_fact_is_just_new():
    known = [row(1, "like", "Likes tea")]
    d = factdecide.decide(check("fact", "No longer plays tennis"), known, chat=no_model)
    assert d["action"] == factdecide.ADD


def test_events_and_lasting_facts_are_never_compared_with_each_other():
    known = [row(1, "event", "Robotics meet", date="2026-12-05")]
    d = factdecide.decide(check("fact", "Robotics meet is the team's favourite thing"), known, chat=no_model)
    assert d["action"] == factdecide.ADD


def test_the_ten_closest_known_facts_are_the_ones_compared():
    known = [row(i, "fact", f"Owns {n} widgets of type {i}") for i, n in enumerate(["a", "b"] * 15, 1)]
    known.append(row(99, "fact", "Collects antique brass telescopes"))
    near = factdecide.neighbours(check("fact", "Collects brass telescopes from antique shops"), known)
    assert len(near) <= factdecide.NEIGHBOURS and near[0]["fact"]["id"] == 99


# ---- the local model, only for unclear cases ----------------------------------------------------------------------

UNCLEAR_NEW = "Plays cello in the school orchestra"
UNCLEAR_KNOWN = [row(7, "fact", "Plays the cello in a youth orchestra")]


def test_an_unclear_case_asks_the_local_model_with_a_json_schema():
    asked = []

    def model(messages, **kw):
        asked.append((messages, kw))
        return {"action": "UPDATE", "target": 7, "reason": "the cello is now taught at school"}

    d = factdecide.decide(check("fact", UNCLEAR_NEW), UNCLEAR_KNOWN, chat=model)
    assert d["action"] == factdecide.UPDATE and d["target"]["id"] == 7 and d["via"] == "model"
    (messages, kw), = asked
    assert kw["schema"] == factdecide.SCHEMA and "7: Plays the cello" in messages[1]["content"]
    assert factdecide.SCHEMA["properties"]["action"]["enum"] == ["ADD", "UPDATE", "INVALIDATE", "SKIP"]


def test_the_model_can_decide_add_without_a_target():
    d = factdecide.decide(check("fact", UNCLEAR_NEW), UNCLEAR_KNOWN,
                          chat=lambda m, **k: {"action": "ADD", "target": None, "reason": "different things"})
    assert d["action"] == factdecide.ADD and d["via"] == "model"


@pytest.mark.parametrize("answer", [
    {"action": "UPDATE", "target": 12345, "reason": "x"},  # a fact that was never offered
    {"action": "UPDATE", "target": None, "reason": "x"},
    {"action": "DELETE", "target": 7, "reason": "x"},  # not an allowed action
    {"action": "SKIP", "target": "7", "reason": "x"},
    ["not", "an", "object"], None, {}])
def test_a_model_answer_that_does_not_validate_is_ignored_and_the_fact_is_added(answer):
    d = factdecide.decide(check("fact", UNCLEAR_NEW), UNCLEAR_KNOWN, chat=lambda m, **k: answer)
    assert d["action"] == factdecide.ADD and d["via"] == "fallback" and d["target"] is None


def test_without_ollama_an_unclear_case_is_added(s):
    # conftest makes every Ollama request fail: nothing is mocked here
    d = factdecide.decide(check("fact", UNCLEAR_NEW), UNCLEAR_KNOWN, settings=s)
    assert d["action"] == factdecide.ADD and d["via"] == "fallback"


def test_the_schema_reaches_ollama_as_the_format_field(s, monkeypatch):
    sent = []
    monkeypatch.setattr(ollama, "_post", lambda path, payload, timeout=300, settings=None: sent.append(
        (path, payload)) or {"message": {"content": '{"action": "ADD", "target": null, "reason": "new"}'}})
    d = factdecide.decide(check("fact", UNCLEAR_NEW), UNCLEAR_KNOWN, settings=s)
    (path, payload), = sent
    assert path == "/api/chat" and payload["format"] == factdecide.SCHEMA and payload["options"] == {"temperature": 0}
    assert d["via"] == "model"


# ---- learn.file_fact acts on the decision --------------------------------------------------------------------------

def decided():
    return [e for e in events.read("learn") if e.get("action") == "decided"]


def test_add_files_the_fact_and_leaves_the_others(s):
    ok1, a = learn.file_fact({"kind": "like", "text": "Likes mountain biking"}, s, source="t")
    ok2, b = learn.file_fact({"kind": "goal", "text": "Wants to study computer science"}, s, source="t")
    assert ok1 and ok2 and {f["id"] for f in learn.facts("active")} == {a, b}


def test_skip_files_nothing_and_says_duplicate(s):
    ok, first = learn.file_fact({"kind": "fact", "text": "Owns a vintage red bicycle with a basket"}, s, source="t")
    ok2, why = learn.file_fact({"kind": "fact", "text": "Owns a red bicycle"}, s, source="t")  # contained in the first
    assert ok and not ok2 and "duplicate" in why
    assert [f["id"] for f in learn.facts("active")] == [first]
    assert decided()[-1]["reason"].startswith("SKIP via rules")


def test_a_like_is_never_a_duplicate_of_a_dislike_of_the_same_words(s):
    ok, d = learn.file_fact({"kind": "dislike", "text": "Dislikes spicy cheese pizza"}, s, source="t")
    ok2, l = learn.file_fact({"kind": "like", "text": "Likes spicy cheese pizza"}, s, source="t")
    assert ok and ok2 and [f["kind"] for f in learn.facts("active")] == ["like"]  # the like closed the dislike
    assert learn.facts("replaced")[0]["id"] == d


def test_update_files_the_new_fact_and_closes_the_old_one(s):
    old_t, new_t = turn("I have 2 cats at home", 6), turn("I have 3 cats at home now", 1)
    ok, first = learn.file_fact({"kind": "fact", "text": "Has 2 cats at home"}, s, source="digest d6", turn=old_t)
    ok2, second = learn.file_fact({"kind": "fact", "text": "Has 3 cats at home"}, s, source="digest d1", turn=new_t)
    assert ok and ok2
    (active,) = learn.facts("active")
    assert active["id"] == second and "3 cats" in active["text"]
    (gone,) = learn.facts("replaced")
    assert gone["id"] == first and gone["replaced_by"] == second and gone["invalid_at"]
    text = vault.read("Me/Learned.md", s)
    assert "3 cats" in text and "2 cats" not in text  # the old line left the note too


def test_invalidate_closes_the_old_fact_and_files_nothing(s):
    old_t, new_t = turn("I play tennis on weekends", 6), turn("I don't play tennis anymore", 1)
    ok, first = learn.file_fact({"kind": "fact", "text": "Plays tennis on weekends"}, s, source="digest d6", turn=old_t)
    ok2, why = learn.file_fact({"kind": "fact", "text": "No longer plays tennis"}, s, source="digest d1", turn=new_t)
    assert ok and not ok2 and f"#{first}" in why
    assert learn.facts("active") == []
    (gone,) = learn.facts("invalidated")
    assert gone["id"] == first and gone["invalid_at"] and gone["replaced_by"] is None
    assert "tennis" not in vault.read("Me/Learned.md", s)
    assert len(learn.facts(None)) == 1  # the negative statement was not stored as a fact of its own


def test_an_older_statement_that_arrives_late_never_updates_a_newer_one(s):
    new_t, old_t = turn("I have 3 cats at home", 1), turn("I have 2 cats at home", 6)
    ok, newer = learn.file_fact({"kind": "fact", "text": "Has 3 cats at home"}, s, source="digest d1", turn=new_t)
    ok2, why = learn.file_fact({"kind": "fact", "text": "Has 2 cats at home"}, s, source="digest d6", turn=old_t)
    assert ok and not ok2 and "older than fact" in why and f"#{newer}" in why
    assert [f["id"] for f in learn.facts("active")] == [newer] and learn.facts("replaced") == []


def test_an_older_no_longer_statement_cannot_invalidate_a_newer_fact(s):
    new_t, old_t = turn("I play tennis on weekends", 1), turn("I don't play tennis anymore", 6)
    ok, newer = learn.file_fact({"kind": "fact", "text": "Plays tennis on weekends"}, s, source="digest d1", turn=new_t)
    ok2, why = learn.file_fact({"kind": "fact", "text": "No longer plays tennis"}, s, source="digest d6", turn=old_t)
    assert ok and not ok2 and "older than fact" in why
    assert [f["id"] for f in learn.facts("active")] == [newer] and learn.facts("invalidated") == []


def test_an_explicit_supersedes_skips_the_decision_step(s, monkeypatch):
    ok, first = learn.file_fact({"kind": "fact", "text": "Has 2 cats at home"}, s, source="t")
    monkeypatch.setattr(factdecide, "decide", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    ok2, second = learn.file_fact({"kind": "fact", "text": "Has 3 cats at home"}, s, source="t", supersedes=[first])
    assert ok2 and learn.facts("replaced")[0]["id"] == first


def test_a_single_value_about_fact_keeps_its_own_replacement(s):
    learn.file_fact({"kind": "about", "key": "grade", "text": "Sophomore"}, s, source="t")
    ok, new = learn.file_fact({"kind": "about", "key": "grade", "text": "Junior (11th grade)"}, s, source="t")
    assert ok and [f["id"] for f in learn.facts("active")] == [new]
    assert len(learn.facts("replaced")) == 1  # closed once, not twice


def test_every_decision_is_logged_with_its_reason(s):
    learn.file_fact({"kind": "fact", "text": "Has 2 cats at home"}, s, source="t")
    learn.file_fact({"kind": "fact", "text": "Has 3 cats at home"}, s, source="t")
    rows = decided()
    assert [r["reason"].split(" via ")[0] for r in rows] == ["ADD", "UPDATE"]
    assert "rules" in rows[1]["reason"] and rows[1]["target"] == 1


def test_setting_off_is_the_old_behaviour(s):
    s.memory_fact_decide = False
    ok, first = learn.file_fact({"kind": "fact", "text": "Plays tennis on weekends"}, s, source="t")
    ok2, second = learn.file_fact({"kind": "fact", "text": "No longer plays tennis"}, s, source="t")
    assert ok and ok2  # the old code filed it as one more fact
    assert {f["id"] for f in learn.facts("active")} == {first, second} and learn.facts("invalidated") == []
    assert decided() == []


def test_a_failing_decision_step_never_blocks_filing(s, monkeypatch):
    monkeypatch.setattr(factdecide, "decide", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ok, fid = learn.file_fact({"kind": "like", "text": "Likes baking bread"}, s, source="t")
    assert ok and learn.facts("active")[0]["id"] == fid


def test_the_local_model_settles_an_unclear_fact_in_file_fact(s, monkeypatch):
    ok, first = learn.file_fact({"kind": "fact", "text": "Plays the cello in a youth orchestra"}, s, source="t")
    monkeypatch.setattr(ollama, "_post", lambda path, payload, timeout=300, settings=None: {"message": {"content": (
        '{"action": "SKIP", "target": %d, "reason": "same thing"}' % first)}})
    ok2, why = learn.file_fact({"kind": "fact", "text": "Plays cello in the school orchestra"}, s, source="t")
    assert ok and not ok2 and "duplicate" in why
    assert kstore.db().execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
