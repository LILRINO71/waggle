"""The learning loop (waggle/learnloop.py): corrections and quiz misses become small fixes that are re-checked, kept
or reverted, reported with an undo code, and undone on the owner's word. Fakes and a temp home only: no Claude, no
Ollama, no real Telegram. Invented content only."""
import hashlib
import json
import re
import time

import pytest

from waggle import brief, chat_engine, kstore, learn, learnloop, memory, traces, vault
from waggle.core import jobs, plugins, timers

STOP = {"the", "and", "for", "does", "when", "what", "that", "with", "are", "was", "has", "have", "your", "you"}


def bag_embed(texts):
    """A fake embedder that tells unrelated texts apart (the shared one lets everything look alike): a hashed bag
    of words, so texts with no word in common score about zero and a few shared words stand out."""
    out = []
    for t in texts:
        v = [0.0] * 1024
        for w in re.findall(r"[a-z0-9]+", t.lower().replace("search_query:", "").replace("search_document:", "")):
            if len(w) >= 3 and w not in STOP:
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 1024] += 1.0
        out.append(v)
    return out


@pytest.fixture
def s(settings, tmp_path):
    settings.watch_roots = [str(tmp_path / "work")]
    settings.save()
    (tmp_path / "work").mkdir()
    vault.init(settings)
    return settings


# ---- helpers: a chat with a wrong answer, then the owner's correction ----------------------------------------

CHAT = "telegram:111"
ASKED = "when does my robotics team meet?"
WRONG = "Your robotics team meets on Monday and Wednesday."
CORRECTION = "No, my robotics team meets on Tuesday and Thursday"


def turn(request, reply="", *, chat=CHAT, origin="user", platform="telegram", route="sonnet", steps=None, **kw):
    return traces.record(request, session=chat, chat=chat, platform=platform, origin=origin, reply=reply, route=route,
                         steps=steps, **kw)


def fill_vault(s, n=40):
    """Enough unrelated notes that retrieval's relevance gate behaves as it does on a real vault."""
    for i in range(n):
        vault.write(f"Research/Filler{i}", f"# Filler {i}\n\ntopic{i} alpha{i} beta{i} gamma{i} delta{i} lorem{i} "
                    f"ipsum{i} dolor{i} sit{i} amet{i} notes about subject number {i}.\n", s)


def seed_old_fact(s, text="robotics team meets on Monday and Wednesday"):
    """A durable fact from an earlier, real conversation (its source turn is the owner's)."""
    fill_vault(s)
    tid = turn("my robotics team meets on Monday and Wednesday", "Noted.")
    ok, fid = learn.file_fact({"kind": "about", "key": "routine", "text": text}, s, source="digest x",
                              turn=traces.get(tid))
    assert ok
    memory.reindex(s, bag_embed)
    return fid


def seed_correction(wrong=WRONG, correction=CORRECTION, asked=ASKED):
    a = turn(asked, wrong)
    b = turn(correction, "Sorry, fixed.")
    return a, b


def run(s, **kw):
    return learnloop.run(s, now=time.time(), embedder=bag_embed, chat=kw.pop("chat", _no_model), **kw)


def _no_model(*a, **k):
    raise ConnectionError("no local model in this test")


# ---- recognising a correction ------------------------------------------------------------------------------

POSITIVE = [
    ("No, my robotics team meets on Tuesday and Thursday", "wrong"),
    ("no, that's not what I asked for", "misread"),
    ("That's wrong. My team meets Tuesday and Thursday.", "wrong"),
    ("that is incorrect, the meet is on the 22nd", "wrong"),
    ("You got that wrong, I live in Fargo", "wrong"),
    ("Wrong.", "wrong"),
    ("Incorrect, it was 9 am", "wrong"),
    ("actually it's 26 holes not 24", "wrong"),
    ("Actually, my team meets on Tuesday and Thursday", "wrong"),
    ("Nope, I sold the old robot last month", "wrong"),
    ("I meant the other repository", "misread"),
    ("I didn't ask for a summary", "misread"),
    ("You misunderstood me", "misread"),
    ("👎", "thumbs_down"),
    ("thumbs down", "thumbs_down"),
    ("That's totally wrong.", "wrong"),
]


@pytest.mark.parametrize("text,kind", POSITIVE)
def test_a_pushback_on_the_answer_is_recognised(text, kind):
    v = learnloop.pushback(text, "Here is the answer.")
    assert v and v["kind"] == kind, v


