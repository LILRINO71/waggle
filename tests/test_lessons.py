"""Keyword lessons, the hooks-alive check and fact ordering (borrow-list rows 5, 6 and 8).

Fakes and a temp home only: no Claude, no Ollama, no real Telegram. Invented content only."""
import json
import sys
import textwrap
import time

import pytest
from engine_fakes import FakeClaude, FakeDelivery

from waggle import canary as cn
from waggle import chat_engine, context_alive, digest, doctor, learn, learnloop, lessons, traces, vault
from waggle.chat_session import ChatSession
from waggle.core import ledger, store


@pytest.fixture(autouse=True)
def _close_store():
    chat_engine._available.clear()
    yield
    store.close_all()


@pytest.fixture
def s(settings, tmp_path):
    settings.watch_roots = [str(tmp_path / "work")]
    settings.save()
    (tmp_path / "work").mkdir()
    vault.init(settings)
    return settings


# ---- 1. the lessons store and matching ----------------------------------------------------------------------------

def test_a_lesson_is_a_markdown_file_with_front_matter_and_matches_its_keywords():
    lid = lessons.add(["repos", "private repos"], "When asked for repos, use gh and include private repos.",
                      why="a corrected answer", source="test")
    text = (lessons.lessons_dir() / f"{lid}.md").read_text(encoding="utf-8")
    assert text.startswith("---\nmatch: [repos, private repos]\nwhy: a corrected answer\nsource: test\nadded: ")
    (row,) = lessons.load_all()
    assert row["id"] == lid and row["match"] == ["repos", "private repos"] and row["body"].startswith("When asked")
    assert [r["id"] for r in lessons.match("show me my GitHub Repos please")] == [lid]  # case does not matter
    assert [r["id"] for r in lessons.match("how many repo")] == [lid]  # the singular and plural both count
    assert lessons.match("list my private repos")[0]["hits"] == ["repos", "private repos"]


def test_keywords_are_narrow_a_message_without_one_gets_nothing():
    lessons.add(["repos"], "Use gh and include private repos when listing repositories.")
    for text in ["what is the weather", "reposition the camera", "I want to report a bug", "", "   ", "preposterous"]:
        assert lessons.match(text) == [], text  # whole words only: "reposition", "report", "preposterous" are not "repos"
    assert lessons.block("what is the weather") == ("", [])


@pytest.mark.parametrize("word", ["git", "python", "test", "code", "file", "waggle", "help"])
def test_a_generic_single_word_keyword_is_refused(word):
    with pytest.raises(lessons.LessonError, match="too generic"):
        lessons.add([word], "Always do the careful thing here, please.")
    assert lessons.load_all() == []


def test_a_phrase_is_fine_even_when_each_word_is_generic():
    lid = lessons.add(["git push"], "Never force push; ask the owner first.")
    assert [r["id"] for r in lessons.match("can you git push this?")] == [lid]
    assert lessons.match("push the button, then git status") == []


def test_bad_lessons_are_refused_with_a_reason():
    bad = [([], "text that is long enough"), (["repos"], "short"), (["repos"], "x" * (lessons.BODY_MAX + 1)),
           (["a"], "text that is long enough"), ([f"kw{i}one" for i in range(9)], "text that is long enough"),
           (["repos"], "my token is sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCD")]
    for keys, body in bad:
        with pytest.raises(lessons.LessonError):
            lessons.add(keys, body)
    assert lessons.load_all() == []


def test_the_same_lesson_twice_is_refused_and_remove_is_idempotent():
    lid = lessons.add(["repos"], "Use gh and include private repos when listing repositories.")
    with pytest.raises(lessons.LessonError, match="already a lesson"):
        lessons.add(["repos"], "Use gh and include private repos when listing repositories!")
    assert lessons.remove(lid) is True and lessons.remove(lid) is False and lessons.remove("../../x") is False
    assert lessons.load_all() == []


