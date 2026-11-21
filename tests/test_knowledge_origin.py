"""Only the user's own words teach Waggle (rebuild plan decision 10), and facts carry their source and validity.
Invented content only."""
import datetime as dt
import json
import time

import pytest

from waggle import digest, events, kstore, learn, learning, memory, traces, vault


@pytest.fixture
def s(settings, tmp_path):
    settings.watch_roots = [str(tmp_path / "work")]
    settings.save()
    (tmp_path / "work").mkdir()
    vault.init(settings)
    return settings


def _noon(day):
    return time.mktime(dt.datetime.combine(day, dt.time(21)).timetuple())


def _row(**kw):
    return {"id": 1, "time": time.time(), "request": "x", "origin": None, "platform": "", **kw}


# ---- which turns count -------------------------------------------------------------------------------

@pytest.mark.parametrize("origin,platform,counts", [
    ("user", "telegram", True), ("user", "", True), ("user", "cli", True),
    ("test", "telegram", False), ("canary", "telegram", False), ("bench", "", False), ("agent", "telegram", False),
    (None, "telegram", True), (None, "voice", True),
    (None, "cli", False), (None, "CLI", False), (None, "", False), (None, None, False)])
def test_which_turns_are_the_users_own(origin, platform, counts):
    assert learn.is_user_turn(_row(origin=origin, platform=platform)) is counts


def test_user_turns_reads_through_the_traces_and_drops_every_other_origin(s):
    ids = {}
    ids["user"] = traces.record("a real one", platform="telegram", origin="user")
    ids["old"] = traces.record("an old telegram row", platform="telegram")
    ids["cli_old"] = traces.record("an old cli row", platform="cli")
    ids["agent_old"] = traces.record("an old agent row", session="objective-1")
    ids["test"] = traces.record("a test", platform="telegram", origin="test")
    ids["canary"] = traces.record("a canary", platform="telegram", origin="canary")
    got = {r["id"] for r in learn.user_turns(0)}
    assert got == {ids["user"], ids["old"]}


# ---- a test-origin turn teaches nothing, anywhere ---------------------------------------------------------

STATED = "I have a robotics meet Saturday and a math test tomorrow"


def _fake_for(day, quote=STATED):
    return {"summary": "A day.", "conversations": [], "outcomes": [],
            "facts": [{"kind": "event", "text": "Robotics meet", "date": (day + dt.timedelta(days=2)).isoformat(),
                       "quote": quote},
                      {"kind": "preference", "text": "Math test tomorrow matters", "quote": quote}]}


@pytest.mark.parametrize("origin,platform", [("test", "telegram"), ("canary", "telegram"), ("bench", "cli"),
                                              (None, "cli"), (None, "")])
def test_a_non_user_turn_that_states_a_fact_produces_nothing(s, origin, platform):
    today = dt.date.today()
    traces.record(STATED, session="dev", platform=platform, origin=origin, reply="Good luck!")
    seen = []
    out = digest.run(s, now=_noon(today), asker=lambda p, **k: seen.append(p) or {"json": _fake_for(today)})
    assert seen == [] and out["distilled"] == [] and out["written"] == 0  # not even sent to the model
    assert learn.facts(status=None) == []
    assert "Robotics meet" not in vault.read("Knowledge/Upcoming", s)
    assert "math test" not in vault.read(f"Journal/{today:%Y-%m-%d}", s).lower()
    assert digest.yesterday_block(s, now=_noon(today + dt.timedelta(days=1))) == ""


def test_a_non_user_turn_mixed_into_a_days_rows_is_still_refused_by_the_filing_step(s):
    today = dt.date.today()
    tid = traces.record(STATED, session="dev", platform="telegram", origin="test")
    rows = [r for r in traces.all_traces(0, include_non_user=True) if r["id"] == tid]
    r = digest.file_results(today, rows, _fake_for(today), s)
    assert r["facts_filed"] == 0 and learn.facts(status=None) == []
    old_cli = traces.record(STATED, session="dev", platform="cli")
    rows = [r for r in traces.all_traces(0, include_non_user=True) if r["id"] == old_cli]
    assert digest.file_results(today, rows, _fake_for(today), s)["facts_filed"] == 0
    ok, why = learn.file_fact({"kind": "preference", "text": "Likes long walks"}, s, source="digest x",
                              turn=rows[0])
    assert not ok and "user's own words" in why
    ok, why = learn.file_fact({"kind": "preference", "text": "Likes long walks"}, s, source="digest x")
    assert not ok and why == "no source turn"