NEGATIVE = [
    "No problem", "no worries, thanks", "No thanks", "no need to hurry", "No idea what that means",
    "no, I don't want that one", "No, I haven't", "nope", "no", "No.", "Nah",
    "no, that's fine", "No, it's fine, thanks",
    "actually that's great", "Actually, that's perfect", "actually I have a question about the meet",
    "actually can you also check the weather", "actually never mind", "Actually I need a ride on Saturday",
    "Is that wrong?", "wrong turn on the road, how do I get back?", "Why did you say that?", "not now, thanks",
    "that's not bad at all", "That's right", "correct", "thanks, that's exactly what I meant!",
    "great, that worked", "Know what? I like it", "now what?", "Nowhere else to go", "nothing else",
    "yes", "ok do it", "remind me tomorrow at 7", "what is wrong with my robot?",
    "I said I'd be there at 5, can you remind me?", "No, I haven't finished the report yet, give me an hour",
    "x" * 700,
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_everything_else_is_not_a_correction(text):
    assert learnloop.pushback(text, "Here is the answer.") is None, text


def test_a_no_to_a_question_waggle_asked_is_an_answer_not_a_correction():
    assert learnloop.pushback("No, make it for tomorrow instead", "Should I set the reminder for today?") is None
    assert learnloop.pushback("No, my robotics team meets Tuesday", "Your team meets Monday.") is not None


def test_the_claim_is_what_the_owner_stated_after_the_pushback_words():
    assert learnloop.pushback("No, my team meets Tue and Thu", "x")["claim"] == "my team meets Tue and Thu"
    assert learnloop.pushback("That's wrong. My team meets Tuesday and Thursday.", "x")["claim"] == \
        "My team meets Tuesday and Thursday"
    assert learnloop.pushback("Wrong.", "x")["claim"] == ""
    assert learnloop.standalone("my team meets Tue and Thu")
    for weak in ("it's Tuesday", "that's the old one", "please do it again now", "Tuesday"):
        assert not learnloop.standalone(weak), weak


# ---- which turns pair up -----------------------------------------------------------------------------------------

def pairs(s=None, **kw):
    rows = traces.all_traces(0, include_non_user=True)
    return learnloop.find_corrections(rows, **kw)


def test_a_correction_pairs_with_the_answer_just_before_it():
    a, b = seed_correction()
    got = pairs()
    assert len(got) == 1 and got[0]["prev"]["id"] == a and got[0]["turn"]["id"] == b
    assert got[0]["verdict"]["kind"] == "wrong"


def test_only_the_users_own_turns_count_and_a_test_turn_in_between_breaks_the_pair():
    turn(ASKED, WRONG, origin="canary")
    turn(CORRECTION, "Sorry.")  # the owner's turn, but it follows a canary answer
    turn(ASKED, WRONG, origin="user")
    turn(CORRECTION, "x", origin="test")  # a test turn that looks like a correction
    turn(ASKED, WRONG, chat="telegram:222")
    turn(CORRECTION, "x", chat="telegram:222", origin="agent")
    turn(ASKED, WRONG, chat="cli:1", platform="cli", origin=None)  # an old development turn
    turn(CORRECTION, "x", chat="cli:1", platform="cli", origin=None)
    assert pairs() == []


def test_a_correction_must_follow_the_answer_closely_in_the_same_chat():
    a = turn(ASKED, WRONG)
    con = traces._db()
    con.execute("UPDATE traces SET time=time-? WHERE id=?", (3600, a))
    con.commit()
    con.close()
    turn(CORRECTION, "ok")
    turn(ASKED, WRONG, chat="telegram:333")
    turn(CORRECTION, "ok", chat="telegram:444")  # another chat
    assert pairs() == []


def test_an_answer_that_was_not_an_answer_is_not_corrected():
    turn("approve ABCD", "Approved once.", route="approval")
    turn("no, that is not right", "ok")
    turn("/waggle", "Waggle status", route="command")
    turn("No, the other one please", "ok")
    turn("fix the build", "", route="sonnet")  # no reply at all
    turn("No, I meant the other build", "ok")
    assert pairs() == []


def test_a_turn_whose_own_answer_failed_is_not_a_fact_correction(s):
    a = turn(ASKED, WRONG, turn_outcome="failed")
    turn("No, try the other one", "ok")
    assert pairs() == []


# ---- a wrong fact corrected, re-checked and kept -----------------------------------------------------------------

def fact_texts(status="active"):
    return [f["text"] for f in learn.facts(status)]


def test_a_correction_replaces_the_wrong_fact_and_is_kept_when_it_re_checks(s):
    old = seed_old_fact(s)
    a, b = seed_correction()
    rep = run(s)
    assert rep["signals"]["correction"] == 1 and rep["changes"] == 1
    (row,) = learnloop.rows()
    assert row["status"] == "kept" and row["source"] == "correction" and row["origin"] == "user"
    assert row["checks"]["recheck_passed"] and row["checks"]["held_out_lost"] == []
    # the new fact is the owner's own words, sourced to the correcting turn; the old one is closed, not erased
    new = next(f for f in learn.facts("active"))
    assert new["text"] == "my robotics team meets on Tuesday and Thursday" and new["source_turn"] == b
    assert new["origin"] == "user" and new["source"] == "learnloop correction"
    (gone,) = learn.facts("replaced")
    assert gone["id"] == old and gone["replaced_by"] == new["id"] and gone["invalid_at"]
    prof = vault.read("Me/Profile.md", s)
    assert "Tuesday and Thursday" in prof and "Monday and Wednesday" not in prof
    # the answered turn carries the owner's verdict
    prev = traces.get(a)
    assert prev["feedback"] == -1 and prev["correction"] == CORRECTION
    # the evening note says what happened, with the undo code
    line = learnloop.evening_lines(time.time(), settings=s)
    assert line.startswith("Fixed: your robotics team meets on Tuesday and Thursday (you corrected me).")
    assert f"Undo: reply 'undo L{row['id']}'" in line
    # a second pass over the same turns changes nothing
    again = run(s)
    assert again["signals"]["correction"] == 0 and again["changes"] == 0 and len(learnloop.rows()) == 1


def test_a_correction_that_a_known_fact_already_covers_files_nothing(s):
    seed_old_fact(s, text="my robotics team meets on Tuesday and Thursday")
    seed_correction()
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] in ("noted", "needs_code")  # the fact was known: this is retrieval's problem, not a fix
    assert len(learn.facts("active")) == 1 and not learn.facts("replaced")