def test_the_size_cap_is_three_lessons_and_600_characters_never_cut_in_half():
    fillers = ["alpha beta gamma", "delta epsilon zeta", "eta theta iota", "kappa lambda sigma", "omega tau upsilon"]
    for i in range(5):
        lessons.add(["calendar"], f"Rule number {i}: " + (fillers[i] + " ") * 8, now=1_700_000_000 + i * 86400)
    got = lessons.match("what is on my calendar")
    assert len(got) == lessons.MAX_LESSONS == 3
    assert sum(len(r["body"]) for r in got) <= lessons.MAX_CHARS == 600
    assert [r["body"][:13] for r in got] == ["Rule number 4", "Rule number 3", "Rule number 2"]  # the newest first
    block, ids = lessons.block("what is on my calendar")
    assert len(ids) == 3 and block.startswith("<waggle_lessons>") and block.count("\n- ") == 3
    # a lesson that does not fit is left out whole: with a 600-character budget only two 240-character ones fit
    for r in lessons.load_all():
        lessons.remove(r["id"])
    for i, ch in enumerate("xyz"):
        lessons.add(["robot"], f"Lesson {i}: " + ch * 220, now=1_700_000_000 + i * 86400)
    got = lessons.match("tell me about the robot")
    assert len(got) == 2 and sum(len(r["body"]) for r in got) <= 600


def test_more_matched_keywords_wins_over_newer():
    old = lessons.add(["robot", "intake"], "Rule about the robot intake, be exact.", now=1_700_000_000)
    new = lessons.add(["robot"], "Rule about the robot in general, newer.", now=1_700_000_000 + 86400)
    assert [r["id"] for r in lessons.match("how is the robot intake")] == [old, new]


def test_a_hand_written_file_works_and_a_damaged_one_is_skipped():
    d = lessons.lessons_dir(create=True)
    (d / "mine.md").write_text('---\nmatch: ["meet day", pit]\nwhy: x\nsource: me\nadded: 2026-11-01\n---\n'
                               "Always bring the checklist.\n", encoding="utf-8")
    (d / "broken.md").write_text("no front matter at all", encoding="utf-8")
    (d / "empty.md").write_bytes(b"\xff\xfe\x00")
    assert [r["id"] for r in lessons.load_all()] == ["mine"]
    assert lessons.match("when is meet day?")[0]["id"] == "mine"


def test_lessons_can_be_switched_off_and_on():
    lessons.add(["repos"], "Use gh and include private repos when listing repositories.")
    assert lessons.block("my repos")[1]
    lessons.set_enabled(False)
    assert lessons.enabled() is False and lessons.block("my repos") == ("", [])
    lessons.set_enabled(True)
    assert lessons.block("my repos")[1]


def test_the_command_line(capsys):
    assert lessons.main(["add", "--match", "repos,private repos", "Use gh", "and", "include", "private", "repos."]) == 0
    lid = capsys.readouterr().out.split()[-1]
    assert lessons.main(["add", "--match", "git", "Be careful with git, always."]) == 1
    assert "too generic" in capsys.readouterr().out
    assert lessons.main(["list"]) == 0 and lid in capsys.readouterr().out
    assert lessons.main(["match", "list", "my", "repos"]) == 0
    out = capsys.readouterr().out
    assert lid in out and "1 lesson(s) would be injected" in out
    assert lessons.main(["remove", lid]) == 0 and "removed" in capsys.readouterr().out
    assert lessons.main(["off"]) == 0 and lessons.enabled() is False


# ---- 1b. the injection into a chat turn --------------------------------------------------------------------------------

def one_turn(settings, text, origin="user", chat="telegram:5"):
    """A real run_turn with the real build_context and a fake Claude; returns (record, text sent to Claude)."""
    settings.edit_interval_s = 0.05
    sent = []

    def script(proc, msg):
        sent.append(msg)
        proc.answer("Done.")
    sess = ChatSession(chat, settings=settings, popen=FakeClaude(script), claude="claude")
    rec = chat_engine.run_turn(chat, text, FakeDelivery(), platform="telegram", origin=origin, settings=settings,
                               session=sess)
    return rec, (sent[0] if sent else "")


def extra_of(rec):
    return json.loads(traces.get(rec.trace_id)["extra"])


def test_a_matching_lesson_reaches_the_turn_the_model_sees(s):
    lid = lessons.add(["repos"], "Owner's rule (when they ask for repos): use gh and include private repos.")
    rec, sent = one_turn(s, "list my github repos")
    assert "<waggle_lessons>" in sent and "use gh and include private repos" in sent
    assert sent.index("<waggle_lessons>") < sent.index("list my github repos")  # in the context, before the message
    ctx = extra_of(rec)["context"]
    assert ctx["lessons"] == [lid] and ctx["lessons_checked"] is True and ctx["core"] is True
    assert "lessons" in ctx["sources"] and "core" in ctx["sources"]