def test_the_remember_tool_refuses_inside_a_test_run(s, monkeypatch):
    monkeypatch.setenv("WAGGLE_ORIGIN", "test")
    assert learn.file_facts([{"kind": "preference", "text": "Likes long walks"}], s) == []
    monkeypatch.delenv("WAGGLE_ORIGIN")
    assert len(learn.file_facts([{"kind": "preference", "text": "Likes long walks"}], s)) == 1
    f = learn.facts()[0]
    assert f["origin"] == "user" and f["source_turn"] is None and f["invalid_at"] is None


def test_the_digest_model_only_sees_the_users_turns(s):
    today = dt.date.today()
    traces.record("a development prompt about robots", session="dev", platform="cli", reply="ok")
    traces.record("a test run prompt", platform="telegram", origin="test", reply="ok")
    traces.record("what is the load cell range on my hive scale", platform="telegram", origin="user", reply="10 kg")
    seen = []
    digest.run(s, now=_noon(today), asker=lambda p, **k: seen.append(p) or {"json": {
        "summary": "s", "conversations": [], "facts": [], "outcomes": []}})
    assert len(seen) == 1 and "load cell" in seen[0]
    assert "development prompt" not in seen[0] and "test run prompt" not in seen[0]


def test_nothing_else_that_reads_conversations_sees_a_test_turn(s):
    traces.record("a test run prompt", platform="telegram", origin="test", reply="ok")
    traces.record("an old development prompt", platform="cli", reply="ok")
    now = time.time()
    assert learning.analyze(1)["turns"] == 0 and learning.discover_skills() == []
    day = learning.growth_today(s, now, None, {})
    assert day["turns"] == 0
    assert memory.previous_exchange() is None
    traces.record("my real question", platform="telegram", origin="user", reply="an answer")
    assert memory.previous_exchange() == ("my real question", "an answer")


# ---- a user turn teaches, with its source ------------------------------------------------------------------

def test_a_user_turn_files_a_fact_that_carries_its_source(s):
    today = dt.date.today()
    tid = traces.record("I love honey on toast, my scale reads 3.2 kg", platform="telegram", origin="user",
                        reply="Nice.")
    traces.record(STATED, session="dev", platform="cli", reply="x")  # same day, a test: must not matter
    rows = digest.day_traces(today)
    fake = {"facts": [{"kind": "like", "text": "Likes honey on toast", "quote": "I love honey on toast"}]}
    assert digest.file_results(today, rows, fake, s)["facts_filed"] == 1
    f = learn.facts()[0]
    assert f["source_turn"] == tid and f["origin"] == "user" and f["status"] == "active"
    assert f["observed"] == pytest.approx(traces.get(tid)["time"]) and f["valid_from"] == today.isoformat()
    assert f["invalid_at"] is None
    assert "Likes honey on toast" in vault.read("Me/Likes", s)


def test_an_old_row_with_no_origin_from_telegram_counts_and_says_user(s):
    today = dt.date.today()
    tid = traces.record("I hate long answers", platform="telegram", reply="Ok.")
    rows = digest.day_traces(today)
    fake = {"facts": [{"kind": "preference", "text": "Wants short answers", "quote": "I hate long answers"}]}
    assert digest.file_results(today, rows, fake, s)["facts_filed"] == 1
    assert learn.facts()[0]["source_turn"] == tid and learn.facts()[0]["origin"] == "user"


# ---- a later contradiction ends the old fact ------------------------------------------------------------------