# ---- reverted: the re-check fails, or a held-out item is lost --------------------------------------------------------

def test_a_change_whose_recheck_fails_is_reverted_and_the_old_fact_comes_back(s, monkeypatch):
    old = seed_old_fact(s)
    seed_correction()
    monkeypatch.setattr(learnloop, "retrieval_check", lambda gate, q, expect: (False, "retrieved nothing"))
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "reverted" and "re-check still fails" in row["reason"]
    assert [f["id"] for f in learn.facts("active")] == [old]
    (gone,) = learn.facts("reverted")
    assert gone["text"] == "my robotics team meets on Tuesday and Thursday"
    back = learn.facts("active")[0]
    assert back["replaced_by"] is None and back["invalid_at"] is None
    prof = vault.read("Me/Profile.md", s)
    assert "Monday and Wednesday" in prof and "Tuesday" not in prof
    line = learnloop.evening_lines(time.time(), settings=s)
    assert line.startswith("Tried a fix, put it back:") and f"[L{row['id']}]" in line and "Undo" not in line


def _bank(ids="abc"):
    return {i: {"id": i, "note": "n", "question": f"question {i} ___", "answer": f"answer{i}", "active": True}
            for i in ids}


def test_a_change_that_makes_a_held_out_quiz_item_fail_is_reverted(s, monkeypatch):
    old = seed_old_fact(s)
    seed_correction()
    kstore.put("quiz.bank", _bank())
    calls = []

    def answered(self, items):
        calls.append(sorted(i["id"] for i in items))
        return {"a", "b", "c"} if len(calls) == 1 else {"a", "b"}  # c was answered before the change, not after
    monkeypatch.setattr(learnloop.Gate, "answered", answered)
    run(s)
    (row,) = learnloop.rows()
    assert calls == [["a", "b", "c"], ["a", "b", "c"]]  # the same sample, before and after
    assert row["status"] == "reverted" and row["reason"].startswith("1 held-out quiz item(s) lost: c")
    assert row["checks"]["recheck_passed"] is True and row["checks"]["held_out_lost"] == ["c"]
    assert [f["id"] for f in learn.facts("active")] == [old]
    assert "Tuesday" not in vault.read("Me/Profile.md", s)


def test_an_item_that_was_already_missed_before_the_change_does_not_block_it(s, monkeypatch):
    seed_old_fact(s)
    seed_correction()
    kstore.put("quiz.bank", _bank())
    monkeypatch.setattr(learnloop.Gate, "answered", lambda self, items: {"a", "b"})  # c is missed, before and after
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "kept" and row["checks"]["held_out_before"] == 2


def test_nothing_is_kept_when_retrieval_cannot_be_measured(s, monkeypatch):
    old = seed_old_fact(s)
    seed_correction()
    kstore.put("quiz.bank", _bank())

    def down(self, items):
        raise RuntimeError("retrieval unavailable")
    monkeypatch.setattr(learnloop.Gate, "answered", down)
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "deferred" and "cannot prove" in row["reason"]
    assert [f["id"] for f in learn.facts("active")] == [old] and not learn.facts("reverted")  # nothing was changed
    assert learnloop.evening_lines(time.time(), settings=s) == ""


def test_a_held_out_check_that_breaks_after_the_change_reverts_it(s, monkeypatch):
    old = seed_old_fact(s)
    seed_correction()
    kstore.put("quiz.bank", _bank())
    calls = []

    def answered(self, items):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("retrieval unavailable")
        return {"a", "b", "c"}
    monkeypatch.setattr(learnloop.Gate, "answered", answered)
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "reverted" and "could not run" in row["reason"]
    assert [f["id"] for f in learn.facts("active")] == [old]


# ---- undo, from the engine chat, with no model -----------------------------------------------------------------------

def kept_row(s):
    old = seed_old_fact(s)
    seed_correction()
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "kept"
    return old, row


def test_undo_reverts_the_change_and_it_is_not_tried_again(s):
    old, row = kept_row(s)
    kind, text = chat_engine.approval_preflight(CHAT, f"undo L{row['id']}")
    assert kind == "reply" and text.startswith(f"Undone L{row['id']}") and "Monday and Wednesday" in text
    assert [f["id"] for f in learn.facts("active")] == [old]
    prof = vault.read("Me/Profile.md", s)
    assert "Monday and Wednesday" in prof and "Tuesday" not in prof
    assert learnloop.get(row["id"])["status"] == "undone" and "owner" in learnloop.get(row["id"])["reason"]
    assert "already reverted" in chat_engine.approval_preflight(CHAT, f"undo L{row['id']}")[1]
    again = run(s)  # the same correction is in the records still: it must not come back
    assert again["changes"] == 0 and [f["id"] for f in learn.facts("active")] == [old]
    assert learnloop.evening_lines(time.time(), settings=s) == ""  # an undone change is not reported as a fix


@pytest.mark.parametrize("text,id_", [("undo L7", 7), ("Undo l12.", 12), ("  undo #L3 !", 3), ("UNDO L100", 100)])
def test_the_undo_command_forms(text, id_):
    assert int(learnloop.UNDO_CMD.match(text).group(1)) == id_


@pytest.mark.parametrize("text", ["undo", "undo 7", "undo the last commit", "undo L", "please undo L7",
                                  "undo L7 and L8", "can you undo L7?", "redo L7"])
def test_other_messages_are_not_undo_commands(text):
    assert not learnloop.UNDO_CMD.match(text)