def test_a_message_about_something_else_gets_no_lessons_block(s):
    lessons.add(["repos"], "Owner's rule (when they ask for repos): use gh and include private repos.")
    rec, sent = one_turn(s, "what time is the meet")
    assert "<waggle_lessons>" not in sent and "use gh" not in sent
    ctx = extra_of(rec)["context"]
    assert ctx["lessons"] == [] and ctx["lessons_checked"] is True and "lessons" not in ctx["sources"]


def test_test_marked_canary_turns_get_lessons_too(s):
    lessons.add(["repos"], "Owner's rule (when they ask for repos): use gh and include private repos.")
    rec, sent = one_turn(s, "list my repos", origin="canary")
    assert "use gh and include private repos" in sent and extra_of(rec)["context"]["lessons"]
    assert traces.get(rec.trace_id)["origin"] == "canary"


def test_a_broken_lessons_folder_never_breaks_a_turn(s, monkeypatch):
    monkeypatch.setattr(lessons, "match", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk gone")))
    rec, sent = one_turn(s, "list my repos")
    assert rec.reply == "Done." and "<waggle_lessons>" not in sent and "<waggle_core>" in sent


# ---- 1c. the learning loop writes a lesson from a behaviour correction, and undo removes it -----------------------------

CHAT = "telegram:111"
ASKED = "list my github repos"
WRONG = "You have 3 public repos: alpha, beta and gamma."
RULE = "No, use gh and include private repos"


def turn(request, reply="", *, chat=CHAT, origin="user", platform="telegram", **kw):
    return traces.record(request, session=chat, chat=chat, platform=platform, origin=origin, reply=reply, route="sonnet",
                         **kw)


def run(s, **kw):
    return learnloop.run(s, now=time.time(), chat=kw.pop("chat", _no_model), **kw)


def _no_model(*a, **k):
    raise ConnectionError("no local model in this test")


def test_a_behaviour_correction_becomes_a_lesson_that_fires_on_the_same_question(s):
    a = turn(ASKED, WRONG)
    b = turn(RULE, "Sorry.")
    rep = run(s)
    (row,) = learnloop.rows()
    assert rep["claude_calls"] == 0  # words only: no model asked
    assert row["status"] == "kept" and row["kind"] == "lesson" and row["change"]["kind"] == "lesson"
    assert row["checks"]["recheck_passed"] and row["checks"]["false_triggers"] == 0
    (les,) = lessons.load_all()
    assert les["id"] == row["change"]["lesson_id"] == f"l{row['id']}-repos"
    assert les["match"] == ["repos"] and les["source"] == f"learnloop L{row['id']}"
    assert "use gh and include private repos" in les["body"] and ASKED in les["body"]
    assert lessons.block("show my repos again")[1] == [les["id"]]
    assert learn.facts(None) == []  # a rule is not a fact
    assert row["evidence"]["answered_trace"] == a and row["evidence"]["trace_id"] == b
    line = learnloop.evening_lines(time.time(), settings=s)
    assert line.startswith("Fixed: Lesson for repos: use gh and include private repos") and f"undo L{row['id']}" in line


def test_undo_removes_the_lesson_and_it_is_not_tried_again(s):
    turn(ASKED, WRONG)
    turn(RULE, "Sorry.")
    run(s)
    (row,) = learnloop.rows()
    ok, msg = learnloop.undo(row["id"], s)
    assert ok and f"Undone L{row['id']}" in msg and "lesson for repos" in msg
    assert lessons.load_all() == [] and lessons.block("my repos") == ("", [])
    assert learnloop.get(row["id"])["status"] == "undone"
    assert learnloop.undo(row["id"], s)[0] is False  # already reverted
    run(s)
    assert lessons.load_all() == [] and len(learnloop.rows()) == 1  # the signal key is in the ledger: not retried
    assert learnloop.evening_lines(time.time(), settings=s) == ""
    kind, text = chat_engine.approval_preflight(CHAT, f"undo L{row['id']}")
    assert "already reverted" in text


def test_the_dry_run_shows_the_lesson_and_writes_nothing(s):
    turn(ASKED, WRONG)
    turn(RULE, "Sorry.")
    rep = learnloop.run(s, now=time.time(), dry_run=True)
    (res,) = rep["results"]
    assert res["would"].startswith("write a keyword lesson") and res["match"] == ["repos"]
    assert lessons.load_all() == [] and learnloop.rows() == []


def test_a_lesson_that_would_fire_on_many_other_messages_is_reverted(s):
    for i in range(6):
        turn(f"what changed in my repos today number {i}", "Fine.", chat="telegram:222")
    turn(ASKED, WRONG)
    turn(RULE, "Sorry.")
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] == "reverted" and "too broad" in row["reason"]
    assert lessons.load_all() == []
    assert "put it back" in row["summary"] and "Tried a fix, put it back" in learnloop.evening_lines(time.time(), settings=s)