def _file(s, day, request, fact, supersedes=None, **kw):
    """One user turn on `day` and the digest filing the fact it states."""
    t = time.mktime(dt.datetime.combine(day, dt.time(10)).timetuple())
    tid = traces.record(request, platform="telegram", origin="user", reply="ok")
    con = traces._db()
    con.execute("UPDATE traces SET time=? WHERE id=?", (t, tid))
    con.commit()
    con.close()
    fact = {**fact, "quote": request, **({"supersedes": supersedes} if supersedes else {})}
    out = digest.file_results(day, digest.day_traces(day), {"facts": [fact]}, s)
    return tid, out


def test_a_contradicting_later_user_turn_closes_the_earlier_fact(s):
    d1 = dt.date.today() - dt.timedelta(days=3)
    d2 = dt.date.today() - dt.timedelta(days=1)
    t1, _ = _file(s, d1, "I really like pineapple pizza", {"kind": "like", "text": "Likes pineapple pizza"})
    old = learn.facts()[0]
    assert "pineapple pizza" in vault.read("Me/Likes", s)
    t2, out = _file(s, d2, "I hate pineapple pizza now", {"kind": "dislike", "text": "Hates pineapple pizza"})
    assert out["facts_filed"] == 1
    active = learn.facts("active")
    assert [f["text"] for f in active] == ["Hates pineapple pizza"]
    closed = [f for f in learn.facts("replaced")]
    assert [f["id"] for f in closed] == [old["id"]] and closed[0]["invalid_at"] == d2.isoformat()
    assert closed[0]["replaced_by"] == active[0]["id"] and closed[0]["source_turn"] == t1
    assert active[0]["source_turn"] == t2 and active[0]["valid_from"] == d2.isoformat()
    assert "pineapple" not in vault.read("Me/Likes", s)  # no longer filed where retrieval reads


def test_a_single_value_detail_and_a_model_named_supersede_close_the_old_one(s):
    d1 = dt.date.today() - dt.timedelta(days=3)
    d2 = dt.date.today() - dt.timedelta(days=1)
    _file(s, d1, "I live in Springfield", {"kind": "about", "key": "home", "text": "Lives in Springfield"})
    _file(s, d1, "I want to build a hive camera", {"kind": "goal", "text": "Wants to build a hive camera"})
    goal = learn.facts("active", "goal")[0]
    _file(s, d2, "I moved to Shelbyville", {"kind": "about", "key": "home", "text": "Lives in Shelbyville"})
    home_old = [f for f in learn.facts("replaced") if f["key"] == "home"][0]
    assert home_old["invalid_at"] == d2.isoformat() and home_old["text"] == "Lives in Springfield"
    _file(s, d2, "I dropped the camera idea, it is a scale now",
          {"kind": "goal", "text": "Wants to build a hive scale"}, supersedes=[goal["id"]])
    assert [f["text"] for f in learn.facts("active", "goal")] == ["Wants to build a hive scale"]
    assert learn.facts("replaced", "goal")[0]["invalid_at"] == d2.isoformat()
    assert "hive camera" not in vault.read("Me/Learned", s) and "hive scale" in vault.read("Me/Learned", s)


def test_supersedes_with_an_unknown_or_own_id_changes_nothing_else(s):
    d = dt.date.today() - dt.timedelta(days=1)
    _file(s, d, "I like jazz a lot", {"kind": "like", "text": "Likes jazz a lot"})
    _file(s, d, "I own a hive scale", {"kind": "fact", "text": "Owns a hive scale"}, supersedes=[999, "x"])
    assert len(learn.facts("active")) == 2


def test_an_old_facts_database_gets_the_new_columns(settings, tmp_path):
    import sqlite3
    con = sqlite3.connect(tmp_path / "home" / "knowledge.db")
    con.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, kind TEXT, key TEXT, "
                "text TEXT, date TEXT, expires TEXT, source TEXT, evidence TEXT, status TEXT DEFAULT 'active', "
                "replaced_by INTEGER, note TEXT)")
    con.execute("INSERT INTO facts (created, kind, text) VALUES (1, 'like', 'Likes tea')")
    con.commit()
    con.close()
    f = learn.facts()[0]
    assert f["text"] == "Likes tea" and f["source_turn"] is None and f["invalid_at"] is None
    assert {c for c, _ in kstore.FACT_COLUMNS} <= set(f)