def test_undo_of_an_unknown_id_or_a_request_without_a_change(s):
    assert "no change L99" in chat_engine.approval_preflight(CHAT, "undo L99")[1]
    i, _ = learnloop.add("tool:x:1", "failed_turn", "tool_failure", "A tool fails", {}, status="needs_code")
    text = chat_engine.approval_preflight(CHAT, f"undo L{i}")[1]
    assert "no change to undo" in text and learnloop.get(i)["status"] == "dismissed"
    assert learnloop.code_requests() == []
    assert "already dismissed" in chat_engine.approval_preflight(CHAT, f"undo L{i}")[1]


def test_undo_restores_the_exact_state_even_after_other_notes_changed(s):
    old, row = kept_row(s)
    vault.append("Me/Learned.md", "- Likes quiet mornings (2026-11-20)", s)
    learnloop.undo(row["id"], s)
    assert "Likes quiet mornings" in vault.read("Me/Learned.md", s)
    assert learn.facts("replaced") == [] and [f["id"] for f in learn.facts("active")] == [old]


def test_undo_through_the_engine_needs_no_model(s, monkeypatch):
    from test_engine_inbound import inbound
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")
    old, row = kept_row(s)
    rec, api, fake = inbound(s, f"undo L{row['id']}")
    assert fake.procs == [] and rec.answered_by == "none" and rec.delivered
    assert api.messages[rec.message_ids[0]].startswith(f"Undone L{row['id']}")
    assert traces.get(rec.trace_id)["route"] == "approval" and learnloop.get(row["id"])["status"] == "undone"


# ---- the evening note ---------------------------------------------------------------------------------------------

def ledger(status, summary, reason="", source="correction", key=None, now=None):
    i, _ = learnloop.add(key or f"k:{summary}", source, "wrong_fact", summary, {}, status=status, now=now)
    learnloop.update(i, status=status, reason=reason, now=now)
    return i


def test_the_evening_lines_cover_kept_reverted_and_needs_code_once_a_day(s):
    now = time.time()
    k = ledger("kept", "my team meets Tue and Thu (you corrected me)")
    r = ledger("reverted", "the scale is 3 kg (you corrected me; I put it back)", reason="the re-check still fails")
    n = ledger("needs_code", "A tool keeps failing: github", source="failed_turn")
    ledger("noted", "nothing to change")
    ledger("deferred", "waiting for a night with budget")
    ledger("undone", "something the owner undid")
    lines = learnloop.evening_lines(now, settings=s).splitlines()
    assert lines == [f"Fixed: my team meets Tue and Thu (you corrected me). Undo: reply 'undo L{k}'.",
                     f"Tried a fix, put it back: the scale is 3 kg (you corrected me; I put it back) "
                     f"(the re-check still fails) [L{r}].",
                     f"Needs a code change: A tool keeps failing: github [L{n}]."]
    assert learnloop.evening_lines(now + 60, settings=s).splitlines() == lines  # a recompose the same day repeats it
    assert learnloop.evening_lines(now + 86400, settings=s) == ""  # tomorrow it is old news


def test_changes_are_never_cut_from_the_note_but_code_requests_are_summarised(s):
    for i in range(6):
        ledger("kept", f"fact number {i}")
    for i in range(5):
        ledger("needs_code", f"code request {i}", source="failed_turn")
    out = learnloop.evening_lines(time.time(), settings=s).splitlines()
    assert [l.split(":")[0] for l in out[:9]] == ["Fixed"] * 6 + ["Needs a code change"] * 3
    assert len(out) == 10 and out[-1].startswith("...and 2 more code requests are in the improvement ledger")
    assert all(r["reported"] for r in learnloop.rows())  # the summarised ones are in the ledger, not lost


def test_a_claim_about_the_assistant_is_not_filed_as_a_fact(s):
    seed_old_fact(s)
    seed_correction(correction="No, I want you to answer shorter next time")
    seen = []
    run(s, chat=lambda messages, **k: seen.append(1) or {"verdict": "style", "fact": ""})
    (row,) = learnloop.rows()
    assert seen and row["status"] == "needs_code" and row["kind"] == "route_style"  # not "standalone": the model decides
    assert learn.facts("replaced") == []
    assert "shorter" not in vault.read("Me/Learned.md", s) + vault.read("Me/Profile.md", s)


def test_the_evening_note_carries_the_lines(s):
    old, row = kept_row(s)
    text = brief.evening_note(s, time.time())
    assert "Fixed: your robotics team meets on Tuesday and Thursday (you corrected me)." in text
    assert f"undo L{row['id']}" in text
    assert "Fixed:" not in brief.evening_note(s, time.time() + 2 * 86400)