def test_a_rule_about_something_the_question_did_not_mention_is_not_made_into_a_lesson(s):
    assert lessons.from_correction("when does my robotics team meet?", "answer shorter next time", "No, answer shorter next time") is None
    assert lessons.from_correction("list my github repos", "my team meets on Tuesdays", RULE) is None  # a fact
    turn("when does my robotics team meet?", "Monday and Wednesday.")
    turn("No, always answer in one short line", "Sorry.")
    run(s)
    (row,) = learnloop.rows()
    assert row["status"] != "kept" and lessons.load_all() == []


def test_the_keywords_are_the_words_the_question_and_the_correction_share():
    assert lessons.keywords_from("list my github repos", "no, use gh and include private repos") == ["repos"]
    assert lessons.keywords_from("what's the weather", "no, include the humidity") == []
    prop = lessons.from_correction("list my github repos", "use gh and include private repos", RULE)
    assert prop["match"] == ["repos"] and prop["body"].startswith("Owner's rule (when they ask \"list my github repos\")")
    assert len(prop["body"]) <= lessons.BODY_MAX


# ---- 2. the hooks-alive check -------------------------------------------------------------------------------------------

def chat_turn(context="absent", when=None, request="hi"):
    """One recorded chat turn; context: a marker dict, None (marker field with no value) or "absent"."""
    extra = {"tools": []}
    if context != "absent":
        extra["context"] = context
    tid = traces.record(request, session="telegram:5", chat="telegram:5", route="chat", reply="ok", extra=extra,
                        origin="user")
    if when:
        con = traces._db()
        con.execute("UPDATE traces SET time=? WHERE id=?", (when, tid))
        con.commit()
        con.close()
    return tid


GOOD = {"v": 1, "core": True, "sources": ["core"], "lessons_checked": True, "lessons": []}
NO_CORE = {"v": 1, "core": False, "sources": [], "lessons_checked": True, "lessons": []}


def test_no_recent_turns_is_nothing_to_judge():
    r = context_alive.check()
    assert r["status"] == "ok" and not r["evaluated"]
    assert doctor.check_context()[0] == "ok"


def test_turns_that_carried_the_core_block_pass():
    for _ in range(3):
        chat_turn(GOOD)
    r = context_alive.check()
    assert r["status"] == "ok" and r["evaluated"] and r["carried"] == 3
    assert doctor.check_context() == ("ok", r["detail"], "")


def test_recent_turns_none_of_which_carried_the_block_fail_loudly():
    for _ in range(3):
        chat_turn(NO_CORE)
    status, detail, fix = doctor.check_context()
    assert status == "fail" and "none carried the core context block" in detail and "build_context" in fix
    chat_turn({"v": 1, "core": False, "built": False, "error": "RuntimeError"})
    assert "RuntimeError" in doctor.check_context()[1]


def test_one_good_turn_among_bad_ones_is_alive():
    chat_turn(NO_CORE)
    chat_turn(GOOD)
    chat_turn(NO_CORE)
    assert doctor.check_context()[0] == "ok"


def test_turns_from_before_the_marker_existed_are_not_judged():
    now = time.time()
    for i in range(4):
        chat_turn("absent", when=now - 5 * 3600 - i)  # answered by the old code
    assert doctor.check_context()[0] == "warn"  # recent turns, no marker ever: unknown, not a failure
    chat_turn(GOOD, when=now - 3600)
    chat_turn(GOOD, when=now - 60)
    r = context_alive.check(now)
    assert r["status"] == "ok" and r["turns"] == 2  # the four unmarked turns before the first marker do not count


def test_a_marker_that_stopped_being_written_is_a_failure():
    now = time.time()
    chat_turn(GOOD, when=now - 7200)
    for i in range(context_alive.RECENT_TURNS):
        chat_turn("absent", when=now - 600 + i)  # newer turns without the marker: the writing broke
    assert doctor.check_context()[0] == "fail"