def test_the_evening_note_survives_a_broken_ledger(s, monkeypatch):
    monkeypatch.setattr(learnloop, "_con", lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    assert learnloop.evening_lines(time.time()) == ""
    assert "Evening note" in brief.evening_note(s, time.time())


# ---- only the owner's own turns are learning input -------------------------------------------------------------------

@pytest.mark.parametrize("origin,platform", [("canary", "telegram"), ("test", "telegram"), ("agent", "telegram"),
                                              ("bench", "cli"), (None, "cli")])
def test_a_non_user_turn_never_teaches_the_loop(s, origin, platform):
    seed_old_fact(s)
    a = turn(ASKED, WRONG, origin=origin, platform=platform)
    turn(CORRECTION, "ok", origin=origin, platform=platform)
    turn("fix it", "", origin=origin, platform=platform, steps=[
        {"tool": "mcp__x__y", "input": "i", "ok": False, "result": "HTTP 500 boom"}], turn_outcome="failed")
    rep = run(s)
    assert rep["signals"] == {"correction": 0, "quiz": 0, "failed_turn": 0} and learnloop.rows() == []
    assert traces.get(a)["feedback"] == 0 and learn.facts("replaced") == []


def test_a_canary_correction_does_not_change_a_real_fact_even_in_the_owners_chat(s):
    old = seed_old_fact(s)
    turn(ASKED, WRONG)
    turn("[waggle-test] " + CORRECTION, "ok", origin="canary")
    run(s)
    assert learnloop.rows() == [] and [f["id"] for f in learn.facts("active")] == [old]


# ---- wording the claim: the owner's words, the local model, then (budget permitting) Claude --------------------------

PRONOUN = "No, it's Tuesday and Thursday"
GOOD_FACT = {"verdict": "fact", "fact": "my robotics team meets on Tuesday and Thursday", "kind": "about"}


def pronoun_case(s):
    old = seed_old_fact(s)
    seed_correction(correction=PRONOUN)
    return old


def test_a_claim_that_needs_the_question_is_worded_by_the_local_model(s):
    old = pronoun_case(s)
    seen = []
    rep = run(s, chat=lambda messages, **k: seen.append(messages) or GOOD_FACT)
    (row,) = learnloop.rows()
    assert row["status"] == "kept" and row["evidence"]["claim_via"] == "local" and rep["claude_calls"] == 0
    assert "when does my robotics team meet" in seen[0][1]["content"]  # it saw the question, to resolve "it"
    assert learn.facts("active")[0]["text"] == "my robotics team meets on Tuesday and Thursday"


def test_a_model_that_invents_something_the_owner_never_said_is_refused(s, monkeypatch):
    from waggle import quota
    old = pronoun_case(s)
    invented = {"verdict": "fact", "fact": "my robotics team meets at the school gym on Friday", "kind": "fact"}
    monkeypatch.setattr(quota, "can_escalate", lambda model, settings=None, **k: (False, "no budget"))
    run(s, chat=lambda messages, **k: invented, asker=lambda *a, **k: pytest.fail("no Claude without budget"))
    (row,) = learnloop.rows()
    assert row["status"] in ("deferred", "noted") and [f["id"] for f in learn.facts("active")] == [old]


def test_claude_is_used_only_when_the_local_model_fails_and_the_budget_allows(s, monkeypatch):
    from waggle import quota
    old = pronoun_case(s)
    calls = []

    def asker(prompt, **kw):
        calls.append(kw)
        return {"json": GOOD_FACT}
    monkeypatch.setattr(quota, "can_escalate", lambda model, settings=None, **k: (False, "Pro window at 95%"))
    run(s, asker=asker)
    (row,) = learnloop.rows()
    assert calls == [] and row["status"] == "deferred" and "Claude not used: Pro window at 95%" in row["reason"]
    assert [f["id"] for f in learn.facts("active")] == [old]
    assert learnloop.evening_lines(time.time(), settings=s) == ""
    monkeypatch.setattr(quota, "can_escalate", lambda model, settings=None, **k: (True, "ok"))
    rep = run(s, asker=asker)  # a later night with budget: the same row is picked up
    (row,) = learnloop.rows()
    assert row["status"] == "kept" and row["evidence"]["claim_via"] == "claude" and rep["claude_calls"] == 1
    assert calls[0]["purpose"] == "learnloop" and calls[0]["model"] == s.router_model


def test_the_budget_machinery_refuses_the_claude_call_in_a_background_job(s):
    s.background_daily_units = 0  # background Claude use switched off: the real checks, no monkeypatching
    s.save()
    old = pronoun_case(s)
    out = learnloop.job({}, settings=s, now=time.time(), embedder=bag_embed, chat=_no_model,
                        asker=lambda *a, **k: pytest.fail("Claude must not be asked"))
    (row,) = learnloop.rows()
    assert out["claude_calls"] == 0 and out["statuses"] == ["deferred"] and "switched off" in row["reason"]
    assert [f["id"] for f in learn.facts("active")] == [old]


def test_claude_calls_per_run_are_limited_and_a_stale_deferral_is_given_up(s, monkeypatch):
    from waggle import quota
    monkeypatch.setattr(quota, "can_escalate", lambda model, settings=None, **k: (True, "ok"))
    monkeypatch.setattr(learnloop, "MAX_CLAUDE", 0)
    pronoun_case(s)
    run(s, asker=lambda *a, **k: pytest.fail("over the limit"))
    (row,) = learnloop.rows()
    assert row["status"] == "deferred" and "limit for this run" in row["reason"]
    con = kstore.db()
    con.execute("UPDATE improvements SET created=created-?", (4 * 86400,))
    con.commit()
    con.close()
    run(s, asker=lambda *a, **k: pytest.fail("over the limit"))
    assert learnloop.rows()[0]["status"] == "noted" and "gave up" in learnloop.rows()[0]["reason"]


def test_a_claude_failure_leaves_the_row_for_later_and_changes_nothing(s, monkeypatch):
    from waggle import quota
    monkeypatch.setattr(quota, "can_escalate", lambda model, settings=None, **k: (True, "ok"))
    old = pronoun_case(s)

    def boom(*a, **k):
        raise RuntimeError("claude exploded")
    run(s, asker=boom)
    (row,) = learnloop.rows()
    assert row["status"] == "deferred" and "claude exploded" in row["reason"]
    assert [f["id"] for f in learn.facts("active")] == [old]


# ---- other kinds of correction: style, tool failure, a bare rating -----------------------------------------------------

def test_a_correction_about_style_goes_to_the_code_queue_with_its_evidence(s):
    a, b = seed_correction(correction="No, that's not what I asked for")
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "needs_code" and row["kind"] == "route_style"
    assert row["evidence"]["said"] == "No, that's not what I asked for" and row["evidence"]["answered_trace"] == a
    assert row["evidence"]["asked"] == ASKED and row["evidence"]["trace_id"] == b
    assert learn.facts(None) == []
    (req,) = learnloop.code_requests()
    assert set(req) == {"ledger_id", "title", "evidence", "origin"} and req["origin"] == "learnloop"
    assert req["title"].startswith("The owner corrected the style or reading of an answer")


def test_a_correction_after_a_failed_tool_is_a_tool_failure_for_code(s):
    turn(ASKED, WRONG, steps=[{"tool": "mcp__github__list_repos", "input": "x", "ok": False,
                               "result": "gh: not signed in"}], turn_outcome="degraded")
    turn("Wrong.", "Sorry.")
    run(s)
    rows = [r for r in learnloop.rows() if r["source"] == "correction"]
    assert len(rows) == 1 and rows[0]["status"] == "needs_code" and rows[0]["kind"] == "tool_failure"
    assert rows[0]["evidence"]["tool_failed"] == "list_repos"


def test_a_thumbs_down_marks_the_turn_and_files_nothing(s):
    a = turn(ASKED, WRONG)
    turn("👎", "Noted.")
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "noted" and traces.get(a)["feedback"] == -1 and learn.facts(None) == []


def test_a_mark_the_old_feedback_hook_already_left_is_not_overwritten(s):
    a = turn(ASKED, WRONG)
    traces.set_feedback(a, -1, "an earlier note")
    turn("👎", "Noted.")
    run(s)
    assert traces.get(a)["correction"] == "an earlier note"
    assert len(learnloop.rows()) == 1


# ---- failed turns --------------------------------------------------------------------------------------------------

def test_one_ledger_row_per_failing_tool_with_the_turns_counted(s):
    fail = [{"tool": "mcp__github__list_repos", "input": "x", "ok": False, "result": "HTTP 401 unauthorized at 12:01"}]
    t1 = turn("list my repos", "I could not.", steps=fail, turn_outcome="degraded")
    fail2 = [{"tool": "mcp__github__list_repos", "input": "x", "ok": False, "result": "HTTP 401 unauthorized at 13:44"}]
    t2 = turn("list my repos again", "Still not.", steps=fail2, turn_outcome="degraded")
    turn("an answer that failed", "", turn_outcome="failed", outcome_why=["no answer: a failure notice was sent"])
    rep = run(s)
    rows = learnloop.rows()
    assert rep["signals"]["failed_turn"] == 2 and {r["status"] for r in rows} == {"needs_code"}
    gh = next(r for r in rows if r["evidence"].get("tool") == "list_repos")
    assert gh["evidence"]["turns"] == [t1, t2] and "unauthorized" in gh["evidence"]["error"]
    assert gh["origin"] == "user" and gh["source"] == "failed_turn"
    t3 = turn("and once more", "no", steps=fail, turn_outcome="degraded")
    again = run(s)
    assert again["signals"]["failed_turn"] == 0 and len(learnloop.rows()) == 2  # the same failure is counted, not repeated
    assert learnloop.get(gh["id"])["evidence"]["count"] == 3 and t3 in learnloop.get(gh["id"])["evidence"]["turns"]


def test_a_tool_the_hook_refused_is_not_a_failure(s):
    turn("delete it", "I need approval.", steps=[{"tool": "Bash", "input": "rm x", "ok": None, "denied": True,
                                                  "result": "NEEDS_APPROVAL"}], turn_outcome="degraded")
    assert run(s)["signals"]["failed_turn"] == 0


# ---- quiz misses -----------------------------------------------------------------------------------------------

def quiz_last(misses, now=None):
    kstore.put("quiz.last", {"time": now or time.time(), "day": "2026-11-20", "items": 6, "answered": 5, "score": 0.83,
                             "misses": misses})


SENTENCE = "The hive scale model is Zephyrus-9 and it sits by the north gate"
QUESTION = "The hive scale model is ___ and it sits by the north gate"


def held_out_bank(n=5):
    return {f"h{i}": {"id": f"h{i}", "note": "f", "question": f"topic{i} alpha{i} ___ gamma{i}", "answer": f"beta{i}",
                      "active": True} for i in range(n)}


def test_a_quiz_miss_whose_answer_is_not_in_the_vault_is_filed_as_a_fact_and_kept(s):
    fill_vault(s)
    memory.reindex(s, bag_embed)
    bank = {**held_out_bank(), "t1": {"id": "t1", "note": "hive-notes", "question": QUESTION, "answer": "Zephyrus-9",
                                      "active": True}}
    kstore.put("quiz.bank", bank)
    quiz_last([{"id": "t1", "note": "hive-notes", "question": QUESTION, "answer": "Zephyrus-9", "in_vault": False}])
    rep = run(s)
    (row,) = learnloop.rows()
    assert rep["signals"]["quiz"] == 1 and row["status"] == "kept" and row["kind"] == "missing_fact"
    assert row["checks"]["held_out_n"] == 5 and row["checks"]["held_out_before"] == 5  # the target is not in the sample
    (f,) = learn.facts("active")
    assert f["text"] == SENTENCE and f["origin"] == "memory" and f["source"] == "learnloop quiz miss"
    assert SENTENCE in vault.read("Me/Learned.md", s)
    assert row["summary"] == f"{SENTENCE} (my knowledge quiz missed it)"
    assert learnloop.evening_lines(time.time(), settings=s).startswith(f"Fixed: {SENTENCE} (my knowledge quiz")
    # the same quiz result tomorrow is not a new signal
    assert run(s)["signals"]["quiz"] == 0
    learnloop.undo(row["id"], s)
    assert learn.facts("active") == [] and SENTENCE not in vault.read("Me/Learned.md", s)


def test_a_quiz_miss_the_index_alone_explains_is_fixed_silently(s):
    fill_vault(s)
    memory.reindex(s, bag_embed)
    vault.write("Research/Valves", "# Valves\n\nThe valve for the pump house is code Qx-77 and nothing else.\n", s)
    q = "The valve for the pump house is code ___ and nothing else"
    quiz_last([{"id": "v1", "note": "valves", "question": q, "answer": "Qx-77", "in_vault": True}])
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "noted" and "index was stale" in row["reason"]
    assert learn.facts(None) == [] and learnloop.evening_lines(time.time(), settings=s) == ""


def test_a_stored_answer_that_retrieval_cannot_find_waits_for_code_with_a_hint(s):
    fill_vault(s)
    vault.write("Research/Hatch", "# Hatch\n\nThe override sequence is Qx-77 for emergencies.\n", s)
    memory.reindex(s, bag_embed)
    q = "Which pass phrase unlocks the exit: ___"  # shares no word with the note
    quiz_last([{"id": "v2", "note": "hatch", "question": q, "answer": "Qx-77", "in_vault": True}])
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "needs_code" and row["kind"] == "retrieval_miss" and learn.facts(None) == []
    assert row["evidence"]["expected_in"] == ["Research/Hatch.md"] and row["evidence"]["query"] == q
    (hint,) = kstore.get("learnloop.hints")
    assert hint["expected_note"] == "Research/Hatch.md" and hint["answer"] == "Qx-77"
    (req,) = learnloop.code_requests()
    assert req["title"].startswith("Retrieval misses a stored answer")
    assert learnloop.evening_lines(time.time(), settings=s).startswith("Needs a code change: Retrieval misses")


def test_a_miss_retrieval_answers_by_now_is_only_noted(s):
    fill_vault(s)
    memory.reindex(s, bag_embed)
    quiz_last([{"id": "h1", "note": "f", "question": "topic1 alpha1 ___ gamma1", "answer": "beta1", "in_vault": True}])
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "noted" and "answers it now" in row["reason"]


def test_the_quiz_result_must_be_recent_and_a_quiz_that_never_ran_is_a_note(s):
    assert run(s)["notes"] == ["no quiz run in the last 40 h"]
    quiz_last([{"id": "x", "note": "n", "question": "q ___", "answer": "a1b2", "in_vault": False}],
              now=time.time() - 3 * 86400)
    rep = run(s)
    assert rep["signals"]["quiz"] == 0 and learnloop.rows() == []


def test_the_nightly_quiz_stores_the_misses_the_loop_reads(s, tmp_path, monkeypatch):
    from waggle import ingest, learning
    d = tmp_path / "mem"
    d.mkdir()
    (d / "scale.md").write_text("# Scale\n\nThe hive scale model is Zephyrus-9 and it sits by the north gate.\n",
                                encoding="utf-8")
    monkeypatch.setattr(ingest, "memory_dirs", lambda settings=None: [d])
    out = learning.daily(s, embedder=bag_embed)
    last = kstore.get("quiz.last")
    assert out["quiz"]["items"] >= 1 and last["items"] == out["quiz"]["items"]
    assert all(m["id"] and m["answer"] for m in last["misses"]) and len(last["misses"]) == len(last["misses"])
    assert any(m["answer"] == "Zephyrus-9" for m in last["misses"])


# ---- the change limit and dry runs -------------------------------------------------------------------------------

def test_at_most_a_few_facts_change_in_one_night(s):
    fill_vault(s)
    memory.reindex(s, bag_embed)
    misses = []
    for i in range(5):
        sent = f"Device{i} firmware version is Fw{i}-Xq{i}k and lives in rack {i}x"
        vault_free = {"id": f"m{i}", "note": "n", "question": sent.replace(f"Fw{i}-Xq{i}k", "___"),
                      "answer": f"Fw{i}-Xq{i}k", "in_vault": False}
        misses.append(vault_free)
    quiz_last(misses)
    rep = run(s)
    assert rep["changes"] == learnloop.MAX_CHANGES == 3
    assert sorted(r["status"] for r in learnloop.rows()).count("open") == 2
    again = run(s)  # the rest wait for the next night
    assert again["changes"] == 2 and "open" not in {r["status"] for r in learnloop.rows()}


def test_a_dry_run_writes_nothing_and_says_what_it_would_do(s):
    old = seed_old_fact(s)
    a, b = seed_correction()
    quiz_last([{"id": "t1", "note": "hive-notes", "question": QUESTION, "answer": "Zephyrus-9", "in_vault": False}])
    turn("x", "y", steps=[{"tool": "mcp__x__y", "input": "i", "ok": False, "result": "boom"}])
    con = kstore.db()
    before_facts = [dict(r) for r in con.execute("SELECT * FROM facts")]
    con.close()
    rep = run(s, dry_run=True)
    assert rep["signals"] == {"correction": 1, "quiz": 1, "failed_turn": 1}
    by = {r["signal"]: r for r in rep["results"]}
    corr = by[f"turn:{b}"]
    assert corr["verdict"] == "fact" and corr["fact"] == "my robotics team meets on Tuesday and Thursday"
    assert corr["replaces"] == "robotics team meets on Monday and Wednesday" and corr["would"].startswith("file the fact")
    assert by["quiz:t1"]["fact"] == SENTENCE
    con = kstore.db()
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='improvements'").fetchone()  # no ledger even
    assert [dict(r) for r in con.execute("SELECT * FROM facts")] == before_facts
    con.close()
    assert traces.get(a)["feedback"] == 0 and learnloop.known_keys() == set()
    assert "Tuesday" not in vault.read("Me/Profile.md", s)


# ---- the core job and its timer ---------------------------------------------------------------------------------

@pytest.fixture
def clean_jobs():
    jobs._handlers.clear()
    yield
    jobs._handlers.clear()


def test_the_plug_in_registers_a_detached_job_and_a_nightly_timer_before_the_evening_note(s, clean_jobs):
    assert "waggle.learnloop" in plugins.MODULES
    rep = learnloop.register_core(now=1_000_000, settings=s)
    assert rep == {"jobs": [learnloop.KIND], "timers": [learnloop.TIMER]}
    h = jobs.handler(learnloop.KIND)
    assert h.detached and h.timeout_s == learnloop.TIMEOUT_S and h.max_attempts == 2
    (t,) = timers.list(origin="learnloop")
    assert (t["kind"], t["spec"], t["catch_up"], t["max_late_s"]) == ("cron", "30 20 * * *", "latest", 20 * 3600)
    learnloop.register_core(now=1_000_500, settings=s)
    assert len(timers.list(origin="learnloop")) == 1


@pytest.mark.parametrize("evening,cron", [("21:00", "30 20 * * *"), ("07:15", "45 6 * * *"), ("00:10", "40 23 * * *"),
                                          ("", "30 20 * * *"), ("soon", "30 20 * * *")])
def test_the_timer_follows_the_evening_note_time(s, evening, cron):
    s.evening_time = evening
    assert learnloop.cron_spec(s) == cron


def test_the_core_loads_the_plug_in_when_it_is_listed(clean_jobs, monkeypatch):
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS", "waggle.learnloop")
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS_ONLY", "1")
    assert plugins.load(record=False) == {"loaded": ["waggle.learnloop"], "failed": {}}
    assert learnloop.KIND in jobs.kinds(detached=True)


def test_the_job_runs_the_loop_as_background_work_and_returns_a_report(s):
    from waggle import budget
    old = seed_old_fact(s)
    seed_correction()
    seen = []
    real = learnloop.run

    def spy(*a, **k):
        seen.append(budget.is_background())
        return real(*a, **k)
    import unittest.mock as mock
    with mock.patch.object(learnloop, "run", spy):
        out = learnloop.job({}, settings=s, now=time.time(), embedder=bag_embed, chat=_no_model)
    assert seen == [True] and out["changes"] == 1 and out["statuses"] == ["kept"]
    assert not budget.is_background()  # the mark did not leak into this thread
    assert json.dumps(out)


def test_one_bad_row_does_not_stop_the_others(s, monkeypatch):
    seed_old_fact(s)
    seed_correction()
    quiz_last([{"id": "t1", "note": "hive-notes", "question": QUESTION, "answer": "Zephyrus-9", "in_vault": False}])
    real = learnloop.handle_correction

    def broken(ctx, row):
        raise RuntimeError("bad row")
    monkeypatch.setattr(learnloop, "handle_correction", broken)
    rep = run(s)
    statuses = {r["status"] for r in rep["results"]}
    assert "error" in statuses and "kept" in statuses  # the quiz row still went through
    assert real is not broken


# ---- the re-check asks what a chat turn is given: the core block and the gated retrieval ------------------------------

def test_a_fact_the_core_block_carries_passes_the_recheck_even_when_retrieval_has_nothing(s):
    old = seed_old_fact(s)
    for p in vault.notes(s):  # a small vault: retrieval's relevance gate cannot make anything stand out
        if "Filler" in p.name:
            p.unlink()
    memory.reindex(s, bag_embed)
    seed_correction()
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "kept" and row["checks"]["recheck"] == "found in the core block"
    assert row["checks"]["held_out_n"] == 0  # no quiz bank: nothing to lose


def test_a_quiz_fact_that_retrieval_cannot_surface_is_reverted_for_real(s):
    for i in range(30):  # thirty notes that say nearly the same, so the new line is lost among them
        vault.write(f"Research/Scale{i}", f"# Scale {i}\n\nThe hive scale model is Alpha{i} and it sits by the north "
                    f"gate of site {i}.\n", s)
    memory.reindex(s, bag_embed)
    quiz_last([{"id": "t1", "note": "hive-notes", "question": QUESTION, "answer": "Zephyrus-9", "in_vault": False}])
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "reverted" and "re-check still fails" in row["reason"]
    assert learn.facts("active") == [] and SENTENCE not in vault.read("Me/Learned.md", s)
    assert [f["status"] for f in learn.facts(None)] == ["reverted"]