def test_stored_lessons_with_no_turn_that_ran_the_lessons_step_fail():
    lessons.add(["repos"], "Use gh and include private repos when listing repositories.")
    chat_turn({"v": 1, "core": True, "sources": ["core"], "lessons_checked": False, "lessons": []})
    chat_turn({"v": 1, "core": True, "sources": ["core"], "lessons_checked": False, "lessons": []})
    status, detail, _ = doctor.check_context()
    assert status == "fail" and "lessons step" in detail
    lessons.set_enabled(False)  # switched off: nothing is expected of it
    assert doctor.check_context()[0] == "ok"


def test_the_doctor_runs_the_check_in_its_list():
    assert doctor.check_context.__name__ == "check_context"
    src = open(doctor.__file__, encoding="utf-8").read()
    assert '("Chat context", check_context)' in src


# ---- 2b. the daily canary ---------------------------------------------------------------------------------------------

PASS = {"ok": True, "id": "abc123", "cleaned": 4, "seconds": 3.0, "error": "", "checks": [
    {"name": "roundtrip", "ok": True, "seconds": 2.1, "detail": "answered in 2.1 s"}]}
NOW = time.time()


@pytest.fixture
def client(tmp_path):
    base = tmp_path / "tc"
    base.mkdir()
    (base / "owner.session").write_text("x")
    (base / "api.json").write_text("{}")
    return base


def run_canary(tmp_path, client, settings, result=PASS):
    script = tmp_path / "fake_canary.py"
    script.write_text(textwrap.dedent(f"import json\nprint(json.dumps({result!r}))\n"), encoding="utf-8")
    return cn.run_check(settings, NOW, reminder=False, alert_owner=False, base=client, python=sys.executable,
                        script=script)


def test_the_canary_fails_when_its_own_turn_carried_no_context_block(tmp_path, client, settings):
    chat_turn(NO_CORE)  # the turn the round trip just caused
    res = run_canary(tmp_path, client, settings)
    assert res["ok"] is False
    ctx = next(c for c in res["checks"] if c["name"] == "context")
    assert not ctx["ok"] and "context block missing" in ctx["detail"]
    assert not ledger.ready("canary.context", time.time()) and "context" in cn.failure_key(res)


def test_the_canary_passes_when_the_turn_carried_it(tmp_path, client, settings):
    chat_turn(GOOD)
    res = run_canary(tmp_path, client, settings)
    assert res["ok"] and [c["name"] for c in res["checks"]] == ["roundtrip", "context"]
    assert ledger.ready("canary.context", time.time())


def test_the_canary_fails_when_the_marker_was_never_written_at_all(tmp_path, client, settings):
    chat_turn("absent")
    res = run_canary(tmp_path, client, settings)
    assert res["ok"] is False and "none has ever recorded" in next(c for c in res["checks"] if c["name"] == "context")["detail"]


def test_the_canary_adds_no_context_check_when_there_is_nothing_to_judge(tmp_path, client, settings):
    res = run_canary(tmp_path, client, settings)  # no chat turn recorded here (the fake child has no Telegram)
    assert res["ok"] and [c["name"] for c in res["checks"]] == ["roundtrip"]
    failed = dict(PASS, ok=False, checks=[{"name": "roundtrip", "ok": False, "seconds": 9.0, "detail": "No answer."}])
    chat_turn(NO_CORE)
    res = run_canary(tmp_path, client, settings, failed)  # a failed round trip: its own failure is reported alone
    assert [c["name"] for c in res["checks"]] == ["roundtrip"]


def test_a_turn_from_before_the_canary_started_does_not_count(tmp_path, client, settings):
    chat_turn(NO_CORE, when=time.time() - 3600)
    res = run_canary(tmp_path, client, settings)
    assert res["ok"] and [c["name"] for c in res["checks"]] == ["roundtrip"]


# ---- 3. fact ordering: an older statement that arrives later never replaces a newer one ------------------------------------

DAY = 86400


def old_turn(request, days_ago):
    tid = turn(request, "Noted.")
    con = traces._db()
    con.execute("UPDATE traces SET time=? WHERE id=?", (time.time() - days_ago * DAY, tid))
    con.commit()
    con.close()
    return traces.get(tid)


def school(text):
    return {"kind": "about", "key": "school", "text": text}


def test_a_late_older_single_value_fact_does_not_replace_the_newer_one(s):
    new_t, old_t = old_turn("I go to Fargo North High", 1), old_turn("I go to Fargo South High", 6)
    ok, newer = learn.file_fact(school("school is Fargo North High"), s, source="digest d1", turn=new_t)
    assert ok
    ok, why = learn.file_fact(school("school is Fargo South High"), s, source="digest d6", turn=old_t)  # arrives later
    assert not ok and "older than fact" in why and f"#{newer}" in why
    (active,) = learn.facts("active", "about")
    assert active["id"] == newer and "North" in active["text"] and learn.facts("replaced") == []
    assert "South" not in vault.read("Me/Profile.md", s) and "North" in vault.read("Me/Profile.md", s)


def test_in_the_right_order_the_newer_fact_replaces_the_older(s):
    old_t, new_t = old_turn("I go to Fargo South High", 6), old_turn("I go to Fargo North High", 1)
    ok, first = learn.file_fact(school("school is Fargo South High"), s, source="digest d6", turn=old_t)
    ok2, second = learn.file_fact(school("school is Fargo North High"), s, source="digest d1", turn=new_t)
    assert ok and ok2
    (active,) = learn.facts("active", "about")
    assert active["id"] == second
    (gone,) = learn.facts("replaced")
    assert gone["id"] == first and gone["replaced_by"] == second


def test_an_explicit_supersedes_id_does_not_close_a_newer_fact_either(s):
    new_t, old_t = old_turn("my team meets on Tuesday and Thursday", 1), old_turn("my team meets on Monday", 5)
    ok, newer = learn.file_fact({"kind": "about", "key": "routine", "text": "team meets on Tuesday and Thursday"}, s,
                                source="digest d1", turn=new_t)
    assert ok
    ok, why = learn.file_fact({"kind": "about", "key": "routine", "text": "team meets on Monday only"}, s,
                              source="digest d5", turn=old_t, supersedes=[newer])
    assert not ok and "older than fact" in why
    assert [f["id"] for f in learn.facts("active")] == [newer]
    # the same call from a turn that really is newer does close it
    later = old_turn("actually the team meets on Friday now", 0)
    ok, third = learn.file_fact({"kind": "about", "key": "routine", "text": "team meets on Friday now"}, s,
                                source="digest d0", turn=later, supersedes=[newer])
    assert ok and learn.facts("replaced")[0]["id"] == newer


def test_a_late_older_like_does_not_close_a_newer_dislike(s):
    new_t, old_t = old_turn("I hate pineapple pizza", 1), old_turn("I love pineapple pizza", 8)
    ok, newer = learn.file_fact({"kind": "dislike", "text": "dislikes pineapple pizza"}, s, source="digest d1", turn=new_t)
    assert ok
    ok, why = learn.file_fact({"kind": "like", "text": "likes pineapple pizza"}, s, source="digest d8", turn=old_t)
    assert not ok and "older than fact" in why
    assert [f["kind"] for f in learn.facts("active")] == ["dislike"]


def test_a_correction_from_the_learning_loop_cannot_overwrite_a_fact_stated_after_it(s):
    new_t, old_t = old_turn("I go to Fargo North High", 1), old_turn("no, I go to Fargo South High", 4)
    ok, newer = learn.file_fact(school("school is Fargo North High"), s, source="digest d1", turn=new_t)
    assert ok
    change, why = learnloop.apply_fact(school("school is Fargo South High"), s, turn=old_t,
                                       source="learnloop correction", evidence="no, I go to Fargo South High")
    assert change is None and "older than fact" in why
    assert [f["id"] for f in learn.facts("active")] == [newer]


def test_a_system_fact_does_not_block_a_statement_filed_after_it(s):
    """A calendar, mail or quiz fact carries the day it was synced, not the day it was said."""
    t = old_turn("I go to Fargo South High", 3)
    learn._insert({"kind": "about", "key": "school", "text": "school is Fargo North High", "source": "quiz",
                   "origin": "memory", "observed": time.time(), "valid_from": time.strftime("%Y-%m-%d")})
    ok, fid = learn.file_fact(school("school is Fargo South High"), s, source="digest d3", turn=t)
    assert ok and fid


# ---- 3b. the digest prompt ------------------------------------------------------------------------------------------------

def test_the_digest_prompt_states_the_correction_rule():
    p = " ".join(digest.DISTILL_SYSTEM.split())
    assert "a correction replaces the older fact at the same scope" in p
    assert "a later mention alone is not a correction" in p
    assert "Never let an older statement replace a newer one" in p
    assert '"supersedes"' in p  # the schema key the rule leans on is still there
